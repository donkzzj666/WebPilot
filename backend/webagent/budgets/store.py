"""Persistent budget authority; all reservations precede external dispatch.

The claim/lifecycle hooks compose with the scheduler's transaction. Normal
intervals use the monotonic clock. Recovery conservatively includes the last
unclosed interval, so restarting never returns consumed time. No method here
changes a Run state, releases a lease, sleeps, or performs external I/O.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..scheduler.models import ExecutionToken, canonical_site, resource_site
from .clock import SystemClock
from .models import BudgetProfile, CONSUMPTION_KINDS, LIMIT_FIELDS, MONITOR_SOURCES, ObstacleType

SHANGHAI = ZoneInfo('Asia/Shanghai')
NS_PER_MS = 1_000_000


def _invalid(field):
    return BusinessError('INVALID_PARAMETER', 'Invalid budget metadata', field=field)


def _id(value, field):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _invalid(field)
    return value


def _dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _deny(reason):
    return BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted: ' + reason, status=409, field=reason)


class BudgetStore:
    def __init__(self, path: Path, *, clock=None):
        self.path, self.clock = Path(path), clock or SystemClock()

    def _stamp(self):
        now, ns, domain = self.clock.utcnow(), self.clock.monotonic_ns(), self.clock.domain
        if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or type(ns) is not int or not 0 <= ns < 2**63 or type(domain) is not str
                or not 1 <= len(domain) <= 200):
            raise ValueError('Clock must provide aware UTC time, nonnegative nanoseconds and a clock domain')
        return now.astimezone(timezone.utc), ns, domain

    @staticmethod
    def _existing(db, run_id):
        return db.execute('''SELECT b.*,t.mode,t.anchor_utc,t.anchor_mono_ns,t.clock_domain,
           t.active_remainder_ns,t.ci_remainder_ns,t.handoff_deadline,t.handoff_started_utc,
           t.handoff_started_mono_ns,t.handoff_clock_domain,t.last_ci_poll_utc,
           t.last_ci_poll_mono_ns,t.last_ci_poll_domain,t.stop_reason,t.stopped_at
           FROM run_budgets b JOIN budget_timers t USING(run_id) WHERE b.run_id=?''', (run_id,)).fetchone()

    @staticmethod
    def _limits(db, run_id):
        row = db.execute('SELECT * FROM budget_limits WHERE run_id=?', (run_id,)).fetchone()
        return None if row is None else {key: row[key] for key in LIMIT_FIELDS}

    @staticmethod
    def _contract(db, run_id):
        run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        contract = json.loads(db.execute('SELECT content_json FROM contracts WHERE task_id=? AND contract_version=?',
                                        (run['task_id'],run['contract_version'])).fetchone()[0])
        try:
            limits = BudgetProfile.model_validate(contract.get('budget_profile', {})).model_dump()
        except ValidationError:
            raise _invalid('budget_profile') from None
        if any(type(value) is not int or value>=2**63 for value in limits.values()):
            raise _invalid('budget_profile')
        return run, contract, limits

    @staticmethod
    def _elapsed(row, limits, stamp, *, recover=False):
        """Milliseconds plus sub-millisecond carry, charged once per anchor.

        A changed boot domain cannot compare monotonic values. UTC rollback in
        that case spends the remaining interval budget rather than gifting time.
        """
        now, ns, domain = stamp
        mode = row['mode']
        if mode == 'inactive':
            return 0, 0
        used_key, limit_key = ('active_ms','max_active_seconds') if mode == 'active' else ('ci_wait_ms','max_ci_wait_seconds')
        remainder = row['active_remainder_ns' if mode == 'active' else 'ci_remainder_ns']
        wall_ns = int((now - _dt(row['anchor_utc'])).total_seconds() * 1_000_000_000)
        compatible = domain == row['clock_domain'] and ns >= row['anchor_mono_ns']
        mono_ns = ns - row['anchor_mono_ns'] if compatible else 0
        if compatible and not recover:
            elapsed_ns = mono_ns
        elif wall_ns < 0 and not compatible:
            elapsed_ns = max(0,limits[limit_key] * 1000 - row[used_key]) * NS_PER_MS
        else:
            elapsed_ns = max(0,wall_ns,mono_ns)
        return divmod(elapsed_ns + remainder, NS_PER_MS)

    @staticmethod
    def _reason(row, limits, stamp, elapsed=0):
        if row['stop_reason'] is not None:
            return row['stop_reason']
        if row['mode'] == 'active' and row['active_ms'] + elapsed >= limits['max_active_seconds'] * 1000:
            return 'active_time'
        if row['mode'] == 'ci' and row['ci_wait_ms'] + elapsed >= limits['max_ci_wait_seconds'] * 1000:
            return 'ci_wait'
        # The first handoff deadline remains meaningful after repeated resume.
        if row['handoff_deadline'] is not None:
            now,ns,domain = stamp
            duration_ns = int((_dt(row['handoff_deadline'])-_dt(row['handoff_started_utc'])).total_seconds()*1_000_000_000)
            elapsed_ns = ns-row['handoff_started_mono_ns'] if domain==row['handoff_clock_domain'] and ns>=row['handoff_started_mono_ns'] else None
            if (utc_text(now)>=row['handoff_deadline'] or elapsed_ns is not None and elapsed_ns>=duration_ns
                    or elapsed_ns is None and now<_dt(row['handoff_started_utc'])):
                return 'handoff'
        return None

    def _settle(self, db, run_id, *, recover=False, stamp=None):
        row = self._existing(db, run_id)
        if row is None:
            return None
        limits, stamp = self._limits(db,run_id), stamp or self._stamp()
        elapsed, remainder = self._elapsed(row,limits,stamp,recover=recover)
        now, ns, domain = stamp
        if row['mode'] != 'inactive':
            usage = 'active_ms' if row['mode'] == 'active' else 'ci_wait_ms'
            carry = 'active_remainder_ns' if row['mode'] == 'active' else 'ci_remainder_ns'
            db.execute(f'UPDATE run_budgets SET {usage}={usage}+?,last_persisted_at=?,heartbeat_at=?,state_version=state_version+1 WHERE run_id=?',
                       (elapsed,utc_text(now),utc_text(now),run_id))
            db.execute(f'UPDATE budget_timers SET {carry}=? WHERE run_id=?', (remainder,run_id))
        db.execute('UPDATE budget_timers SET anchor_utc=?,anchor_mono_ns=?,clock_domain=? WHERE run_id=?',
                   (utc_text(now),ns,domain,run_id))
        fresh = self._existing(db,run_id)
        reason = self._reason(fresh,limits,stamp)
        if reason is not None and fresh['stop_reason'] is None:
            db.execute('UPDATE budget_timers SET stop_reason=?,stopped_at=? WHERE run_id=?', (reason,utc_text(now),run_id))
        return self._status(db,run_id,stamp=stamp,project=False)

    def before_claim(self, db, row):
        """Check everything before writing; one Run debit in the claim transaction.

        The caller must roll back the complete transaction if anything after this
        hook fails. A rejected candidate does not leave a partial budget/debit.
        """
        if not db.in_transaction:
            raise ValueError('Claim budgets require the scheduler transaction')
        run_id = _id(row['run_id'],'run_id')
        stamp = self._stamp(); now,ns,domain = stamp
        run,contract,limits = self._contract(db,run_id)
        existing = self._existing(db,run_id)
        if existing is not None:
            limits = self._limits(db,run_id)
            elapsed,_ = self._elapsed(existing,limits,stamp,recover=True)
            reason = self._reason(existing,limits,stamp,elapsed)
            if reason is not None:
                raise _deny(reason)
            if existing['active_ms'] + (elapsed if existing['mode']=='active' else 0) >= limits['max_active_seconds'] * 1000:
                raise _deny('active_time')
        legacy_budget = db.execute('SELECT * FROM run_budgets WHERE run_id=?',(run_id,)).fetchone()
        legacy_elapsed = 0
        if legacy_budget is not None:
            if existing is None and legacy_budget['active_interval_started_at'] is not None:
                wall_ms = int((now-_dt(legacy_budget['active_interval_started_at'])).total_seconds()*1000)
                legacy_elapsed = wall_ms if wall_ms>=0 else max(0,limits['max_active_seconds']*1000-legacy_budget['active_ms'])
            if legacy_budget['active_ms'] + legacy_elapsed >= limits['max_active_seconds'] * 1000:
                raise _deny('active_time')
        debit = db.execute('SELECT * FROM quota_debits WHERE run_id=?',(run_id,)).fetchone()
        source = contract.get('parameters',{}).get('source_kind')
        monitoring = (row['queue_class']=='monitoring' and run['parent_run_id'] is None
                      and contract.get('scenario')=='monitoring' and type(source) is str and source in MONITOR_SOURCES)
        kind = 'monitoring' if monitoring else 'ordinary'
        date = now.astimezone(SHANGHAI).date().isoformat()
        public = row['queue_class'] != 'webarena'
        if public and debit is None:
            bucket = db.execute("SELECT * FROM quota_buckets WHERE quota_date=? AND quota_type='public'",(date,)).fetchone()
            recorded = db.execute("SELECT count(*) FROM quota_debits WHERE quota_date=? AND quota_type='public'",(date,)).fetchone()[0]
            used = max(recorded,bucket['used'] if bucket else 0)
            ordinary = db.execute("SELECT count(*) FROM quota_debits WHERE quota_date=? AND quota_type='public' AND debit_kind='ordinary'",(date,)).fetchone()[0]
            reserved = db.execute('''SELECT count(*) FROM quota_debits d JOIN quota_monitor_sources s USING(debit_id)
                 WHERE d.quota_date=? AND d.debit_kind='monitoring' AND s.source_kind=?''',(date,source if monitoring else '')).fetchone()[0]
            monitor_total = recorded - ordinary
            monitor_known = db.execute('''SELECT count(*) FROM quota_debits d JOIN quota_monitor_sources s USING(debit_id)
                 WHERE d.quota_date=? AND d.quota_type='public' AND d.debit_kind='monitoring' ''',(date,)).fetchone()[0]
            monitor_unknown = max(0,monitor_total-monitor_known)
            capacity = min(50,bucket['capacity']) if bucket else 50
            # Older monitoring debits lack source provenance. Conservatively
            # charge those unknown slots against each source's ceiling: they
            # cannot be treated as fresh capacity in either reserved source.
            if used >= capacity or (not monitoring and ordinary >= 42) or (monitoring and (monitor_total>=8 or reserved+monitor_unknown>=4)):
                raise BusinessError('DAILY_QUOTA_EXCEEDED','Public daily Run quota exhausted',status=409)
        # No checks after this point can partially accept a denied candidate.
        if self._limits(db,run_id) is None:
            db.execute('INSERT INTO budget_limits(run_id,'+','.join(LIMIT_FIELDS)+',created_at) VALUES ('+','.join('?' for _ in range(len(LIMIT_FIELDS)+2))+')',
                       (run_id,*[limits[field] for field in LIMIT_FIELDS],utc_text(now)))
        if legacy_budget is None:
            db.execute('INSERT INTO run_budgets(budget_record_id,run_id,last_persisted_at) VALUES(?,?,?)',
                       ('budget-'+hashlib.sha256(run_id.encode()).hexdigest(),run_id,utc_text(now)))
        if public and debit is None:
            db.execute("INSERT INTO quota_buckets(quota_date,quota_type) VALUES(?,'public') ON CONFLICT DO NOTHING",(date,))
            debit_id = 'debit-'+hashlib.sha256(run_id.encode()).hexdigest()
            db.execute("INSERT INTO quota_debits(debit_id,run_id,quota_date,quota_type,debit_kind,debited_at) VALUES(?,?,?,'public',?,?)",
                       (debit_id,run_id,date,kind,utc_text(now)))
            if monitoring:
                db.execute('INSERT INTO quota_monitor_sources VALUES(?,?)',(debit_id,source))
            db.execute("UPDATE quota_buckets SET used=used+1,state_version=state_version+1 WHERE quota_date=? AND quota_type='public'",(date,))
            db.execute('UPDATE run_budgets SET quota_debit_id=? WHERE run_id=?',(debit_id,run_id))
        elif debit is not None and (legacy_budget is None or legacy_budget['quota_debit_id'] is None):
            db.execute('UPDATE run_budgets SET quota_debit_id=? WHERE run_id=?',(debit['debit_id'],run_id))
        if existing is None:
            db.execute('INSERT INTO budget_timers(run_id,anchor_utc,anchor_mono_ns,clock_domain) VALUES(?,?,?,?)',
                       (run_id,utc_text(now),ns,domain))
        # The v11 timer owns future intervals. Import an older unclosed wall
        # interval once, conservatively; an already exhausted legacy Run was
        # rejected above and requires explicit recovery handling, never a gift
        # of fresh execution. The old cumulative ledger is otherwise untouched.
        db.execute('UPDATE run_budgets SET active_ms=active_ms+?,active_interval_started_at=NULL WHERE run_id=?',
                   (legacy_elapsed,run_id))

    def on_claim(self, db, token):
        """Resume the same counters; acquiring a slot begins an active interval."""
        return self.on_transition(db,token.run_id,'RUNNING')

    def on_transition(self, db, run_id, target, *, recover=False):
        """Settle the old interval before switching modes; site cooldown stays active."""
        if not db.in_transaction:
            raise ValueError('Budget lifecycle requires the scheduler transaction')
        result = self._settle(db,run_id,recover=recover)
        if result is None:
            return None  # Historical, never-scheduled Runs have no v11 timer.
        mode = 'active' if target in ('RUNNING','VERIFYING','WAITING_SITE') else 'ci' if target=='WAITING_CI' else 'inactive'
        if target=='WAITING_HANDOFF':
            self.handoff_deadline(db,run_id)
        db.execute('UPDATE budget_timers SET mode=? WHERE run_id=?',(mode,run_id))
        return self._status(db,run_id,project=False)

    def handoff_deadline(self, db, run_id, requested=None):
        if not db.in_transaction:
            raise ValueError('Handoff deadline requires the scheduler transaction')
        row = self._existing(db,run_id)
        if row is None:
            raise BusinessError('STATE_CONFLICT','Run budget must be initialized before handoff',status=409)
        now,ns,domain = self._stamp()
        deadline = now + timedelta(seconds=self._limits(db,run_id)['max_handoff_seconds'])
        if requested is not None:
            if not isinstance(requested,datetime) or requested.tzinfo is None or requested.utcoffset() is None:
                raise _invalid('handoff_deadline')
            deadline = min(deadline,requested.astimezone(timezone.utc))
        if row['handoff_deadline'] is not None:
            deadline = min(deadline,_dt(row['handoff_deadline']))
        if row['handoff_deadline'] is None:
            db.execute('UPDATE budget_timers SET handoff_deadline=?,handoff_started_utc=?,handoff_started_mono_ns=?,handoff_clock_domain=? WHERE run_id=?',
                       (utc_text(deadline),utc_text(now),ns,domain,run_id))
        else:
            db.execute('UPDATE budget_timers SET handoff_deadline=? WHERE run_id=?',(utc_text(deadline),run_id))
        return deadline

    def flush_in_transaction(self, db, run_id, *, recover=False):
        """Trusted heartbeat/model hook; the caller owns its fresh qualification check."""
        if not db.in_transaction:
            raise ValueError('Budget flush requires an explicit transaction')
        return self._settle(db,_id(run_id,'run_id'),recover=recover)

    def _status(self, db, run_id, *, stamp=None, project=True):
        row = self._existing(db,run_id)
        if row is None:
            if not db.execute('SELECT 1 FROM runs WHERE run_id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','Run not found',status=404)
            return {'run_id':run_id,'initialized':False,'exhausted':False,'reason':None}
        limits,stamp = self._limits(db,run_id),stamp or self._stamp()
        elapsed,_ = self._elapsed(row,limits,stamp) if project else (0,0)
        active = row['active_ms'] + (elapsed if row['mode']=='active' else 0)
        ci = row['ci_wait_ms'] + (elapsed if row['mode']=='ci' else 0)
        reason = self._reason(row,limits,stamp,elapsed)
        recovery = json.loads(row['recovery_counts_json'])
        result = {key:row[key] for key in ('actions_used','content_pages_used','observations_used','screenshots_used','model_calls_used')}
        debit = db.execute('''SELECT d.quota_date,d.debit_kind,s.source_kind FROM quota_debits d
           LEFT JOIN quota_monitor_sources s USING(debit_id) WHERE d.run_id=?''',(run_id,)).fetchone()
        return {**result,'run_id':run_id,'initialized':True,'active_ms':active,'ci_wait_ms':ci,
                'mode':row['mode'],'handoff_deadline':row['handoff_deadline'], 'limits':limits,
                'recovery_counts':recovery,'exhausted':reason is not None,'reason':reason,
                'remaining_actions':max(0,limits['max_actions']-row['actions_used']),
                'remaining_content_pages':max(0,limits['max_content_pages']-row['content_pages_used']),
                'remaining_active_ms':max(0,limits['max_active_seconds']*1000-active),
                'remaining_ci_wait_ms':max(0,limits['max_ci_wait_seconds']*1000-ci),
                'limit_exhausted':{'actions':row['actions_used']>=limits['max_actions'],
                                   'content_pages':row['content_pages_used']>=limits['max_content_pages'],
                                   'recovery':any(value>=limits['max_recoveries_per_obstacle'] for value in recovery.values())},
                'quota':dict(debit) if debit else None}

    def status(self, run_id):
        with connect(self.path) as db:
            db.execute('BEGIN')  # One coherent WAL read snapshot, without a write lock.
            result = self._status(db,_id(run_id,'run_id'))
            db.execute('COMMIT')
            return result

    def flush(self, token):
        from ..scheduler.store import validate_in_transaction
        with connect(self.path) as db,transaction(db):
            validate_in_transaction(db,token,now=self._stamp()[0],allow_reconciling=True)
            result = self._settle(db,token.run_id)
            if result is None:
                raise BusinessError('STATE_CONFLICT','Run budget is not initialized',status=409)
            return result

    def stop_in_transaction(self, db, run_id, reason='site_wait_exceeds_budget'):
        """A trusted site wait may exceed the remaining time before the clock does."""
        if not db.in_transaction or reason not in ('active_time','ci_wait','handoff','site_wait_exceeds_budget','action_limit','content_page_limit','recovery_limit'):
            raise ValueError('A bounded stop reason and explicit transaction are required')
        result = self._settle(db,run_id)
        if result is None:
            raise BusinessError('STATE_CONFLICT','Run budget is not initialized',status=409)
        if result['reason'] is None:
            db.execute('UPDATE budget_timers SET stop_reason=?,stopped_at=? WHERE run_id=?',
                       (reason,utc_text(self._stamp()[0]),run_id))
        return self._status(db,run_id,project=False)

    def sweep_due(self):
        """Persist time stops even when the Run is waiting outside an executor."""
        due = []
        with connect(self.path) as db,transaction(db):
            stamp = self._stamp()
            for row in db.execute('''SELECT t.run_id FROM budget_timers t JOIN runs r USING(run_id)
                WHERE r.state NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED') ORDER BY t.run_id''').fetchall():
                status = self._settle(db,row['run_id'],stamp=stamp)
                if status['exhausted']:
                    due.append({'run_id':row['run_id'],'reason':status['reason']})
        return due

    @staticmethod
    def _paced(last_utc, last_ns, last_domain, stamp, seconds):
        if last_utc is None:
            return False
        now,ns,domain = stamp
        elapsed = (ns-last_ns)/1_000_000_000 if domain==last_domain and ns>=last_ns else (now-_dt(last_utc)).total_seconds()
        return elapsed < seconds

    def _site(self, token, site_id):
        scopes = {resource_site(key) for key in token.resources if key.startswith('site_identity:')}
        if type(site_id) is not str:
            raise _invalid('site_id')
        if ':' in site_id:
            realm,separator,site = site_id.partition(':')
            if realm not in ('public','webarena') or not separator:
                raise _invalid('site_id')
            scope = realm,canonical_site(site)
        else:
            site = canonical_site(site_id)
            possible = [scope for scope in scopes if scope[1]==site]
            if len(possible)!=1:
                raise BusinessError('RESOURCE_CONFLICT','Site is outside the execution qualification',status=409)
            scope = possible[0]
        if scope not in scopes:
            raise BusinessError('RESOURCE_CONFLICT','Site is outside the execution qualification',status=409)
        return ':'.join(scope)

    def consume(self, token, *, kind, attempt_id, site_id=None, subgoal=None,
                obstacle_type=None, content_page=False, navigation=False, allow_reconciling=False,
                allow_write_check=False):
        with connect(self.path) as db, transaction(db):
            result, error = self.consume_in_transaction(db, token, kind=kind,
                attempt_id=attempt_id, site_id=site_id, subgoal=subgoal,
                obstacle_type=obstacle_type, content_page=content_page, navigation=navigation,
                allow_reconciling=allow_reconciling, allow_write_check=allow_write_check)
        if error is not None:
            raise error  # A denied dispatch still persists elapsed time/stops.
        return result

    def consume_in_transaction(self, db, token, *, kind, attempt_id, site_id=None, subgoal=None,
                obstacle_type=None, content_page=False, navigation=False, allow_reconciling=False,
                allow_write_check=False):
        """Reserve one immutable dispatch attempt; failures never refund it.

        Duplicate calls return the already charged attempt after checking fresh
        authority and exact metadata. Browser reads/screenshots report separate
        counters. A recovery dispatch and a CI poll each also spend one action.
        """
        if not db.in_transaction:
            raise ValueError('Consumption requires an explicit transaction')
        from ..scheduler.store import validate_in_transaction
        if type(allow_reconciling) is not bool or type(allow_write_check) is not bool:
            raise _invalid('allow_reconciling')
        # This exception only journals a GET under the private write-check
        # surface. It never grants an ordinary browser context or mutation.
        if allow_write_check and (allow_reconciling or kind != 'action'
                                  or not content_page or not navigation):
            raise _invalid('allow_write_check')
        if type(kind) is not str or kind not in CONSUMPTION_KINDS or type(content_page) is not bool or type(navigation) is not bool:
            raise _invalid('kind')
        _id(attempt_id,'attempt_id')
        if not isinstance(token,ExecutionToken):
            raise BusinessError('RESOURCE_CONFLICT','Execution qualification is stale or unavailable',status=409)
        if (content_page or navigation) and kind not in ('action','recovery'):
            raise _invalid('content_page' if content_page else 'navigation')
        if kind=='recovery':
            _id(subgoal,'subgoal')
            try:
                obstacle_type = ObstacleType(obstacle_type).value
            except (ValueError,TypeError):
                raise _invalid('obstacle_type') from None
        elif subgoal is not None or obstacle_type is not None:
            raise _invalid('obstacle_type')
        actions = int(kind in ('action','recovery','ci_poll'))
        site = self._site(token,site_id) if site_id is not None else None
        if (navigation or kind=='recovery') and site is None:
            raise _invalid('site_id')
        recovery_key = hashlib.sha256(canonical_json([site,subgoal,obstacle_type]).encode()).hexdigest() if kind=='recovery' else None
        payload = hashlib.sha256(canonical_json([kind,site,recovery_key,content_page,navigation]).encode()).hexdigest()
        error = None
        stamp = self._stamp()
        qualified = validate_in_transaction(db,token,now=stamp[0],
            allow_reconciling=allow_reconciling or allow_write_check or kind in ('observation','screenshot'))
        if allow_reconciling and (qualified['state'] != 'RECONCILING' or kind not in ('action','recovery')):
            raise _invalid('allow_reconciling')
        status = self._settle(db,token.run_id,stamp=stamp)
        if status is None:
            raise BusinessError('STATE_CONFLICT','Run budget is not initialized',status=409)
        duplicate = db.execute('SELECT * FROM budget_attempts WHERE run_id=? AND attempt_id=?',(token.run_id,attempt_id)).fetchone()
        if duplicate and duplicate['payload_sha256']!=payload:
            raise BusinessError('STATE_CONFLICT','Attempt identifier is bound to another dispatch',status=409)
        if status['exhausted']:
            error = _deny(status['reason'])
        elif duplicate:
            return {**status,'charged':False,'attempt_id':attempt_id,'duplicate':True,'dispatch_allowed':False}, None
        else:
            # A control request and a new dispatch admission serialize on the
            # same SQLite writer lock. Existing attempts can still settle.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_controls'").fetchone():
                from ..controls.models import ControlPending
                from ..controls.store import ControlStore
                pending = ControlStore.pending_in_transaction(db, token.run_id)
                if pending is not None:
                    raise ControlPending(pending)
            row,limits = self._existing(db,token.run_id),status['limits']
            recovery = dict(status['recovery_counts'])
            if actions and status['actions_used']>=limits['max_actions']:
                error = _deny('action_limit')
            elif content_page and status['content_pages_used']>=limits['max_content_pages']:
                error = _deny('content_page_limit')
            elif recovery_key and recovery.get(recovery_key,0)>=limits['max_recoveries_per_obstacle']:
                error = _deny('recovery_limit')
            gates = db.execute('SELECT * FROM site_gates WHERE site_id IN (?,?)',(site,site.partition(':')[2])).fetchall() if site else []
            if error is None and any(gate['state']=='BLOCKED' or gate['next_eligible_at'] is not None and gate['next_eligible_at']>utc_text(stamp[0]) for gate in gates):
                error = BusinessError('SITE_THROTTLED','Logical site is blocked or cooling down',status=409)
            pacing = db.execute('SELECT * FROM site_pacing WHERE site_id=?',(site,)).fetchone() if navigation else None
            if error is None and pacing and self._paced(pacing['last_utc'],pacing['last_mono_ns'],pacing['clock_domain'],stamp,max(limits['min_site_interval_seconds'],pacing['interval_seconds'])):
                error = BusinessError('SITE_THROTTLED','Logical site navigation interval has not elapsed',status=409)
            if error is None and kind=='ci_poll' and self._paced(row['last_ci_poll_utc'],row['last_ci_poll_mono_ns'],row['last_ci_poll_domain'],stamp,limits['min_ci_poll_seconds']):
                error = BusinessError('SITE_THROTTLED','CI polling interval has not elapsed',status=409)
            if error is None:
                if recovery_key:
                    recovery[recovery_key] = recovery.get(recovery_key,0)+1
                db.execute('''INSERT INTO budget_attempts VALUES(?,?,?,?,?,?,?,?,?)''',
                           (token.run_id,attempt_id,kind,payload,token.epoch,actions,int(content_page),recovery_key,utc_text(stamp[0])))
                db.execute('''UPDATE run_budgets SET actions_used=actions_used+?,content_pages_used=content_pages_used+?,
                   observations_used=observations_used+?,screenshots_used=screenshots_used+?,recovery_counts_json=?,
                   last_persisted_at=?,state_version=state_version+1 WHERE run_id=?''',
                           (actions,int(content_page),int(kind in ('observation','ci_poll')),int(kind=='screenshot'),
                            canonical_json(recovery),utc_text(stamp[0]),token.run_id))
                if navigation:
                    db.execute('''INSERT INTO site_pacing VALUES(?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET
                      last_utc=excluded.last_utc,last_mono_ns=excluded.last_mono_ns,clock_domain=excluded.clock_domain,
                      interval_seconds=MAX(site_pacing.interval_seconds,excluded.interval_seconds)''',
                               (site,utc_text(stamp[0]),stamp[1],stamp[2],limits['min_site_interval_seconds']))
                if kind=='ci_poll':
                    db.execute('UPDATE budget_timers SET last_ci_poll_utc=?,last_ci_poll_mono_ns=?,last_ci_poll_domain=? WHERE run_id=?',
                               (utc_text(stamp[0]),stamp[1],stamp[2],token.run_id))
                status = self._status(db,token.run_id,stamp=stamp,project=False)
            if error is not None and error.code=='BUDGET_EXCEEDED' and error.field in ('action_limit','content_page_limit','recovery_limit'):
                self.stop_in_transaction(db,token.run_id,error.field)
        if error is not None:
            return status, error
        return {**status,'charged':True,'attempt_id':attempt_id,'duplicate':False,'dispatch_allowed':True}, None
