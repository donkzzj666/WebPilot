-- M1-16: application-owned, append-only graph progress. Framework saver internals
-- remain in the independent graph DB and cannot become business authority.
CREATE TABLE graph_progress (
    progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
    graph_version TEXT NOT NULL CHECK(graph_version='browser-loop-v1'),
    state_schema_version TEXT NOT NULL CHECK(state_schema_version='browser-loop-state-v1'),
    phase TEXT NOT NULL CHECK(phase IN ('reconcile','observe','decide','dispatch','confirm',
        'verify','aggregate','wait','recover','stopped')),
    business_event_id INTEGER NOT NULL CHECK(business_event_id>0),
    checkpoint_id TEXT,
    snapshot_id TEXT,
    verification_id TEXT,
    wait_id TEXT,
    iteration INTEGER NOT NULL CHECK(iteration>=0),
    diagnostic TEXT CHECK(diagnostic IS NULL OR diagnostic IN ('evidence_required','input_required',
        'verification_incomplete','invalid_model_output','model_failed','page_changed',
        'budget_exceeded','recovery_required','run_finished','configuration_required',
        'identity_recheck_required','write_adapter_unavailable','graph_preparation_failed')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key)=64),
    occurred_at TEXT NOT NULL,
    UNIQUE(run_id,progress_id),
    UNIQUE(run_id,idempotency_key),
    FOREIGN KEY(run_id,business_event_id) REFERENCES task_events(run_id,event_id),
    FOREIGN KEY(run_id,checkpoint_id) REFERENCES run_checkpoints(run_id,checkpoint_id),
    FOREIGN KEY(run_id,snapshot_id) REFERENCES observations(run_id,snapshot_id),
    FOREIGN KEY(run_id,verification_id) REFERENCES run_verifications(run_id,verification_id)
) STRICT;
CREATE INDEX graph_progress_run ON graph_progress(run_id,progress_id);
CREATE TRIGGER graph_progress_no_replace BEFORE INSERT ON graph_progress
WHEN EXISTS(SELECT 1 FROM graph_progress WHERE progress_id=NEW.progress_id)
BEGIN SELECT RAISE(ABORT,'graph progress identity is immutable'); END;
CREATE TRIGGER graph_progress_no_update BEFORE UPDATE ON graph_progress
BEGIN SELECT RAISE(ABORT,'graph progress is immutable'); END;
CREATE TRIGGER graph_progress_no_delete BEFORE DELETE ON graph_progress
BEGIN SELECT RAISE(ABORT,'graph progress history must remain'); END;
CREATE TRIGGER graph_progress_binding BEFORE INSERT ON graph_progress
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN task_events e USING(run_id)
 WHERE r.run_id=NEW.run_id AND r.state_version=NEW.state_version
 AND r.contract_sha256=NEW.contract_sha256 AND r.graph_version=NEW.graph_version
 AND r.graph_state_schema_version=NEW.state_schema_version
 AND e.event_id=NEW.business_event_id AND e.state_version=NEW.state_version)
BEGIN SELECT RAISE(ABORT,'graph progress run/event binding mismatch'); END;

-- Field names survive a RequestInput wait without retaining raw model prose.
CREATE TABLE graph_input_requests (
    wait_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    requested_fields_json TEXT NOT NULL CHECK(json_valid(requested_fields_json)
        AND json_type(requested_fields_json)='array'
        AND json_array_length(requested_fields_json) BETWEEN 1 AND 64
        AND length(requested_fields_json)<=65536),
    created_at TEXT NOT NULL
) STRICT;
CREATE TRIGGER graph_input_request_binding BEFORE INSERT ON graph_input_requests
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN task_events e USING(run_id)
 WHERE r.run_id=NEW.run_id AND r.state='PAUSED' AND r.state_version=NEW.state_version
 AND e.state_version=NEW.state_version AND e.event_type='wait_registered'
 AND json_extract(e.payload_json,'$.wait_id')=NEW.wait_id)
BEGIN SELECT RAISE(ABORT,'input request needs a committed business wait'); END;
CREATE TRIGGER graph_input_request_no_replace BEFORE INSERT ON graph_input_requests
WHEN EXISTS(SELECT 1 FROM graph_input_requests WHERE wait_id=NEW.wait_id)
BEGIN SELECT RAISE(ABORT,'input request is immutable'); END;
CREATE TRIGGER graph_input_request_no_update BEFORE UPDATE ON graph_input_requests
BEGIN SELECT RAISE(ABORT,'input request is immutable'); END;
CREATE TRIGGER graph_input_request_no_delete BEFORE DELETE ON graph_input_requests
BEGIN SELECT RAISE(ABORT,'input request history must remain'); END;
