"""Trusted orchestration: freeze read-only inputs, then atomically aggregate.

Rules and the semantic provider have no DB, browser, path or execution authority.
Only this service reads immutable artifact bytes and commits terminal outputs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from functools import wraps
import inspect
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..events import ResultEvent, append_event
from ..evidence.redaction import TextRedactor, safe_metadata
from ..evidence.store import EvidenceStore
from ..models.schema import ProposeResult
from ..scheduler.models import ExecutionToken, Resource
from ..scheduler.store import SchedulerStore, validate_in_transaction
from ..state import transition_in_transaction
from ..tasks.models import TaskContract
from .models import Check, EvidenceDocument, FieldBinding, Result, SideEffect, Verdict
from .rules import evaluate_rules
from .arxiv import PARSER_VERSION as ARXIV_PARSER_VERSION, parse_visible as parse_arxiv_visible


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('Ambiguous original JSON')
        value[key] = child
    return value


def _storage_checked(method):
    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            with self.evidence._storage_errors():
                return await method(self, *args, **kwargs)
        return asynchronous
    @wraps(method)
    def synchronous(self, *args, **kwargs):
        with self.evidence._storage_errors():
            return method(self, *args, **kwargs)
    return synchronous


def _conflict(message='Verification inputs or execution qualification changed'):
    return BusinessError('STATE_CONFLICT', message, status=409)


def _refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'evidence_ids':
                yield from child
            else:
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


class VerificationService:
    def __init__(self, data_dir: Path, *, scheduler=None, fault_hook=None):
        self.data_dir = Path(data_dir)
        self.database = self.data_dir / 'business.sqlite3'
        self.evidence = EvidenceStore(self.data_dir)
        self.scheduler = scheduler or SchedulerStore(self.database)
        self.budgets = self.scheduler.budgets
        self.fault_hook = fault_hook

    def _hook(self, stage):
        if self.fault_hook:
            self.fault_hook(stage)

    @staticmethod
    def _run(db, run_id, version, token, *, state='VERIFYING'):
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        if type(version) is not int or version != row['state_version'] or row['state'] != state:
            raise _conflict()
        scheduled = db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
        if token is not None:
            if not isinstance(token,ExecutionToken) or token.run_id != run_id:
                raise _conflict()
            # Read-only verification is useful during uncertain writes, but may
            # not turn their existence into success. Aggregation inspects all.
            validate_in_transaction(db, token, allow_reconciling=True)
        elif scheduled:
            raise BusinessError('RESOURCE_CONFLICT', 'Verification requires current execution qualification', status=409)
        content = db.execute('SELECT content_json FROM contracts WHERE task_id=? AND contract_version=?',
                             (row['task_id'], row['contract_version'])).fetchone()[0]
        if hashlib.sha256(content.encode()).hexdigest() != row['contract_sha256']:
            raise _conflict('Frozen contract integrity mismatch')
        try:
            contract = TaskContract.model_validate_json(content)
        except ValidationError:
            raise BusinessError('INVALID_PARAMETER', 'Run requires a complete frozen contract') from None
        return dict(row), contract

    @_storage_checked
    def begin(self, run_id, expected_state_version, execution_token=None):
        """Enter VERIFYING and refresh the same lease; never dispatch an action."""
        with connect(self.database) as db, transaction(db):
            self._run(db, run_id, expected_state_version, execution_token, state='RUNNING')
            from ..controls.models import ControlPending
            from ..controls.store import ControlStore
            pending = ControlStore.pending_in_transaction(db, run_id)
            if pending is not None:
                raise ControlPending(pending)
            budget = self.budgets.on_transition(db, run_id, 'VERIFYING')
            if budget and budget['exhausted']:
                raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409)
            transition_in_transaction(db, run_id=run_id, expected_state_version=expected_state_version, target='VERIFYING')
            if execution_token is None:
                return None
            token = execution_token
            now, _ = self.scheduler._time()
            row = self.scheduler._row(db, run_id)
            self.scheduler._change(db, row, now, 'heartbeat', run_state_version=expected_state_version + 1)
            return ExecutionToken(token.run_id, token.worker_id, token.worker_generation, token.epoch,
                                  expected_state_version + 1, token.expires_at, token.resources)

    def _documents(self, db, run_id, ids):
        """Called under the artifact lock; no external I/O, no untrusted paths."""
        documents, fingerprints = [], []
        self.evidence.assert_dispatch_allowed(db=db)
        for evidence_id in ids:
            item, raw, chain, problem = None, None, [], None
            try:
                item = self.evidence._metadata(db, evidence_id, run_id)
                seen = set()
                while True:
                    if item['evidence_id'] in seen or len(seen) >= 8:
                        raise _conflict()
                    seen.add(item['evidence_id'])
                    chain.append({key: item[key] for key in ('evidence_id','sha256','availability','expires_at','original_evidence_id')})
                    if (item['capture_status'] != 'COMPLETE' or item['availability'] != 'AVAILABLE'
                            or item['expires_at'] is not None and item['expires_at'] <= utc_text()):
                        raise BusinessError('EVIDENCE_MISSING', 'Evidence unavailable', status=409)
                    data = self.evidence.files.read(item['artifact_path'], size_bytes=item['size_bytes'], sha256=item['sha256'])
                    if item['original_evidence_id'] is None:
                        raw = data
                        break
                    item = self.evidence._metadata(db, item['original_evidence_id'], run_id)
            except (BusinessError, OSError):
                problem = 'original_artifact_unavailable'
            content, qualified_arxiv = None, False
            if raw is not None and item['artifact_kind'] in ('text','ci','diff','har') and len(raw) <= 4_000_000:
                try:
                    text = raw.decode('utf-8')
                    try:
                        content = json.loads(text, object_pairs_hook=_unique_object,
                                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        canonical_json(content)  # reject nonfinite/unsupported values
                        # A browser capture is an immutable {title,text} envelope.
                        # Decode the entire visible JSON, never a model-selected
                        # substring or a generated derivative. The original
                        # bytes/hash remain the evidence authority.
                        if (item.get('snapshot_id') is not None and item['object_id'] == item['snapshot_id']
                                and isinstance(content, dict)
                                and (set(content) == {'title', 'text'} or set(content) == {'title', 'text', 'text_truncated'}
                                    and content['text_truncated'] is False)
                                and isinstance(content['text'], str)):
                            original_envelope = dict(content)
                            try:
                                parsed = json.loads(content['text'], object_pairs_hook=_unique_object,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                                canonical_json(parsed)
                                content = {**content, 'parsed_text': parsed}
                            except (ValueError, RecursionError):
                                pass  # Plain visible text needs a site parser.
                            arxiv = parse_arxiv_visible(item['source_url'], original_envelope)
                            if arxiv is not None:
                                projection = self._arxiv_direct_projection(db, run_id, item, arxiv)
                                if projection is not None:
                                    content = {**content, 'arxiv_direct': projection}
                                    qualified_arxiv = True
                    except (ValueError, RecursionError):
                        if text.lstrip().startswith(('{','[')):
                            problem = 'original_json_ambiguous_or_invalid'
                        else:
                            content = {'text': text}
                except UnicodeError:
                    problem = 'original_text_unreadable'
            elif raw is not None:
                problem = 'original_format_requires_parser'
            # This namespace belongs only to the pure parser plus execution
            # ledger projection above. Original JSON cannot self-declare it.
            if isinstance(content, dict) and 'arxiv_direct' in content and not qualified_arxiv:
                content = {key: value for key, value in content.items() if key != 'arxiv_direct'}
            if item is None:
                item = dict(source_url='https://unavailable.invalid/', captured_at='1970-01-01T00:00:00Z', sha256='0'*64,
                            object_id='unavailable', locator_or_page='unavailable',artifact_kind='text',commit_sha=None,test_run_id=None)
            documents.append(EvidenceDocument.model_validate_json(canonical_json(dict(
                evidence_id=evidence_id, run_id=run_id, content=content, readable=problem is None, problem=problem,
                snapshot_id=item.get('snapshot_id'),
                **{k:item[k] for k in ('source_url','captured_at','artifact_kind','sha256','object_id','locator_or_page','commit_sha','test_run_id')}))))
            fingerprints.append({'evidence_id':evidence_id, 'chain':chain, 'problem':problem,
                **({'arxiv_direct': content['arxiv_direct']} if isinstance(content, dict)
                    and 'arxiv_direct' in content else {})})
        return tuple(documents), _hash(fingerprints)

    @staticmethod
    def _arxiv_direct_projection(db, run_id, item, publication):
        """Bind one-page coverage to current immutable execution ledgers.

        Frozen query/cutoff define the requested boundary; title/authors/dates
        are parsed exclusively from original text. Sources and page counts
        must have an actual gateway observation and completed navigation.
        No model proposal enters this function. Ledger facts enter the digest
        rechecked before publication and again before final aggregation.
        """
        from ..tasks.models import ResearchParameters
        row = db.execute('''SELECT r.contract_sha256,c.content_json FROM runs r JOIN contracts c
            ON c.task_id=r.task_id AND c.contract_version=r.contract_version WHERE r.run_id=?''',
            (run_id,)).fetchone()
        if row is None or hashlib.sha256(row['content_json'].encode()).hexdigest() != row['contract_sha256']:
            return None
        try:
            contract = TaskContract.model_validate_json(row['content_json'])
        except (ValueError, TypeError):
            return None
        query = 'arxiv:' + publication['canonical_id'] + publication['version']
        if (not isinstance(contract.parameters, ResearchParameters)
                or contract.action_policy.mode != 'read_only' or contract.identity_ref is not None
                or contract.parameters.queries != [query] or contract.parameters.max_items != 1
                or len(contract.sources) != 1 or contract.start_urls != [item['source_url']]
                or not contract.sources[0].permits(item['source_url'])):
            return None
        observations = [dict(value) for value in db.execute('''SELECT o.snapshot_id,o.source_url,o.captured_at,
            g.capture_sha256,g.epoch FROM observations o JOIN gateway_observations g
            ON g.run_id=o.run_id AND g.snapshot_id=o.snapshot_id WHERE o.run_id=?
            ORDER BY o.captured_at,o.snapshot_id LIMIT 2049''', (run_id,))]
        if (not observations or len(observations) > 2048
                or not any(value['snapshot_id'] == item['snapshot_id']
                    and value['source_url'] == item['source_url'] for value in observations)
                or any(value['source_url'] not in ('about:blank', item['source_url']) for value in observations)):
            return None
        steps = [dict(value) for value in db.execute('''SELECT s.step_id,s.status,s.ended_at,
            s.actual_result_json,g.action_kind,g.external_write FROM steps s JOIN gateway_attempts g
            ON g.run_id=s.run_id AND g.step_id=s.step_id WHERE s.run_id=? ORDER BY s.sequence LIMIT 151''',
            (run_id,))]
        navigation = [value for value in steps if value['action_kind'] == 'navigate']
        if (not steps or len(steps) > 150 or len(navigation) != 1
                or any(value['status'] != 'COMPLETED' or value['external_write']
                    or value['action_kind'] not in ('navigate', 'read_visible', 'screenshot', 'scroll') for value in steps)):
            return None
        try:
            if json.loads(navigation[0]['actual_result_json']).get('source_url') != item['source_url']:
                return None
        except (ValueError, AttributeError):
            return None
        if navigation[0]['ended_at'] is None or item['captured_at'] < navigation[0]['ended_at']:
            return None
        debits = [dict(value) for value in db.execute('''SELECT attempt_id,kind,content_pages,payload_sha256
            FROM budget_attempts WHERE run_id=? AND content_pages=1 ORDER BY attempt_id LIMIT 26''', (run_id,))]
        budget = db.execute('SELECT content_pages_used FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()
        if (len(debits) != 1 or budget is None or budget[0] != 1 or debits[0]['kind'] != 'action'
                or debits[0]['attempt_id'] != 'gateway-' + _hash([run_id, navigation[0]['step_id']])):
            return None
        return {'publications': [publication], 'coverage': {
            'searched_sources': [contract.sources[0].source_id], 'queries': [query],
            # This is frozen query metadata, explicitly distinguished from
            # publication dates proven by the UTC submission history.
            'cutoff_at': contract.parameters.model_dump(mode='json')['cutoff_at'],
            'content_pages': 1, 'unread_candidates': [], 'gaps': [], 'complete': True},
            'reader_provenance': {'parser_version': ARXIV_PARSER_VERSION,
                'scope': 'one-explicit-versioned-abstract-page', 'contract_sha256': row['contract_sha256'],
                'text_truncated': False,
                'cutoff_basis': 'frozen-request-boundary', 'observations': observations,
                'steps': steps, 'content_page_debits': debits}}

    @_storage_checked
    async def verify(self, run_id, proposal, bindings, *, expected_state_version, execution_token=None, provider=None):
        try:
            proposal = ProposeResult.model_validate_json(proposal.model_dump_json())
            bindings = tuple(FieldBinding.model_validate_json(item.model_dump_json()) for item in bindings)
            if len(bindings) > 20000 or len(proposal.model_dump_json().encode()) > 4_000_000:
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise BusinessError('INVALID_PARAMETER', 'A bounded result proposal and field bindings are required') from None
        # Detected credentials in candidate output cannot become a public Result.
        redactor = TextRedactor(known_secrets=tuple(getattr(provider, 'sensitive_literals', ())))
        def safe(value):
            if isinstance(value,str):
                return redactor.filter(value) == value
            if isinstance(value,dict):
                return all(safe(k) and safe(v) for k,v in value.items())
            if isinstance(value,list):
                return all(safe(v) for v in value)
            return True
        if not safe(proposal.model_dump(mode='json')):
            raise BusinessError('INPUT_BLOCKED','Sensitive candidate output cannot be published',status=409)
        ids = sorted(set(_refs(proposal.model_dump(mode='json'))) | {b.evidence_id for b in bindings})
        if len(ids) > 256:
            raise BusinessError('INVALID_PARAMETER', 'Too many evidence references')
        with self.evidence.files.locked(), connect(self.database) as db:
            run, contract = self._run(db, run_id, expected_state_version, execution_token)
            documents, digest = self._documents(db, run_id, ids)
        checked_at = datetime.now(timezone.utc)
        evaluation = evaluate_rules(contract, proposal, documents, bindings, checked_at=checked_at, run_id=run_id)
        checks, unresolved = list(evaluation.checks), list(evaluation.unresolved)
        def revalidate():
            with self.evidence.files.locked(), connect(self.database) as db:
                self._run(db, run_id, expected_state_version, execution_token)
                _, current = self._documents(db, run_id, ids)
                if current != digest:
                    raise _conflict('Evidence changed during verification')
                self._run(db, run_id, expected_state_version, execution_token)
        if evaluation.semantic_criterion_ids:
            from .semantic import verify_semantics
            criteria = [c for c in contract.acceptance_criteria if c.criterion_id in evaluation.semantic_criterion_ids]
            semantic, pending = await verify_semantics(self.database, provider, run=run, contract=contract,
                proposal=proposal, documents=documents, criteria=criteria, execution_token=execution_token, revalidate=revalidate)
            by_id = {check.criterion_id:check for check in semantic}
            # Independent semantics may fill only explicitly pending semantics;
            # source/rule FAIL or CONFLICT can never be overwritten by a model.
            checks = [by_id.get(check.criterion_id, check) if check.criterion_id in evaluation.semantic_criterion_ids
                      and check.verdict == Verdict.INSUFFICIENT and check.actual == {'code':'independent_semantic_review_required'}
                      else check for check in checks]
            unresolved.extend(pending)
        capsule = dict(proposal=proposal.model_dump(mode='json'), bindings=[b.model_dump(mode='json') for b in bindings],
                       evaluation=evaluation.model_dump(mode='json'), checks=[c.model_dump(mode='json') for c in checks],
                       unresolved=sorted(set(unresolved)), evidence_ids=ids, checked_at=utc_text(checked_at))
        payload = canonical_json(capsule)
        verification_id = 'verification-' + uuid4().hex
        with self.evidence.files.locked(), connect(self.database) as db, transaction(db):
            fresh, _ = self._run(db, run_id, expected_state_version, execution_token)
            _, current = self._documents(db, run_id, ids)
            if digest != current:
                raise _conflict('Evidence changed during verification')
            # Artifact reads are bounded but can outlive a short lease. Time
            # expiry must be checked again at the actual publication boundary.
            self._run(db, run_id, expected_state_version, execution_token)
            db.execute('INSERT INTO run_verifications VALUES(?,?,?,?,?,?,?,?,?)',
                       (verification_id,run_id,expected_state_version,fresh['contract_sha256'],
                        _hash([capsule['proposal'],capsule['bindings']]),digest,payload,hashlib.sha256(payload.encode()).hexdigest(),utc_text()))
        return dict(verification_id=verification_id, state_version=expected_state_version,
                    evaluation=capsule['evaluation'], checks=capsule['checks'])

    def _effects(self, db, run, contract):
        effects, unresolved, evidence_ids = [], [], set()
        policy = contract.action_policy
        for item in db.execute('SELECT * FROM write_intents WHERE task_id=? ORDER BY operation_id', (run['task_id'],)):
            refs = [row[0] for row in db.execute('SELECT evidence_id FROM write_intent_evidence WHERE operation_id=? ORDER BY evidence_id', (item['operation_id'],))]
            protocol = (db.execute("SELECT 1 FROM sqlite_schema WHERE name='write_protocol_claims'").fetchone()
                        and db.execute('SELECT 1 FROM write_protocol_claims WHERE operation_id=?', (item['operation_id'],)).fetchone())
            check = None
            operation_name = item['expected_change']
            if protocol:
                from ..writes.store import WriteProtocolStore
                claim = db.execute('SELECT target_json FROM write_protocol_claims WHERE operation_id=?',
                                   (item['operation_id'],)).fetchone()
                operation_name = json.loads(claim['target_json'])['operation']
                check = WriteProtocolStore.verified_check_for_run(db, item['operation_id'], run['run_id'])
                qualification = db.execute('SELECT epoch,worker_id,worker_generation FROM scheduler_queue WHERE run_id=?',
                                           (run['run_id'],)).fetchone()
                if check and qualification and any(check[key] != qualification[key]
                        for key in ('epoch','worker_id','worker_generation')):
                    check = None
                # Historical evidence remains linked to its original operation;
                # the current output uses a current Run's independent query.
                refs = check['evidence_ids'] if check else [row[0] for row in db.execute(
                    'SELECT evidence_id FROM write_intent_evidence WHERE operation_id=? AND run_id=? ORDER BY evidence_id',
                    (item['operation_id'], run['run_id']))]
            evidence_ids.update(refs)
            kinds = {'branch_create':'branch','branch_push':'commit','pr_create':'pr','benchmark_write':'benchmark_write',
                     'create_branch':'branch','commit':'commit','edit_file':'commit','push':'commit','create_pr':'pr','update_pr':'pr',
                     'branch':'branch','pr':'pr'}
            effect_type = kinds.get(operation_name)
            # Existing operation identity, scope and evidence are business facts,
            # never inferred from the executor's list of successful operations.
            allowed = (policy.mode == 'repository_write' and effect_type in ('branch','commit','pr')
                       and item['target'] == Resource.repository_write(policy.repository).resource_key
                       and operation_name in policy.allowed_operations
                       and item['identity_ref'] == contract.identity_ref)
            critical = not allowed or effect_type is None
            status, receipt = item['status'], item['receipt']
            if status == 'CONFIRMED' and (not refs or not receipt):
                # Preserve confirmed historical status. The protocol requires
                # its receipt and evidence, so an inconsistent ledger is an error.
                raise BusinessError('EVIDENCE_MISSING','Confirmed operation lacks receipt evidence',status=409)
            if status in ('INTENT','UNKNOWN'):
                unresolved.append('unresolved_write:' + item['operation_id'])
            if critical:
                unresolved.append('unauthorized_side_effect:' + item['operation_id'])
            if status == 'CONFIRMED':
                verified_receipt = False
                for eid in refs:
                    try:
                        meta = self.evidence._metadata(db, eid)
                        docs, _ = self._documents(db, meta['run_id'], [eid])
                        original = docs[0]
                        if not original.readable or meta['capture_status'] != 'COMPLETE':
                            raise ValueError()
                        if meta['run_id'] != run['run_id']:
                            unresolved.append('historical_receipt_requires_current_verification:' + item['operation_id'])
                        elif isinstance(original.content,dict) and any(s.permits(original.source_url) for s in contract.sources):
                            # A versioned receipt capture must expose these
                            # independently observed facts. A readable but
                            # unrelated artifact is not evidence of a write.
                            if protocol:
                                verified_receipt |= bool(check and original.content == check['facts']
                                    and _hash(check['facts']) == check['facts_sha256']
                                    and check['facts']['identity_ref'] == item['identity_ref']
                                    and canonical_json(check['facts']['receipt']) == item['receipt'])
                            else:
                                expected = {key:item[key] for key in ('operation_id','target','receipt','identity_ref')}
                                verified_receipt |= all(original.content.get(k) == v for k,v in expected.items())
                    except (BusinessError,OSError,ValueError):
                        unresolved.append('side_effect_evidence_unavailable:' + item['operation_id'])
                if not verified_receipt:
                    unresolved.append('side_effect_receipt_not_verified:' + item['operation_id'])
            effects.append(SideEffect(operation_id=item['operation_id'],target=safe_metadata(item['target']),
                effect_type=effect_type or 'benchmark_write',status=status,
                receipt=safe_metadata(receipt) if receipt else None,evidence_ids=refs,critical_violation=critical))
        return effects, unresolved, evidence_ids

    @_storage_checked
    def finalize(self, verification_id, *, expected_state_version, execution_token=None):
        with self.evidence.files.locked(), connect(self.database) as db, transaction(db):
            record = db.execute('SELECT * FROM run_verifications WHERE verification_id=?', (verification_id,)).fetchone()
            if record is None:
                raise BusinessError('NOT_FOUND','Verification not found',status=404)
            existing = db.execute('SELECT * FROM run_results WHERE run_id=?', (record['run_id'],)).fetchone()
            if existing:
                if existing['verification_id'] != verification_id or expected_state_version != record['state_version']:
                    raise _conflict()
                return self._decode_result(existing)
            run, contract = self._run(db, record['run_id'], expected_state_version, execution_token)
            from ..controls.models import ControlPending
            from ..controls.store import ControlStore
            pending = ControlStore.pending_in_transaction(db, run['run_id'])
            if pending is not None:
                raise ControlPending(pending)
            if record['state_version'] != expected_state_version or record['contract_sha256'] != run['contract_sha256']:
                raise _conflict()
            if hashlib.sha256(record['content_json'].encode()).hexdigest() != record['content_sha256']:
                raise _conflict('Verification integrity mismatch')
            content = json.loads(record['content_json'])
            proposal = ProposeResult.model_validate_json(canonical_json(content['proposal']))
            bindings = tuple(FieldBinding.model_validate_json(canonical_json(b)) for b in content['bindings'])
            documents, digest = self._documents(db, run['run_id'], content['evidence_ids'])
            if digest != record['evidence_sha256'] or _hash([content['proposal'],content['bindings']]) != record['input_sha256']:
                raise _conflict('Evidence or proposal changed since verification')
            evaluation = evaluate_rules(contract,proposal,documents,bindings,
                                        checked_at=datetime.fromisoformat(content['checked_at'].replace('Z','+00:00')),run_id=run['run_id'])
            if evaluation.model_dump(mode='json') != content['evaluation']:
                raise _conflict('Rule evaluation changed since verification')
            checks = [Check.model_validate_json(canonical_json(c)) for c in content['checks']]
            if {(c.criterion_id,c.expected_rule) for c in checks} != {(c.criterion_id,c.expected_rule) for c in contract.acceptance_criteria}:
                raise _conflict('Verification criterion binding mismatch')
            effects, pending, effect_refs = self._effects(db, run, contract)
            unresolved = set(content['unresolved'] + proposal.unresolved + pending + evaluation.violations)
            unresolved.update('criterion_' + c.verdict.value.lower() + ':' + c.criterion_id for c in checks if c.verdict != Verdict.PASS)
            unresolved.update('field_' + f.verdict.value.lower() + ':' + f.result_path for f in evaluation.fields if f.verdict != Verdict.PASS)
            # A semantic pending marker is resolved only by a corresponding PASS.
            if all(c.verdict == Verdict.PASS for c in checks):
                unresolved.discard('semantic_verification_required')
            unknown_ids = set(proposal.existing_operation_ids) - {e.operation_id for e in effects}
            if unknown_ids:
                unresolved.add('unknown_operation_reference')
            # A current, evidence-backed M1-17 receipt may resolve only an old
            # read attempt. Its original uncertain status stays in history;
            # unknown writes and any newer attempt still block aggregation.
            from ..graph.recovery import RecoveryStore
            if RecoveryStore(self.database).has_unresolved_steps(db, run['run_id']):
                unresolved.add('unresolved_action_attempt')
            budget = self.budgets.flush_in_transaction(db, run['run_id'])
            if budget and budget['exhausted']:
                unresolved.add('budget_exhausted')
            all_fields = bool(evaluation.fields) and all(f.verdict == Verdict.PASS for f in evaluation.fields)
            success = (all(c.verdict == Verdict.PASS for c in checks) and all_fields
                       and bool(proposal.evidence_ids) and not unresolved and not evaluation.violations)
            if success:
                self.evidence.assert_run_ready(run['run_id'], db=db)
                outcome = 'SUCCEEDED'
            elif (evaluation.deliverable_paths and not evaluation.violations and not any(e.critical_violation for e in effects)
                  and all(c.verdict == Verdict.PASS for c in checks if c.criterion_id in evaluation.semantic_criterion_ids)):
                outcome = 'PARTIAL'
            else:
                outcome = 'FAILED'
            result = Result(task_id=run['task_id'],run_id=run['run_id'],contract_version=run['contract_version'],
                scenario=contract.scenario,outcome=outcome,assistance_count=run['assistance_count'],items=proposal.items,
                checks=checks,coverage=proposal.coverage,evidence_ids=sorted(set(proposal.evidence_ids)|effect_refs),
                unresolved=sorted(unresolved),side_effects=effects,generated_by='business_aggregator')
            self._run(db, run['run_id'], expected_state_version, execution_token)
            payload = result.model_dump_json()
            db.execute('INSERT INTO run_results VALUES(?,?,?,?,?,?,?)', (run['run_id'],verification_id,expected_state_version+1,
                       outcome,payload,hashlib.sha256(payload.encode()).hexdigest(),utc_text()))
            self._hook('before_transition')
            self.budgets.on_transition(db, run['run_id'], outcome)
            transition_in_transaction(db,run_id=run['run_id'],expected_state_version=expected_state_version,target=outcome)
            if execution_token is not None:
                self.scheduler._release_safe(db,run['run_id'],preserve_context=False,preserve_logical=False)
                if self.scheduler._unresolved(db,run['run_id']):
                    db.execute("UPDATE resource_leases SET logical_hold=1,control_owner='none',state_version=state_version+1 WHERE holder_run_id=?",(run['run_id'],))
                fresh = self.scheduler._row(db,run['run_id'])
                self.scheduler._change(db,fresh,utc_text(),'finished',reason='finished',status='FINISHED',
                    epoch=execution_token.epoch+1,worker_id=None,worker_generation=None,expires_at=None,
                    run_state_version=expected_state_version+1)
            self._hook('before_result_event')
            append_event(db,run_id=run['run_id'],expected_state_version=expected_state_version+1,
                         payload=ResultEvent(result_ref=verification_id,outcome=outcome))
            return result

    @staticmethod
    def _decode_result(row):
        if hashlib.sha256(row['result_json'].encode()).hexdigest() != row['result_sha256']:
            raise _conflict('Result integrity mismatch')
        return Result.model_validate_json(row['result_json'])

    def read(self, run_id):
        with connect(self.database) as db:
            row = db.execute('SELECT * FROM run_results WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise BusinessError('NOT_FOUND','Run has no aggregated result',status=404)
            result = self._decode_result(row)
            verification = db.execute('SELECT content_json,content_sha256 FROM run_verifications WHERE verification_id=?', (row['verification_id'],)).fetchone()
            if hashlib.sha256(verification['content_json'].encode()).hexdigest() != verification['content_sha256']:
                raise _conflict('Verification integrity mismatch')
            fields = json.loads(verification['content_json'])['evaluation']['fields']
        return dict(result=result.model_dump(mode='json'), field_checks=fields, verification_id=row['verification_id'],
                    assistance='assisted' if result.assistance_count else 'autonomous')
