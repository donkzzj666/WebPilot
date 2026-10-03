-- M1-23: immutable semantics, individual dispatches and append-only page proofs.
-- Existing intent/step outcomes remain historical; reconciliation never edits steps.
CREATE TABLE write_protocol_claims (
    operation_id TEXT PRIMARY KEY REFERENCES write_intents(operation_id),
    business_key TEXT NOT NULL UNIQUE,
    target_json TEXT NOT NULL CHECK(json_valid(target_json) AND json_type(target_json)='object'),
    expected_change_sha256 TEXT NOT NULL CHECK(length(expected_change_sha256)=64 AND expected_change_sha256 NOT GLOB '*[^0-9a-f]*'),
    adapter_id TEXT NOT NULL CHECK(length(adapter_id) BETWEEN 1 AND 200),
    claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64 AND claim_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    FOREIGN KEY(operation_id,business_key) REFERENCES write_intents(operation_id,business_key)
) STRICT;
CREATE UNIQUE INDEX write_intent_operation_business ON write_intents(operation_id,business_key);
CREATE TABLE write_protocol_links (
    operation_id TEXT NOT NULL REFERENCES write_protocol_claims(operation_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    linked_at TEXT NOT NULL,
    PRIMARY KEY(operation_id,run_id)
) STRICT;
CREATE TRIGGER write_protocol_link_task BEFORE INSERT ON write_protocol_links
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN write_intents w USING(task_id)
    WHERE r.run_id=NEW.run_id AND w.operation_id=NEW.operation_id)
BEGIN SELECT RAISE(ABORT,'write protocol Run belongs to another task'); END;
CREATE TABLE write_protocol_checks (
    check_id TEXT PRIMARY KEY CHECK(length(check_id) BETWEEN 1 AND 200),
    operation_id TEXT NOT NULL REFERENCES write_protocol_claims(operation_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    worker_id TEXT NOT NULL,
    worker_generation INTEGER NOT NULL CHECK(worker_generation>0),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    run_state TEXT NOT NULL CHECK(run_state IN ('RUNNING','VERIFYING','RECONCILING')),
    snapshot_id TEXT NOT NULL,
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json) AND json_type(facts_json)='object'),
    facts_sha256 TEXT NOT NULL CHECK(length(facts_sha256)=64 AND facts_sha256 NOT GLOB '*[^0-9a-f]*'),
    effective_status TEXT NOT NULL CHECK(effective_status IN ('CONFIRMED','NOT_APPLIED','UNKNOWN')),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 200),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,snapshot_id) REFERENCES observations(run_id,snapshot_id),
    FOREIGN KEY(operation_id,run_id) REFERENCES write_protocol_links(operation_id,run_id),
    UNIQUE(operation_id,check_id)
) STRICT;
CREATE INDEX write_protocol_checks_operation ON write_protocol_checks(operation_id,created_at,check_id);
CREATE TABLE write_protocol_check_evidence (
    check_id TEXT NOT NULL REFERENCES write_protocol_checks(check_id),
    run_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(check_id,evidence_id),
    FOREIGN KEY(run_id,evidence_id) REFERENCES evidence(run_id,evidence_id)
) STRICT;
CREATE TABLE write_protocol_dispatches (
    step_id TEXT PRIMARY KEY REFERENCES gateway_attempts(step_id),
    operation_id TEXT NOT NULL REFERENCES write_protocol_claims(operation_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    worker_id TEXT NOT NULL,
    worker_generation INTEGER NOT NULL CHECK(worker_generation>0),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    check_id TEXT UNIQUE REFERENCES write_protocol_checks(check_id),
    dispatched_at TEXT NOT NULL,
    FOREIGN KEY(operation_id,run_id) REFERENCES write_protocol_links(operation_id,run_id),
    UNIQUE(operation_id,step_id)
) STRICT;
CREATE TRIGGER write_protocol_dispatch_binding BEFORE INSERT ON write_protocol_dispatches
WHEN NOT EXISTS(SELECT 1 FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
    WHERE g.step_id=NEW.step_id AND g.operation_id=NEW.operation_id AND g.run_id=NEW.run_id
    AND g.external_write=1 AND g.worker_id=NEW.worker_id AND g.worker_generation=NEW.worker_generation
    AND g.epoch=NEW.epoch AND g.state_version=NEW.state_version AND s.status='INTENT')
BEGIN SELECT RAISE(ABORT,'write protocol dispatch binding mismatch'); END;
CREATE TRIGGER write_protocol_retry_proof BEFORE INSERT ON write_protocol_dispatches
WHEN NEW.check_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM write_protocol_checks c
    WHERE c.check_id=NEW.check_id AND c.operation_id=NEW.operation_id AND c.run_id=NEW.run_id
    AND c.worker_id=NEW.worker_id AND c.worker_generation=NEW.worker_generation
    AND c.epoch=NEW.epoch AND (c.state_version=NEW.state_version OR
        (c.run_state='RECONCILING' AND c.state_version+1=NEW.state_version
         AND EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id AND r.state='RUNNING')))
    AND c.effective_status='NOT_APPLIED')
BEGIN SELECT RAISE(ABORT,'write protocol retry proof mismatch'); END;
CREATE TRIGGER write_protocol_claims_no_replace BEFORE INSERT ON write_protocol_claims
WHEN EXISTS(SELECT 1 FROM write_protocol_claims WHERE operation_id=NEW.operation_id OR business_key=NEW.business_key)
BEGIN SELECT RAISE(ABORT,'write protocol semantics already exist'); END;
CREATE TRIGGER write_protocol_claims_no_update BEFORE UPDATE ON write_protocol_claims
BEGIN SELECT RAISE(ABORT,'write protocol semantics are immutable'); END;
CREATE TRIGGER write_protocol_claims_no_delete BEFORE DELETE ON write_protocol_claims
BEGIN SELECT RAISE(ABORT,'write protocol semantics must remain'); END;
CREATE TRIGGER write_protocol_links_no_update BEFORE UPDATE ON write_protocol_links
BEGIN SELECT RAISE(ABORT,'write protocol links are immutable'); END;
CREATE TRIGGER write_protocol_links_no_replace BEFORE INSERT ON write_protocol_links
WHEN EXISTS(SELECT 1 FROM write_protocol_links WHERE operation_id=NEW.operation_id AND run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'write protocol link already exists'); END;
CREATE TRIGGER write_protocol_links_no_delete BEFORE DELETE ON write_protocol_links
BEGIN SELECT RAISE(ABORT,'write protocol links must remain'); END;
CREATE TRIGGER write_protocol_checks_no_replace BEFORE INSERT ON write_protocol_checks
WHEN EXISTS(SELECT 1 FROM write_protocol_checks WHERE check_id=NEW.check_id)
BEGIN SELECT RAISE(ABORT,'write protocol check already exists'); END;
CREATE TRIGGER write_protocol_checks_no_update BEFORE UPDATE ON write_protocol_checks
BEGIN SELECT RAISE(ABORT,'write protocol checks are immutable'); END;
CREATE TRIGGER write_protocol_checks_no_delete BEFORE DELETE ON write_protocol_checks
BEGIN SELECT RAISE(ABORT,'write protocol check history must remain'); END;
CREATE TRIGGER write_protocol_check_evidence_no_update BEFORE UPDATE ON write_protocol_check_evidence
BEGIN SELECT RAISE(ABORT,'write protocol proof references are immutable'); END;
CREATE TRIGGER write_protocol_check_evidence_no_replace BEFORE INSERT ON write_protocol_check_evidence
WHEN EXISTS(SELECT 1 FROM write_protocol_check_evidence WHERE check_id=NEW.check_id AND evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'write protocol proof reference already exists'); END;
CREATE TRIGGER write_protocol_check_evidence_no_delete BEFORE DELETE ON write_protocol_check_evidence
BEGIN SELECT RAISE(ABORT,'write protocol proof references must remain'); END;
CREATE TRIGGER write_protocol_dispatches_no_replace BEFORE INSERT ON write_protocol_dispatches
WHEN EXISTS(SELECT 1 FROM write_protocol_dispatches WHERE step_id=NEW.step_id)
BEGIN SELECT RAISE(ABORT,'write protocol dispatch already exists'); END;
CREATE TRIGGER write_protocol_dispatches_no_update BEFORE UPDATE ON write_protocol_dispatches
BEGIN SELECT RAISE(ABORT,'write protocol dispatches are immutable'); END;
CREATE TRIGGER write_protocol_dispatches_no_delete BEFORE DELETE ON write_protocol_dispatches
BEGIN SELECT RAISE(ABORT,'write protocol dispatch history must remain'); END;
CREATE TRIGGER write_protocol_confirmed_immutable BEFORE UPDATE ON write_intents
WHEN EXISTS(SELECT 1 FROM write_protocol_claims WHERE operation_id=OLD.operation_id)
 AND OLD.status='CONFIRMED' AND (NEW.status IS NOT OLD.status OR NEW.receipt IS NOT OLD.receipt)
BEGIN SELECT RAISE(ABORT,'confirmed external write cannot be revoked'); END;
CREATE TRIGGER write_protocol_status_proof BEFORE UPDATE ON write_intents
WHEN EXISTS(SELECT 1 FROM write_protocol_claims WHERE operation_id=OLD.operation_id)
 AND NEW.status IN ('CONFIRMED','NOT_APPLIED') AND NEW.status<>OLD.status
 AND NOT EXISTS(SELECT 1 FROM write_protocol_checks c WHERE c.operation_id=OLD.operation_id
     AND c.effective_status=NEW.status AND c.created_at=NEW.updated_at)
BEGIN SELECT RAISE(ABORT,'write protocol resolution requires proof'); END;
CREATE TABLE write_reconciliation_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    source_run_id TEXT NOT NULL REFERENCES runs(run_id),
    created_at TEXT NOT NULL,
    CHECK(run_id<>source_run_id)
) STRICT;
CREATE TRIGGER write_reconciliation_run_binding BEFORE INSERT ON write_reconciliation_runs
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN runs s ON s.run_id=NEW.source_run_id
    JOIN contracts c ON c.task_id=r.task_id AND c.contract_version=r.contract_version
    JOIN contracts old ON old.task_id=s.task_id AND old.contract_version=s.contract_version
    WHERE r.run_id=NEW.run_id AND r.task_id=s.task_id AND r.contract_sha256=s.contract_sha256
    AND s.state IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')
    AND json_extract(c.content_json,'$.identity_ref') IS json_extract(old.content_json,'$.identity_ref'))
BEGIN SELECT RAISE(ABORT,'write reconciliation Run binding mismatch'); END;
CREATE TRIGGER write_reconciliation_runs_no_update BEFORE UPDATE ON write_reconciliation_runs
BEGIN SELECT RAISE(ABORT,'write reconciliation Run is immutable'); END;
CREATE TRIGGER write_reconciliation_runs_no_replace BEFORE INSERT ON write_reconciliation_runs
WHEN EXISTS(SELECT 1 FROM write_reconciliation_runs WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'write reconciliation Run already exists'); END;
CREATE TRIGGER write_reconciliation_runs_no_delete BEFORE DELETE ON write_reconciliation_runs
BEGIN SELECT RAISE(ABORT,'write reconciliation Run history must remain'); END;
DROP TRIGGER run_transitions_no_insert;
INSERT INTO run_transitions VALUES('QUEUED','RECONCILING');
CREATE TRIGGER run_transitions_no_insert BEFORE INSERT ON run_transitions
BEGIN SELECT RAISE(ABORT,'transition matrix is immutable'); END;
CREATE TRIGGER write_reconciliation_transition_guard BEFORE UPDATE ON runs
WHEN OLD.state='QUEUED' AND NEW.state='RECONCILING'
 AND NOT EXISTS(SELECT 1 FROM write_reconciliation_runs WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'new Run requires explicit write reconciliation binding'); END;
CREATE TRIGGER write_protocol_success_guard BEFORE UPDATE ON runs
WHEN NEW.state='SUCCEEDED' AND EXISTS(SELECT 1 FROM write_intents w
 JOIN write_protocol_claims c USING(operation_id) WHERE w.task_id=NEW.task_id AND w.status IN ('INTENT','UNKNOWN'))
BEGIN SELECT RAISE(ABORT,'unresolved external write blocks task success'); END;
CREATE TRIGGER write_protocol_attempt_history_no_update BEFORE UPDATE ON write_intent_attempts
WHEN EXISTS(SELECT 1 FROM write_protocol_claims WHERE operation_id=OLD.operation_id OR operation_id=NEW.operation_id)
BEGIN SELECT RAISE(ABORT,'write protocol attempt references are immutable'); END;
CREATE TRIGGER write_protocol_attempt_history_no_delete BEFORE DELETE ON write_intent_attempts
WHEN EXISTS(SELECT 1 FROM write_protocol_claims WHERE operation_id=OLD.operation_id)
BEGIN SELECT RAISE(ABORT,'write protocol attempt references must remain'); END;
