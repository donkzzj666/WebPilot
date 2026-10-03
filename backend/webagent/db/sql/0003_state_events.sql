-- M1-03: the transition matrix is shared by SQL guards and the service.
-- Existing v1/v2 rows remain historical facts; do not fabricate missing events.
CREATE TABLE run_transitions (
    previous_state TEXT NOT NULL,
    current_state TEXT NOT NULL,
    PRIMARY KEY(previous_state,current_state)
) STRICT;
INSERT INTO run_transitions VALUES
 ('QUEUED','RUNNING'), ('QUEUED','CANCELLED'),
 ('RUNNING','VERIFYING'), ('RUNNING','WAITING_CI'),
 ('RUNNING','WAITING_SITE'), ('RUNNING','WAITING_HANDOFF'),
 ('RUNNING','PAUSED'), ('RUNNING','RECONCILING'),
 ('RUNNING','PARTIAL'), ('RUNNING','FAILED'), ('RUNNING','CANCELLED'),
 ('VERIFYING','RUNNING'), ('VERIFYING','WAITING_HANDOFF'),
 ('VERIFYING','RECONCILING'), ('VERIFYING','SUCCEEDED'),
 ('VERIFYING','PARTIAL'), ('VERIFYING','FAILED'), ('VERIFYING','CANCELLED'),
 ('WAITING_CI','RECONCILING'), ('WAITING_CI','PARTIAL'),
 ('WAITING_CI','FAILED'), ('WAITING_CI','CANCELLED'),
 ('WAITING_SITE','RECONCILING'), ('WAITING_SITE','CANCELLED'),
 ('WAITING_HANDOFF','RECONCILING'), ('WAITING_HANDOFF','PARTIAL'),
 ('WAITING_HANDOFF','FAILED'), ('WAITING_HANDOFF','CANCELLED'),
 ('PAUSED','RECONCILING'), ('PAUSED','CANCELLED'),
 ('RECONCILING','RUNNING'), ('RECONCILING','VERIFYING'), ('RECONCILING','CANCELLED');
CREATE TRIGGER run_transitions_no_insert BEFORE INSERT ON run_transitions BEGIN
 SELECT RAISE(ABORT,'transition matrix is immutable');
END;
CREATE TRIGGER run_transitions_no_update BEFORE UPDATE ON run_transitions BEGIN
 SELECT RAISE(ABORT,'transition matrix is immutable');
END;
CREATE TRIGGER run_transitions_no_delete BEFORE DELETE ON run_transitions BEGIN
 SELECT RAISE(ABORT,'transition matrix is immutable');
END;
CREATE TRIGGER runs_initial_state BEFORE INSERT ON runs
WHEN NEW.state<>'QUEUED' OR NEW.state_version<>0 OR NEW.blocked_reason IS NOT NULL BEGIN
 SELECT RAISE(ABORT,'new runs must start QUEUED at version zero');
END;
CREATE TRIGGER runs_transition_guard BEFORE UPDATE ON runs BEGIN
 SELECT CASE WHEN OLD.state IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')
   THEN RAISE(ABORT,'terminal runs are immutable') END;
 SELECT CASE WHEN NEW.state<>OLD.state AND NOT EXISTS (
   SELECT 1 FROM run_transitions WHERE previous_state=OLD.state AND current_state=NEW.state
 ) THEN RAISE(ABORT,'illegal run transition') END;
 SELECT CASE WHEN (NEW.state<>OLD.state AND NEW.state_version<>OLD.state_version+1)
   OR (NEW.state=OLD.state AND NEW.state_version<>OLD.state_version)
   THEN RAISE(ABORT,'state version must increment once per transition') END;
 SELECT CASE WHEN NEW.state=OLD.state AND NEW.blocked_reason IS NOT OLD.blocked_reason
   THEN RAISE(ABORT,'blocked reason changes require a transition') END;
END;
CREATE TRIGGER runs_transition_event AFTER UPDATE OF state ON runs
WHEN NEW.state<>OLD.state BEGIN
 INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json)
 VALUES(NEW.task_id,NEW.run_id,'state_changed',NEW.state_version,
   strftime('%Y-%m-%dT%H:%M:%f','now')||'000Z',
   json_object('event_type','state_changed','previous_state',OLD.state,
     'current_state',NEW.state,'blocked_reason',NEW.blocked_reason));
END;
-- New state events must agree with the current row and cannot duplicate a version.
-- An index supports the guard without imposing uniqueness on legacy v2 history.
CREATE INDEX events_run_state_version ON task_events(run_id,state_version,event_type);
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
