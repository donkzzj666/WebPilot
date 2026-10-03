-- Immutable publication facts are separate from mutable availability.
CREATE TABLE evidence_artifacts (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 67108864),
    mime_type TEXT NOT NULL CHECK(mime_type IN ('image/png','text/plain; charset=utf-8','application/pdf','application/json')),
    redaction_status TEXT NOT NULL CHECK(redaction_status IN ('BLOCKED','FILTERED')),
    policy_version TEXT,
    snapshot_id TEXT REFERENCES observations(snapshot_id),
    step_id TEXT REFERENCES steps(step_id),
    published_at TEXT NOT NULL,
    CHECK(redaction_status<>'FILTERED' OR policy_version IS NOT NULL)
) STRICT;
CREATE TABLE evidence_availability (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
    status TEXT NOT NULL CHECK(status IN ('AVAILABLE','MISSING','CORRUPT','EXPIRED')),
    checked_at TEXT NOT NULL
) STRICT;
CREATE TABLE evidence_retention (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
    expires_at TEXT,
    keep_until_explicit_cleanup INTEGER NOT NULL CHECK(keep_until_explicit_cleanup IN (0,1)),
    CHECK((keep_until_explicit_cleanup=1)=(expires_at IS NULL))
) STRICT;
CREATE TABLE evidence_events (
    event_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    evidence_id TEXT REFERENCES evidence(evidence_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('published','missing','corrupt','expired','filtered_observation')),
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
) STRICT;
CREATE INDEX evidence_events_run ON evidence_events(run_id,event_id);
CREATE TABLE filtered_observations (
    snapshot_id TEXT PRIMARY KEY REFERENCES observations(snapshot_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    policy_version TEXT NOT NULL,
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,snapshot_id) REFERENCES observations(run_id,snapshot_id)
) STRICT;
CREATE TABLE evidence_orphans (
    artifact_path TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
    status TEXT NOT NULL CHECK(status IN ('REGISTERED','CLEANED'))
) STRICT;
CREATE TABLE evidence_run_guards (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    tracked_at TEXT NOT NULL
) STRICT;
CREATE TABLE evidence_domain (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    faulted INTEGER NOT NULL CHECK(faulted IN (0,1)),
    fault_code TEXT,
    changed_at TEXT NOT NULL
) STRICT;
INSERT INTO evidence_domain VALUES(1,0,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER evidence_artifacts_no_replace BEFORE INSERT ON evidence_artifacts
WHEN EXISTS(SELECT 1 FROM evidence_artifacts WHERE evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'artifact identity is immutable'); END;
CREATE TRIGGER evidence_artifacts_no_update BEFORE UPDATE ON evidence_artifacts
BEGIN SELECT RAISE(ABORT,'artifact facts are immutable'); END;
CREATE TRIGGER evidence_artifacts_no_delete BEFORE DELETE ON evidence_artifacts
BEGIN SELECT RAISE(ABORT,'artifact history must remain'); END;
CREATE TRIGGER evidence_artifacts_binding BEFORE INSERT ON evidence_artifacts
WHEN (NEW.snapshot_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM observations o JOIN evidence e
 ON e.evidence_id=NEW.evidence_id WHERE o.snapshot_id=NEW.snapshot_id AND o.run_id=e.run_id))
 OR (NEW.step_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM steps s JOIN evidence e
 ON e.evidence_id=NEW.evidence_id WHERE s.step_id=NEW.step_id AND s.run_id=e.run_id))
 OR (NEW.redaction_status='FILTERED' AND EXISTS(SELECT 1 FROM evidence WHERE evidence_id=NEW.evidence_id AND sensitivity='restricted'))
BEGIN SELECT RAISE(ABORT,'artifact run or sensitivity binding mismatch'); END;
CREATE TRIGGER evidence_events_no_update BEFORE UPDATE ON evidence_events
BEGIN SELECT RAISE(ABORT,'evidence audit is append only'); END;
CREATE TRIGGER evidence_events_no_delete BEFORE DELETE ON evidence_events
BEGIN SELECT RAISE(ABORT,'evidence audit must remain'); END;
CREATE TRIGGER filtered_observations_no_replace BEFORE INSERT ON filtered_observations
WHEN EXISTS(SELECT 1 FROM filtered_observations WHERE snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'filtered observation already exists'); END;
CREATE TRIGGER filtered_observations_no_update BEFORE UPDATE ON filtered_observations
BEGIN SELECT RAISE(ABORT,'filtered observation is immutable'); END;
CREATE TRIGGER filtered_observations_no_delete BEFORE DELETE ON filtered_observations
BEGIN SELECT RAISE(ABORT,'filtered observation history must remain'); END;
CREATE TRIGGER filtered_base_observations_no_update BEFORE UPDATE ON observations
WHEN EXISTS(SELECT 1 FROM filtered_observations WHERE snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'filtered observation binding is immutable'); END;
CREATE TRIGGER filtered_base_observations_no_delete BEFORE DELETE ON observations
WHEN EXISTS(SELECT 1 FROM filtered_observations WHERE snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'filtered observation binding must remain'); END;
CREATE TRIGGER filtered_base_observations_no_replace BEFORE INSERT ON observations
WHEN EXISTS(SELECT 1 FROM filtered_observations WHERE snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'filtered observation binding already exists'); END;
CREATE TRIGGER evidence_run_guards_no_update BEFORE UPDATE ON evidence_run_guards
BEGIN SELECT RAISE(ABORT,'evidence Run guard is immutable'); END;
CREATE TRIGGER evidence_run_guards_no_delete BEFORE DELETE ON evidence_run_guards
BEGIN SELECT RAISE(ABORT,'evidence Run guard must remain'); END;
CREATE TRIGGER evidence_run_guards_no_replace BEFORE INSERT ON evidence_run_guards
WHEN EXISTS(SELECT 1 FROM evidence_run_guards WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'evidence Run guard already exists'); END;
CREATE TRIGGER evidence_availability_no_delete BEFORE DELETE ON evidence_availability
BEGIN SELECT RAISE(ABORT,'availability history must remain'); END;
CREATE TRIGGER evidence_retention_no_update BEFORE UPDATE ON evidence_retention
BEGIN SELECT RAISE(ABORT,'publication retention is immutable'); END;
CREATE TRIGGER evidence_retention_no_delete BEFORE DELETE ON evidence_retention
BEGIN SELECT RAISE(ABORT,'publication retention must remain'); END;
CREATE TRIGGER evidence_retention_no_replace BEFORE INSERT ON evidence_retention
WHEN EXISTS(SELECT 1 FROM evidence_retention WHERE evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'publication retention already exists'); END;
CREATE TRIGGER evidence_availability_no_replace BEFORE INSERT ON evidence_availability
WHEN EXISTS(SELECT 1 FROM evidence_availability WHERE evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'availability identity must remain'); END;
CREATE TRIGGER evidence_availability_identity BEFORE UPDATE ON evidence_availability
WHEN NEW.evidence_id IS NOT OLD.evidence_id
BEGIN SELECT RAISE(ABORT,'availability identity is immutable'); END;
CREATE TRIGGER evidence_expiry_latched BEFORE UPDATE ON evidence_availability
WHEN OLD.status='EXPIRED' AND NEW.status<>'EXPIRED'
BEGIN SELECT RAISE(ABORT,'expired evidence cannot silently become available'); END;
CREATE TRIGGER evidence_domain_fault_latched BEFORE UPDATE ON evidence_domain
WHEN OLD.faulted=1 AND NEW.faulted<>1
BEGIN SELECT RAISE(ABORT,'evidence storage fault requires explicit operator recovery'); END;
CREATE TRIGGER evidence_domain_no_delete BEFORE DELETE ON evidence_domain
BEGIN SELECT RAISE(ABORT,'evidence domain guard must remain'); END;
