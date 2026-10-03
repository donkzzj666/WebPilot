-- M1-17: immutable reconciliation receipts. Never replace business authority.
CREATE TABLE graph_recoveries (
    recovery_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
    business_event_id INTEGER NOT NULL CHECK(business_event_id>0),
    phase TEXT NOT NULL CHECK(phase IN ('BEGIN','BLOCKED','COMPLETE')),
    reason TEXT CHECK(reason IS NULL OR reason IN ('graph_state_invalid','graph_version_mismatch',
      'contract_mismatch','graph_ahead','event_missing','event_version_mismatch','checkpoint_mismatch',
      'snapshot_missing','progress_mismatch','summary_mismatch','evidence_missing','evidence_corrupt',
      'unknown_write','human_control','object_mismatch','object_version_mismatch','identity_mismatch',
      'budget_exhausted','recovery_not_completed','session_unavailable','source_scope_mismatch','proof_missing')),
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json) AND length(facts_json)<=1048576),
    facts_sha256 TEXT NOT NULL CHECK(length(facts_sha256)=64),
    created_at TEXT NOT NULL,
    UNIQUE(run_id,recovery_id),
    UNIQUE(run_id,epoch,phase,input_sha256),
    FOREIGN KEY(run_id,business_event_id) REFERENCES task_events(run_id,event_id),
    CHECK((phase='BLOCKED')=(reason IS NOT NULL))
) STRICT;
CREATE INDEX graph_recoveries_run ON graph_recoveries(run_id,recovery_seq);
CREATE TRIGGER graph_recovery_binding BEFORE INSERT ON graph_recoveries
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN task_events e USING(run_id) JOIN scheduler_queue q USING(run_id)
 WHERE r.run_id=NEW.run_id AND r.state='RECONCILING' AND r.state_version=NEW.state_version
 AND r.contract_sha256=NEW.contract_sha256 AND e.event_id=NEW.business_event_id
 AND e.state_version=NEW.state_version AND q.status='ACTIVE' AND q.epoch=NEW.epoch
 AND q.run_state_version=NEW.state_version)
BEGIN SELECT RAISE(ABORT,'recovery receipt needs current business qualification'); END;
CREATE TRIGGER graph_recovery_no_replace BEFORE INSERT ON graph_recoveries
WHEN EXISTS(SELECT 1 FROM graph_recoveries WHERE recovery_id=NEW.recovery_id)
BEGIN SELECT RAISE(ABORT,'recovery receipt identity is immutable'); END;
CREATE TRIGGER graph_recovery_no_update BEFORE UPDATE ON graph_recoveries
BEGIN SELECT RAISE(ABORT,'recovery receipt is immutable'); END;
CREATE TRIGGER graph_recovery_no_delete BEFORE DELETE ON graph_recoveries
BEGIN SELECT RAISE(ABORT,'recovery history must remain'); END;

CREATE TABLE graph_recovery_navigations (
    navigation_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    recovery_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch>0),
    attempt_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('INTENT','COMPLETED','FAILED','UNKNOWN')),
    created_at TEXT NOT NULL,
    UNIQUE(run_id,attempt_id,status),
    FOREIGN KEY(run_id,recovery_id) REFERENCES graph_recoveries(run_id,recovery_id),
    FOREIGN KEY(run_id,attempt_id) REFERENCES budget_attempts(run_id,attempt_id)
) STRICT;
CREATE TRIGGER recovery_navigation_binding BEFORE INSERT ON graph_recovery_navigations
WHEN NOT EXISTS(SELECT 1 FROM graph_recoveries r JOIN budget_attempts b USING(run_id)
 WHERE r.recovery_id=NEW.recovery_id AND r.run_id=NEW.run_id AND r.phase='BEGIN'
 AND r.epoch=NEW.epoch AND b.attempt_id=NEW.attempt_id AND b.epoch=NEW.epoch AND b.kind='action')
 OR (NEW.status<>'INTENT' AND NOT EXISTS(SELECT 1 FROM graph_recovery_navigations n
 WHERE n.run_id=NEW.run_id AND n.attempt_id=NEW.attempt_id AND n.status='INTENT'
 AND n.recovery_id=NEW.recovery_id AND n.source_url=NEW.source_url))
 OR EXISTS(SELECT 1 FROM graph_recovery_navigations n WHERE n.run_id=NEW.run_id
 AND n.attempt_id=NEW.attempt_id AND n.status<>'INTENT')
BEGIN SELECT RAISE(ABORT,'recovery navigation requires charged immutable intent'); END;
CREATE TRIGGER recovery_navigation_no_replace BEFORE INSERT ON graph_recovery_navigations
WHEN EXISTS(SELECT 1 FROM graph_recovery_navigations WHERE navigation_seq=NEW.navigation_seq)
BEGIN SELECT RAISE(ABORT,'recovery navigation identity is immutable'); END;
CREATE TRIGGER recovery_navigation_no_update BEFORE UPDATE ON graph_recovery_navigations
BEGIN SELECT RAISE(ABORT,'recovery navigation is immutable'); END;
CREATE TRIGGER recovery_navigation_no_delete BEFORE DELETE ON graph_recovery_navigations
BEGIN SELECT RAISE(ABORT,'recovery navigation history must remain'); END;
