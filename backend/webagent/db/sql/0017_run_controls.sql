-- M1-18: accepted requests and separately committed control completion.
-- Expand the event enum without changing any historical row, ID or FK target.
-- The migration runner temporarily disables FK enforcement before BEGIN and
-- checks every FK before commit, restoring enforcement in finally. Recreate
-- the original name to retain every referencing trigger and FK target.
CREATE TEMP TABLE m18_event_history AS SELECT * FROM task_events;
CREATE TEMP TABLE m18_event_sequence AS
 SELECT seq FROM sqlite_sequence WHERE name='task_events';
DROP TABLE task_events;
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200),
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200),
    event_type TEXT NOT NULL CHECK(event_type IN ('state_changed','action_recorded','wait_registered','result_ready','operation_requested','operation_completed')),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    occurred_at TEXT NOT NULL CHECK(occurred_at IS NULL OR (length(occurred_at)=27 AND occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(occurred_at,1,23)||'Z') IS substr(occurred_at,1,23)||'Z')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
    UNIQUE(run_id,event_id),
    FOREIGN KEY(task_id,run_id) REFERENCES runs(task_id,run_id)
) STRICT;
INSERT INTO task_events SELECT * FROM m18_event_history ORDER BY event_id;
UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM m18_event_sequence),0)) WHERE name='task_events';
INSERT INTO sqlite_sequence(name,seq)
 SELECT 'task_events',seq FROM m18_event_sequence WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='task_events');
DROP TABLE m18_event_history;
DROP TABLE m18_event_sequence;
CREATE INDEX events_task_sequence ON task_events(task_id,event_id);
CREATE INDEX events_run_sequence ON task_events(run_id,event_id);
CREATE INDEX events_run_state_version ON task_events(run_id,state_version,event_type);
CREATE TRIGGER task_events_no_replace BEFORE INSERT ON task_events
WHEN EXISTS(SELECT 1 FROM task_events WHERE event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT,'existing task_events identity'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT,'historical task_events cannot be deleted'); END;
CREATE TRIGGER task_events_immutable BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT,'immutable task_events'); END;
CREATE TRIGGER state_events_guard BEFORE INSERT ON task_events
WHEN NEW.event_type='state_changed' BEGIN
 SELECT CASE WHEN NEW.state_version<1 OR NOT EXISTS (
   SELECT 1 FROM runs r JOIN run_transitions t
    ON t.previous_state=json_extract(NEW.payload_json,'$.previous_state')
     AND t.current_state=r.state
   WHERE r.run_id=NEW.run_id AND r.task_id=NEW.task_id
     AND r.state_version=NEW.state_version
     AND json_extract(NEW.payload_json,'$.event_type') IS 'state_changed'
     AND json_extract(NEW.payload_json,'$.current_state') IS r.state
     AND json_extract(NEW.payload_json,'$.blocked_reason') IS r.blocked_reason
 ) THEN RAISE(ABORT,'state event does not match run transition') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM task_events WHERE run_id=NEW.run_id
   AND state_version=NEW.state_version AND event_type='state_changed')
   THEN RAISE(ABORT,'duplicate state event') END;
END;

CREATE TABLE run_controls (
    operation_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    parent_run_id TEXT REFERENCES runs(run_id),
    action TEXT NOT NULL CHECK(action IN ('start','retry','pause','resume','cancel')),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPLIED','REJECTED')),
    requested_state_version INTEGER NOT NULL CHECK(requested_state_version>=0),
    accepted_run_state_version INTEGER NOT NULL CHECK(accepted_run_state_version>=0),
    contract_version INTEGER NOT NULL CHECK(contract_version>0),
    settings_version INTEGER NOT NULL CHECK(settings_version>=0),
    request_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    accepted_json TEXT NOT NULL CHECK(json_valid(accepted_json)),
    requested_event_id INTEGER NOT NULL,
    completed_event_id INTEGER,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    reason TEXT,
    completion_worker_id TEXT,
    completion_worker_generation INTEGER,
    completion_epoch INTEGER,
    completion_input_state_version INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(request_scope,idempotency_key),
    FOREIGN KEY(run_id,requested_event_id) REFERENCES task_events(run_id,event_id),
    FOREIGN KEY(run_id,completed_event_id) REFERENCES task_events(run_id,event_id),
    CHECK((status='PENDING' AND completed_event_id IS NULL AND completed_at IS NULL AND result_json IS NULL)
       OR (status<>'PENDING' AND completed_event_id IS NOT NULL AND completed_at IS NOT NULL
           AND result_json IS NOT NULL AND json_type(result_json)='object')),
    CHECK((completion_worker_id IS NULL AND completion_worker_generation IS NULL AND completion_epoch IS NULL AND completion_input_state_version IS NULL)
       OR (completion_worker_id IS NOT NULL AND completion_worker_generation IS NOT NULL AND completion_worker_generation>0
           AND completion_epoch IS NOT NULL AND completion_epoch>0 AND completion_input_state_version IS NOT NULL
           AND completion_input_state_version>=accepted_run_state_version)),
    CHECK(status<>'APPLIED' OR reason IS NULL),
    CHECK(status<>'REJECTED' OR reason IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX run_controls_one_pending ON run_controls(run_id) WHERE status='PENDING';
CREATE INDEX run_controls_pending ON run_controls(status,operation_seq);
CREATE TRIGGER run_controls_binding BEFORE INSERT ON run_controls
WHEN NEW.status<>'PENDING' OR NOT EXISTS(SELECT 1 FROM runs r JOIN task_events e USING(run_id)
 WHERE r.run_id=NEW.run_id AND r.task_id=NEW.task_id AND r.contract_version=NEW.contract_version
 AND NEW.parent_run_id IS r.parent_run_id
 AND r.state_version=NEW.accepted_run_state_version AND e.event_id=NEW.requested_event_id
 AND e.state_version=NEW.accepted_run_state_version AND e.event_type='operation_requested'
 AND json_extract(e.payload_json,'$.operation_id')=NEW.operation_id
 AND json_extract(e.payload_json,'$.action')=NEW.action)
BEGIN SELECT RAISE(ABORT,'control request must bind its committed run/event'); END;
CREATE TRIGGER run_controls_completion BEFORE UPDATE ON run_controls
WHEN OLD.status<>'PENDING' OR NEW.status='PENDING'
 OR NEW.operation_seq IS NOT OLD.operation_seq OR NEW.operation_id IS NOT OLD.operation_id
 OR NEW.task_id IS NOT OLD.task_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.parent_run_id IS NOT OLD.parent_run_id OR NEW.action IS NOT OLD.action
 OR NEW.requested_state_version IS NOT OLD.requested_state_version
 OR NEW.accepted_run_state_version IS NOT OLD.accepted_run_state_version
 OR NEW.contract_version IS NOT OLD.contract_version OR NEW.settings_version IS NOT OLD.settings_version
 OR NEW.request_scope IS NOT OLD.request_scope OR NEW.idempotency_key IS NOT OLD.idempotency_key
 OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.accepted_json IS NOT OLD.accepted_json
 OR NEW.requested_event_id IS NOT OLD.requested_event_id OR NEW.created_at IS NOT OLD.created_at
 OR NOT EXISTS(SELECT 1 FROM task_events e JOIN runs r USING(run_id)
 WHERE e.run_id=NEW.run_id AND e.event_id=NEW.completed_event_id AND e.event_type='operation_completed'
 AND e.state_version=r.state_version AND json_extract(e.payload_json,'$.operation_id')=NEW.operation_id
 AND json_extract(NEW.result_json,'$.run_id') IS r.run_id
 AND json_extract(NEW.result_json,'$.state') IS r.state
 AND json_extract(NEW.result_json,'$.state_version') IS r.state_version
 AND json_extract(e.payload_json,'$.action')=NEW.action
 AND json_extract(e.payload_json,'$.result_ref')=NEW.operation_id
 AND json_extract(e.payload_json,'$.status')=NEW.status)
BEGIN SELECT RAISE(ABORT,'control completion is one immutable terminal receipt'); END;
CREATE TRIGGER run_controls_no_delete BEFORE DELETE ON run_controls
BEGIN SELECT RAISE(ABORT,'control history must remain'); END;
CREATE TRIGGER run_controls_no_replace BEFORE INSERT ON run_controls
WHEN EXISTS(SELECT 1 FROM run_controls WHERE operation_id=NEW.operation_id OR
 (request_scope=NEW.request_scope AND idempotency_key=NEW.idempotency_key))
BEGIN SELECT RAISE(ABORT,'control request identity is immutable'); END;

CREATE TABLE run_retry_operations (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    operation_id TEXT NOT NULL REFERENCES write_intents(operation_id),
    originating_run_id TEXT NOT NULL REFERENCES runs(run_id),
    recorded_status TEXT NOT NULL CHECK(recorded_status IN ('INTENT','CONFIRMED','NOT_APPLIED','UNKNOWN')),
    PRIMARY KEY(run_id,operation_id)
) STRICT;
CREATE TRIGGER run_retry_operations_binding BEFORE INSERT ON run_retry_operations
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN write_intents w USING(task_id)
 WHERE r.run_id=NEW.run_id AND r.parent_run_id IS NOT NULL AND w.operation_id=NEW.operation_id
 AND w.originating_run_id=NEW.originating_run_id AND w.status=NEW.recorded_status)
BEGIN SELECT RAISE(ABORT,'retry must retain real task side effects'); END;
CREATE TRIGGER run_retry_operations_no_update BEFORE UPDATE ON run_retry_operations
BEGIN SELECT RAISE(ABORT,'retry side effect references are immutable'); END;
CREATE TRIGGER run_retry_operations_no_delete BEFORE DELETE ON run_retry_operations
BEGIN SELECT RAISE(ABORT,'retry side effect history must remain'); END;

DROP TRIGGER run_transitions_no_insert;
INSERT INTO run_transitions VALUES ('VERIFYING','PAUSED'),('RECONCILING','PAUSED');
CREATE TRIGGER run_transitions_no_insert BEFORE INSERT ON run_transitions
BEGIN SELECT RAISE(ABORT,'transition matrix is immutable'); END;

CREATE TRIGGER control_events_guard BEFORE INSERT ON task_events
WHEN NEW.event_type IN ('operation_requested','operation_completed') AND NOT EXISTS(
 SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id AND r.task_id=NEW.task_id
 AND r.state_version=NEW.state_version
 AND json_extract(NEW.payload_json,'$.event_type')=NEW.event_type
 AND json_extract(NEW.payload_json,'$.action') IN ('start','retry','pause','resume','cancel')
 AND length(json_extract(NEW.payload_json,'$.operation_id')) BETWEEN 1 AND 200)
BEGIN SELECT RAISE(ABORT,'control event must match current run'); END;
CREATE TRIGGER control_completed_events_guard BEFORE INSERT ON task_events
WHEN NEW.event_type='operation_completed' AND NOT EXISTS(
 SELECT 1 FROM run_controls c WHERE c.operation_id=json_extract(NEW.payload_json,'$.operation_id')
 AND c.run_id=NEW.run_id AND c.action=json_extract(NEW.payload_json,'$.action') AND c.status='PENDING')
BEGIN SELECT RAISE(ABORT,'control completion event must bind one accepted intent'); END;
