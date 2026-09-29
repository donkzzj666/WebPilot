-- M1-02 / v1: business records. Never edit an applied migration.
CREATE TABLE tasks (
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) PRIMARY KEY,
    original_instruction TEXT NOT NULL CHECK(length(original_instruction)>0),
    preparation_status TEXT NOT NULL DEFAULT 'NEEDS_INPUT' CHECK(preparation_status IN ('NEEDS_INPUT','READY')),
    current_contract_version INTEGER CHECK(current_contract_version>0),
    current_run_id TEXT,
    requested_fields_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(requested_fields_json) AND json_type(requested_fields_json)='array'),
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0),
    CHECK((preparation_status='NEEDS_INPUT' AND json_array_length(requested_fields_json)>0) OR (preparation_status='READY' AND current_contract_version IS NOT NULL AND json_array_length(requested_fields_json)=0)),
    FOREIGN KEY(task_id,current_contract_version) REFERENCES contracts(task_id,contract_version) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(task_id,current_run_id) REFERENCES runs(task_id,run_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE contracts (
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    contract_version INTEGER NOT NULL CHECK(contract_version>0),
    schema_version TEXT NOT NULL CHECK(length(schema_version) BETWEEN 1 AND 200) ,
    scenario TEXT NOT NULL CHECK(scenario IN ('finance','operations','research','monitoring')),
    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64 AND contract_sha256 NOT GLOB '*[^0-9a-f]*'),
    content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object'),
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    PRIMARY KEY(task_id,contract_version),
    UNIQUE(task_id,contract_version,contract_sha256),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    CHECK(json_extract(content_json,'$.task_id') IS task_id),
    CHECK(json_extract(content_json,'$.contract_version') IS contract_version),
    CHECK(json_extract(content_json,'$.schema_version') IS schema_version),
    CHECK(json_extract(content_json,'$.scenario') IS scenario)
) STRICT;
CREATE TABLE runs (
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) PRIMARY KEY,
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    contract_version INTEGER NOT NULL CHECK(contract_version>0),
    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64 AND contract_sha256 NOT GLOB '*[^0-9a-f]*'),
    parent_run_id TEXT CHECK(parent_run_id IS NULL OR parent_run_id<>run_id),
    state TEXT NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','RUNNING','VERIFYING','WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED','RECONCILING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0),
    blocked_reason TEXT,
    thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 200) UNIQUE CHECK(thread_id=run_id),
    graph_version TEXT NOT NULL CHECK(length(graph_version) BETWEEN 1 AND 200) ,
    graph_state_schema_version TEXT NOT NULL CHECK(length(graph_state_schema_version) BETWEEN 1 AND 200) ,
    model_config_sha256 TEXT NOT NULL CHECK(length(model_config_sha256)=64 AND model_config_sha256 NOT GLOB '*[^0-9a-f]*'),
    runtime_config_sha256 TEXT NOT NULL CHECK(length(runtime_config_sha256)=64 AND runtime_config_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    started_at TEXT CHECK(started_at IS NULL OR (length(started_at)=27 AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(started_at,1,23)||'Z') IS substr(started_at,1,23)||'Z')),
    ended_at TEXT CHECK(ended_at IS NULL OR (length(ended_at)=27 AND ended_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(ended_at,1,23)||'Z') IS substr(ended_at,1,23)||'Z')),
    next_eligible_at TEXT CHECK(next_eligible_at IS NULL OR (length(next_eligible_at)=27 AND next_eligible_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(next_eligible_at,1,23)||'Z') IS substr(next_eligible_at,1,23)||'Z')),
    handoff_deadline TEXT CHECK(handoff_deadline IS NULL OR (length(handoff_deadline)=27 AND handoff_deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(handoff_deadline,1,23)||'Z') IS substr(handoff_deadline,1,23)||'Z')),
    assistance_count INTEGER NOT NULL DEFAULT 0 CHECK(assistance_count>=0),
    UNIQUE(task_id,run_id),
    UNIQUE(run_id,task_id,contract_version),
    FOREIGN KEY(task_id,contract_version,contract_sha256) REFERENCES contracts(task_id,contract_version,contract_sha256),
    FOREIGN KEY(task_id,parent_run_id) REFERENCES runs(task_id,run_id),
    CHECK((state IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')) = (ended_at IS NOT NULL)),
    CHECK(state IN ('QUEUED','CANCELLED') OR started_at IS NOT NULL),
    CHECK(ended_at IS NULL OR started_at IS NULL OR ended_at>=started_at),
    CHECK(state<>'WAITING_HANDOFF' OR handoff_deadline IS NOT NULL)
) STRICT;
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    event_type TEXT NOT NULL CHECK(event_type IN ('state_changed','action_recorded','wait_registered','result_ready')),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    occurred_at TEXT NOT NULL CHECK(occurred_at IS NULL OR (length(occurred_at)=27 AND occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(occurred_at,1,23)||'Z') IS substr(occurred_at,1,23)||'Z')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
    UNIQUE(run_id,event_id),
    FOREIGN KEY(task_id,run_id) REFERENCES runs(task_id,run_id)
) STRICT;
CREATE TABLE observations (
    snapshot_id TEXT NOT NULL CHECK(length(snapshot_id) BETWEEN 1 AND 200) PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    captured_at TEXT NOT NULL CHECK(captured_at IS NULL OR (length(captured_at)=27 AND captured_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(captured_at,1,23)||'Z') IS substr(captured_at,1,23)||'Z')),
    source_url TEXT NOT NULL CHECK(length(source_url)>0) ,
    title TEXT NOT NULL,
    tab_id TEXT NOT NULL CHECK(length(tab_id) BETWEEN 1 AND 200) ,
    frame_id TEXT NOT NULL CHECK(length(frame_id) BETWEEN 1 AND 200) ,
    page_version TEXT NOT NULL CHECK(length(page_version) BETWEEN 1 AND 200) ,
    width INTEGER NOT NULL CHECK(width>0),
    height INTEGER NOT NULL CHECK(height>0),
    visible_excerpt TEXT NOT NULL,
    redaction_status TEXT NOT NULL CHECK(redaction_status IN ('FILTERED','BLOCKED')),
    UNIQUE(run_id,snapshot_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
) STRICT;
CREATE TABLE steps (
    step_id TEXT NOT NULL CHECK(length(step_id) BETWEEN 1 AND 200) PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    sequence INTEGER NOT NULL CHECK(sequence>0),
    step_kind TEXT NOT NULL CHECK(step_kind IN ('decision','atomic_action')),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    input_snapshot_id TEXT NOT NULL CHECK(length(input_snapshot_id) BETWEEN 1 AND 200) ,
    action_json TEXT CHECK(action_json IS NULL OR (json_valid(action_json) AND json_type(action_json)='object')),
    actual_result_json TEXT NOT NULL DEFAULT 'null' CHECK(json_valid(actual_result_json)),
    status TEXT NOT NULL DEFAULT 'INTENT' CHECK(status IN ('INTENT','COMPLETED','FAILED','UNKNOWN')),
    error_code TEXT,
    started_at TEXT NOT NULL CHECK(started_at IS NULL OR (length(started_at)=27 AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(started_at,1,23)||'Z') IS substr(started_at,1,23)||'Z')),
    ended_at TEXT CHECK(ended_at IS NULL OR (length(ended_at)=27 AND ended_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(ended_at,1,23)||'Z') IS substr(ended_at,1,23)||'Z')),
    UNIQUE(run_id,sequence),
    UNIQUE(run_id,step_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id),
    FOREIGN KEY(run_id,input_snapshot_id) REFERENCES observations(run_id,snapshot_id),
    CHECK((step_kind='atomic_action')=(action_json IS NOT NULL)),
    CHECK((status='INTENT')=(ended_at IS NULL)),
    CHECK(ended_at IS NULL OR ended_at>=started_at)
) STRICT;
CREATE TABLE evidence (
    evidence_id TEXT NOT NULL CHECK(length(evidence_id) BETWEEN 1 AND 200) PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    source_url TEXT NOT NULL CHECK(length(source_url)>0) ,
    captured_at TEXT NOT NULL CHECK(captured_at IS NULL OR (length(captured_at)=27 AND captured_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(captured_at,1,23)||'Z') IS substr(captured_at,1,23)||'Z')),
    object_id TEXT NOT NULL CHECK(length(object_id) BETWEEN 1 AND 200) ,
    query_scope TEXT NOT NULL,
    artifact_path TEXT NOT NULL CHECK(length(artifact_path)>0) UNIQUE,
    sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    locator_or_page TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public','restricted','redacted')),
    capture_status TEXT NOT NULL CHECK(capture_status IN ('COMPLETE','INCOMPLETE','CORRUPT','MISSING')),
    artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('screenshot','text','pdf','ci','har','diff')),
    original_evidence_id TEXT CHECK(original_evidence_id IS NULL OR original_evidence_id<>evidence_id),
    commit_sha TEXT CHECK(commit_sha IS NULL OR (length(commit_sha)=40 AND commit_sha NOT GLOB '*[^0-9a-f]*')),
    test_run_id TEXT,
    UNIQUE(run_id,evidence_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id),
    FOREIGN KEY(run_id,original_evidence_id) REFERENCES evidence(run_id,evidence_id),
    CHECK(artifact_kind<>'ci' OR (commit_sha IS NOT NULL AND test_run_id IS NOT NULL)),
    CHECK(sensitivity<>'redacted' OR original_evidence_id IS NOT NULL),
    CHECK(artifact_path NOT LIKE '/%' AND instr(artifact_path,char(92))=0 AND instr('/'||artifact_path||'/','/../')=0 AND instr('/'||artifact_path||'/','/./')=0 AND instr(artifact_path,'//')=0 AND artifact_path NOT LIKE '%/')
) STRICT;
CREATE TABLE write_intents (
    operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 200) PRIMARY KEY,
    business_key TEXT NOT NULL CHECK(length(business_key) BETWEEN 1 AND 200) UNIQUE,
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    originating_run_id TEXT NOT NULL CHECK(length(originating_run_id) BETWEEN 1 AND 200) ,
    target TEXT NOT NULL,
    expected_change TEXT NOT NULL,
    identity_ref TEXT NOT NULL CHECK(length(identity_ref) BETWEEN 1 AND 200) ,
    precondition_version TEXT NOT NULL CHECK(length(precondition_version) BETWEEN 1 AND 200) ,
    status TEXT NOT NULL DEFAULT 'INTENT' CHECK(status IN ('INTENT','CONFIRMED','NOT_APPLIED','UNKNOWN')),
    receipt TEXT,
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    updated_at TEXT NOT NULL CHECK(updated_at IS NULL OR (length(updated_at)=27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(updated_at,1,23)||'Z') IS substr(updated_at,1,23)||'Z')),
    UNIQUE(task_id,operation_id),
    FOREIGN KEY(task_id,originating_run_id) REFERENCES runs(task_id,run_id),
    CHECK(status<>'CONFIRMED' OR receipt IS NOT NULL)
) STRICT;
CREATE TABLE run_checkpoints (
    checkpoint_id TEXT NOT NULL CHECK(length(checkpoint_id) BETWEEN 1 AND 200) PRIMARY KEY,
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    contract_version INTEGER NOT NULL CHECK(contract_version>0),
    current_subgoal TEXT NOT NULL CHECK(length(current_subgoal) BETWEEN 1 AND 200) ,
    verified_item_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(verified_item_ids_json) AND json_type(verified_item_ids_json)='array'),
    pending_item_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(pending_item_ids_json) AND json_type(pending_item_ids_json)='array'),
    current_object_id TEXT NOT NULL CHECK(length(current_object_id) BETWEEN 1 AND 200) ,
    current_object_version TEXT,
    current_snapshot_id TEXT,
    flow_version TEXT,
    action_sequence INTEGER NOT NULL CHECK(action_sequence>=0),
    business_event_id INTEGER CHECK(business_event_id>0),
    budget_record_ref TEXT NOT NULL CHECK(length(budget_record_ref) BETWEEN 1 AND 200) ,
    identity_ref TEXT,
    epoch INTEGER NOT NULL CHECK(epoch>0),
    saved_at TEXT NOT NULL CHECK(saved_at IS NULL OR (length(saved_at)=27 AND saved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(saved_at,1,23)||'Z') IS substr(saved_at,1,23)||'Z')),
    UNIQUE(run_id,checkpoint_id),
    FOREIGN KEY(run_id,task_id,contract_version) REFERENCES runs(run_id,task_id,contract_version),
    FOREIGN KEY(run_id,current_snapshot_id) REFERENCES observations(run_id,snapshot_id),
    FOREIGN KEY(run_id,business_event_id) REFERENCES task_events(run_id,event_id),
    FOREIGN KEY(run_id,budget_record_ref) REFERENCES run_budgets(run_id,budget_record_id)
) STRICT;
CREATE TABLE observations_evidence (
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    snapshot_id TEXT NOT NULL CHECK(length(snapshot_id) BETWEEN 1 AND 200) ,
    evidence_id TEXT NOT NULL CHECK(length(evidence_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(snapshot_id,evidence_id),
    FOREIGN KEY(run_id,snapshot_id) REFERENCES observations(run_id,snapshot_id),
    FOREIGN KEY(run_id,evidence_id) REFERENCES evidence(run_id,evidence_id)
) STRICT;
CREATE TABLE steps_evidence (
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    step_id TEXT NOT NULL CHECK(length(step_id) BETWEEN 1 AND 200) ,
    evidence_id TEXT NOT NULL CHECK(length(evidence_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(step_id,evidence_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,evidence_id) REFERENCES evidence(run_id,evidence_id)
) STRICT;
CREATE TABLE run_checkpoints_evidence (
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    checkpoint_id TEXT NOT NULL CHECK(length(checkpoint_id) BETWEEN 1 AND 200) ,
    evidence_id TEXT NOT NULL CHECK(length(evidence_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(checkpoint_id,evidence_id),
    FOREIGN KEY(run_id,checkpoint_id) REFERENCES run_checkpoints(run_id,checkpoint_id),
    FOREIGN KEY(run_id,evidence_id) REFERENCES evidence(run_id,evidence_id)
) STRICT;
CREATE TABLE checkpoint_operations (
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    checkpoint_id TEXT NOT NULL CHECK(length(checkpoint_id) BETWEEN 1 AND 200) ,
    operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(checkpoint_id,operation_id),
    FOREIGN KEY(task_id,run_id) REFERENCES runs(task_id,run_id),
    FOREIGN KEY(run_id,checkpoint_id) REFERENCES run_checkpoints(run_id,checkpoint_id),
    FOREIGN KEY(task_id,operation_id) REFERENCES write_intents(task_id,operation_id)
) STRICT;
CREATE TABLE write_intent_evidence (
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 200) ,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    evidence_id TEXT NOT NULL CHECK(length(evidence_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(operation_id,evidence_id),
    FOREIGN KEY(task_id,operation_id) REFERENCES write_intents(task_id,operation_id),
    FOREIGN KEY(task_id,run_id) REFERENCES runs(task_id,run_id),
    FOREIGN KEY(run_id,evidence_id) REFERENCES evidence(run_id,evidence_id)
) STRICT;
CREATE TABLE write_intent_attempts (
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 200) ,
    operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 200) ,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) ,
    step_id TEXT NOT NULL CHECK(length(step_id) BETWEEN 1 AND 200) ,
    PRIMARY KEY(operation_id,step_id),
    FOREIGN KEY(task_id,operation_id) REFERENCES write_intents(task_id,operation_id),
    FOREIGN KEY(task_id,run_id) REFERENCES runs(task_id,run_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id)
) STRICT;
CREATE TRIGGER contracts_no_replace BEFORE INSERT ON contracts
WHEN EXISTS(SELECT 1 FROM contracts WHERE task_id=NEW.task_id AND contract_version=NEW.contract_version)
BEGIN SELECT RAISE(ABORT, 'existing contracts identity'); END;
CREATE TRIGGER contracts_no_delete BEFORE DELETE ON contracts
BEGIN SELECT RAISE(ABORT, 'historical contracts cannot be deleted'); END;
CREATE TRIGGER contracts_immutable BEFORE UPDATE ON contracts
BEGIN SELECT RAISE(ABORT, 'immutable contracts'); END;
CREATE TRIGGER runs_no_replace BEFORE INSERT ON runs
WHEN EXISTS(SELECT 1 FROM runs WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'existing runs identity'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'historical runs cannot be deleted'); END;
CREATE TRIGGER task_events_no_replace BEFORE INSERT ON task_events
WHEN EXISTS(SELECT 1 FROM task_events WHERE event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'existing task_events identity'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'historical task_events cannot be deleted'); END;
CREATE TRIGGER task_events_immutable BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'immutable task_events'); END;
CREATE TRIGGER evidence_no_replace BEFORE INSERT ON evidence
WHEN EXISTS(SELECT 1 FROM evidence WHERE evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT, 'existing evidence identity'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'historical evidence cannot be deleted'); END;
CREATE TRIGGER evidence_immutable BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END;
CREATE TRIGGER run_checkpoints_no_replace BEFORE INSERT ON run_checkpoints
WHEN EXISTS(SELECT 1 FROM run_checkpoints WHERE checkpoint_id=NEW.checkpoint_id)
BEGIN SELECT RAISE(ABORT, 'existing run_checkpoints identity'); END;
CREATE TRIGGER run_checkpoints_no_delete BEFORE DELETE ON run_checkpoints
BEGIN SELECT RAISE(ABORT, 'historical run_checkpoints cannot be deleted'); END;
CREATE TRIGGER run_checkpoints_immutable BEFORE UPDATE ON run_checkpoints
BEGIN SELECT RAISE(ABORT, 'immutable run_checkpoints'); END;
CREATE TRIGGER runs_fixed_binding BEFORE UPDATE ON runs
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.task_id IS NOT OLD.task_id
 OR NEW.contract_version IS NOT OLD.contract_version OR NEW.contract_sha256 IS NOT OLD.contract_sha256
 OR NEW.parent_run_id IS NOT OLD.parent_run_id OR NEW.thread_id IS NOT OLD.thread_id
 OR NEW.graph_version IS NOT OLD.graph_version OR NEW.graph_state_schema_version IS NOT OLD.graph_state_schema_version
 OR NEW.model_config_sha256 IS NOT OLD.model_config_sha256 OR NEW.runtime_config_sha256 IS NOT OLD.runtime_config_sha256
 OR NEW.created_at IS NOT OLD.created_at OR (OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at)
BEGIN SELECT RAISE(ABORT, 'immutable run binding'); END;
CREATE TRIGGER runs_terminal_immutable BEFORE UPDATE ON runs
WHEN OLD.state IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')
BEGIN SELECT RAISE(ABORT, 'terminal run is immutable; create a new run'); END;
CREATE INDEX runs_due ON runs(state,next_eligible_at);
CREATE INDEX runs_task_history ON runs(task_id,created_at,run_id);
CREATE INDEX events_task_sequence ON task_events(task_id,event_id);
CREATE INDEX events_run_sequence ON task_events(run_id,event_id);
CREATE INDEX steps_run_sequence ON steps(run_id,sequence);
CREATE INDEX evidence_run_capture ON evidence(run_id,captured_at);
CREATE INDEX checkpoints_run_time ON run_checkpoints(run_id,saved_at);
CREATE INDEX intents_pending ON write_intents(task_id,status);
CREATE TRIGGER tasks_fixed_identity BEFORE UPDATE ON tasks
WHEN NEW.task_id IS NOT OLD.task_id OR NEW.original_instruction IS NOT OLD.original_instruction
 OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.current_contract_version IS NOT NULL AND (NEW.current_contract_version IS NULL OR NEW.current_contract_version<OLD.current_contract_version))
BEGIN SELECT RAISE(ABORT, 'task identity and contract version cannot regress'); END;
CREATE TRIGGER evidence_path_no_replace BEFORE INSERT ON evidence
WHEN EXISTS(SELECT 1 FROM evidence WHERE artifact_path=NEW.artifact_path)
BEGIN SELECT RAISE(ABORT, 'artifact path already referenced'); END;
CREATE TRIGGER write_intents_no_replace BEFORE INSERT ON write_intents
WHEN EXISTS(SELECT 1 FROM write_intents WHERE operation_id=NEW.operation_id OR business_key=NEW.business_key)
BEGIN SELECT RAISE(ABORT, 'existing business operation'); END;
CREATE TRIGGER write_intents_fixed_binding BEFORE UPDATE ON write_intents
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.business_key IS NOT OLD.business_key
 OR NEW.task_id IS NOT OLD.task_id OR NEW.originating_run_id IS NOT OLD.originating_run_id
 OR NEW.target IS NOT OLD.target OR NEW.expected_change IS NOT OLD.expected_change
 OR NEW.identity_ref IS NOT OLD.identity_ref OR NEW.precondition_version IS NOT OLD.precondition_version
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'immutable write intent binding'); END;
CREATE TRIGGER write_intents_no_delete BEFORE DELETE ON write_intents
BEGIN SELECT RAISE(ABORT, 'write intent must remain available for reconciliation'); END;
