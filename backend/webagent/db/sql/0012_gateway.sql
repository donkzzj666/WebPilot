-- M1-13: append-only observation bindings and dispatch audit; no browser data blobs.
CREATE TABLE gateway_observations (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    session_id TEXT NOT NULL REFERENCES browser_sessions(session_id),
    manager_id TEXT NOT NULL CHECK(length(manager_id) BETWEEN 1 AND 200),
    session_generation INTEGER NOT NULL CHECK(session_generation>0),
    tab_id TEXT NOT NULL CHECK(length(tab_id) BETWEEN 1 AND 200),
    frame_id TEXT NOT NULL CHECK(length(frame_id) BETWEEN 1 AND 200),
    page_version TEXT NOT NULL CHECK(length(page_version) BETWEEN 1 AND 200),
    width INTEGER NOT NULL CHECK(width BETWEEN 1 AND 16384),
    height INTEGER NOT NULL CHECK(height BETWEEN 1 AND 16384),
    screenshot_sha256 TEXT CHECK(screenshot_sha256 IS NULL OR
        (length(screenshot_sha256)=64 AND screenshot_sha256 NOT GLOB '*[^0-9a-f]*')),
    screenshot_evidence_id TEXT CHECK(screenshot_evidence_id IS NULL OR length(screenshot_evidence_id) BETWEEN 1 AND 200),
    capture_sha256 TEXT NOT NULL CHECK(length(capture_sha256)=64 AND capture_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK((screenshot_sha256 IS NULL)=(screenshot_evidence_id IS NULL)),
    UNIQUE(run_id,snapshot_id),
    FOREIGN KEY(run_id,snapshot_id) REFERENCES observations(run_id,snapshot_id)
) STRICT;
CREATE TABLE gateway_page_heads (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    snapshot_id TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
    FOREIGN KEY(run_id,snapshot_id) REFERENCES gateway_observations(run_id,snapshot_id)
) STRICT;
CREATE TABLE gateway_attempts (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    action_kind TEXT NOT NULL CHECK(action_kind IN
        ('navigate','click','input','keypress','select','scroll','switch_tab','read_visible','screenshot','download_attachment')),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    session_id TEXT NOT NULL REFERENCES browser_sessions(session_id),
    manager_id TEXT NOT NULL,
    session_generation INTEGER NOT NULL CHECK(session_generation>0),
    worker_id TEXT NOT NULL,
    worker_generation INTEGER NOT NULL CHECK(worker_generation>0),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    external_write INTEGER NOT NULL CHECK(external_write IN (0,1)),
    operation_id TEXT REFERENCES write_intents(operation_id),
    CHECK((external_write=1)=(operation_id IS NOT NULL)),
    UNIQUE(run_id,step_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,snapshot_id) REFERENCES gateway_observations(run_id,snapshot_id)
) STRICT;
CREATE INDEX gateway_attempts_run ON gateway_attempts(run_id,step_id);
CREATE TRIGGER gateway_observations_no_replace BEFORE INSERT ON gateway_observations
WHEN EXISTS(SELECT 1 FROM gateway_observations WHERE snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'gateway observation already exists'); END;
CREATE TRIGGER gateway_observations_no_update BEFORE UPDATE ON gateway_observations
BEGIN SELECT RAISE(ABORT,'gateway observation is immutable'); END;
CREATE TRIGGER gateway_observations_no_delete BEFORE DELETE ON gateway_observations
BEGIN SELECT RAISE(ABORT,'gateway observation history must remain'); END;
CREATE TRIGGER gateway_observations_binding BEFORE INSERT ON gateway_observations
WHEN NOT EXISTS(SELECT 1 FROM observations o JOIN browser_sessions s ON s.session_id=NEW.session_id
    WHERE o.snapshot_id=NEW.snapshot_id AND o.run_id=NEW.run_id
      AND o.tab_id=NEW.tab_id AND o.frame_id=NEW.frame_id AND o.page_version=NEW.page_version
      AND o.width=NEW.width AND o.height=NEW.height AND s.owner_kind='run'
      AND s.run_id=NEW.run_id AND s.manager_id=NEW.manager_id AND s.generation=NEW.session_generation)
BEGIN SELECT RAISE(ABORT,'gateway observation binding mismatch'); END;
CREATE TRIGGER gateway_base_observations_no_update BEFORE UPDATE ON observations
WHEN EXISTS(SELECT 1 FROM gateway_observations WHERE snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'bound gateway observation is immutable'); END;
CREATE TRIGGER gateway_base_observations_no_delete BEFORE DELETE ON observations
WHEN EXISTS(SELECT 1 FROM gateway_observations WHERE snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'bound gateway observation history must remain'); END;
CREATE TRIGGER gateway_attempts_no_replace BEFORE INSERT ON gateway_attempts
WHEN EXISTS(SELECT 1 FROM gateway_attempts WHERE step_id=NEW.step_id)
BEGIN SELECT RAISE(ABORT,'gateway attempt already exists'); END;
CREATE TRIGGER gateway_attempts_binding BEFORE INSERT ON gateway_attempts
WHEN NOT EXISTS(SELECT 1 FROM steps s JOIN gateway_observations o ON o.snapshot_id=NEW.snapshot_id
    WHERE s.step_id=NEW.step_id AND s.run_id=NEW.run_id AND s.step_kind='atomic_action'
      AND s.status='INTENT' AND s.epoch=NEW.epoch AND s.input_snapshot_id=NEW.snapshot_id
      AND json_extract(s.action_json,'$.action_type')=NEW.action_kind
      AND o.run_id=NEW.run_id AND o.epoch=NEW.epoch AND o.state_version=NEW.state_version
      AND o.session_id=NEW.session_id AND o.manager_id=NEW.manager_id
      AND o.session_generation=NEW.session_generation)
BEGIN SELECT RAISE(ABORT,'gateway action and observation binding mismatch'); END;
CREATE TRIGGER gateway_attempts_no_update BEFORE UPDATE ON gateway_attempts
BEGIN SELECT RAISE(ABORT,'gateway dispatch audit is immutable'); END;
CREATE TRIGGER gateway_attempts_no_delete BEFORE DELETE ON gateway_attempts
BEGIN SELECT RAISE(ABORT,'gateway dispatch audit must remain'); END;
CREATE TRIGGER gateway_steps_binding BEFORE UPDATE ON steps
WHEN EXISTS(SELECT 1 FROM gateway_attempts WHERE step_id=OLD.step_id) AND
    (NEW.step_id IS NOT OLD.step_id OR NEW.run_id IS NOT OLD.run_id OR NEW.sequence IS NOT OLD.sequence
     OR NEW.step_kind IS NOT OLD.step_kind OR NEW.epoch IS NOT OLD.epoch
     OR NEW.input_snapshot_id IS NOT OLD.input_snapshot_id OR NEW.action_json IS NOT OLD.action_json
     OR NEW.started_at IS NOT OLD.started_at OR OLD.status<>'INTENT' OR NEW.status='INTENT')
BEGIN SELECT RAISE(ABORT,'gateway step binding or terminal outcome is immutable'); END;
CREATE TRIGGER gateway_steps_no_delete BEFORE DELETE ON steps
WHEN EXISTS(SELECT 1 FROM gateway_attempts WHERE step_id=OLD.step_id)
BEGIN SELECT RAISE(ABORT,'gateway step history must remain'); END;
CREATE TRIGGER gateway_steps_no_replace BEFORE INSERT ON steps
WHEN EXISTS(SELECT 1 FROM gateway_attempts WHERE step_id=NEW.step_id)
BEGIN SELECT RAISE(ABORT,'gateway step already exists'); END;
