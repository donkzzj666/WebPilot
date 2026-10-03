-- M1-10: durable scheduling. Existing resource / quarantine ledgers remain authoritative.
CREATE TABLE scheduler_workers (
    worker_id TEXT PRIMARY KEY CHECK(length(worker_id) BETWEEN 1 AND 200),
    generation INTEGER NOT NULL UNIQUE CHECK(generation>0),
    state TEXT NOT NULL CHECK(state IN ('ACTIVE','STOPPED')),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK(expires_at>=heartbeat_at)
) STRICT;
CREATE TABLE scheduler_generations (
    generation INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE TABLE scheduler_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    queue_class TEXT NOT NULL CHECK(queue_class IN ('ordinary','monitoring','webarena')),
    status TEXT NOT NULL CHECK(status IN ('QUEUED','ACTIVE','WAITING','RECOVERY','FINISHED')),
    epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
    worker_id TEXT,
    worker_generation INTEGER,
    run_state_version INTEGER NOT NULL CHECK(run_state_version>=0),
    available_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT CHECK(reason IN ('resource_conflict','context_capacity','worker_restarted','lease_expired',
       'scope_expanded','waiting','cancelled','finished','reconciled','executor_interrupted') OR reason IS NULL),
    CHECK((status='ACTIVE')=(worker_id IS NOT NULL AND worker_generation IS NOT NULL AND expires_at IS NOT NULL))
) STRICT;
CREATE INDEX scheduler_queue_due ON scheduler_queue(status,available_at,queue_class,queue_id);
CREATE TABLE scheduler_requirements (
    run_id TEXT NOT NULL REFERENCES scheduler_queue(run_id),
    resource_key TEXT NOT NULL CHECK(length(resource_key) BETWEEN 1 AND 200),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('site_identity','repository_write','webarena_environment','browser_context')),
    logical_hold INTEGER NOT NULL DEFAULT 0 CHECK(logical_hold IN (0,1)),
    PRIMARY KEY(run_id,resource_key)
) STRICT;
CREATE UNIQUE INDEX browser_sessions_run_session ON browser_sessions(run_id,session_id);
CREATE TABLE scheduler_context_reservations (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    context_ordinal INTEGER NOT NULL CHECK(context_ordinal BETWEEN 1 AND 4),
    worker_id TEXT NOT NULL,
    worker_generation INTEGER NOT NULL CHECK(worker_generation>0),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    session_id TEXT UNIQUE REFERENCES browser_sessions(session_id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,session_id) REFERENCES browser_sessions(run_id,session_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX scheduler_context_unmaterialized ON scheduler_context_reservations(session_id);
CREATE TABLE scheduler_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    epoch INTEGER NOT NULL CHECK(epoch>=0),
    revision INTEGER NOT NULL CHECK(revision>=0),
    event_type TEXT NOT NULL CHECK(event_type IN ('enqueued','claimed','heartbeat','revoked','waiting',
       'resumed','expanded','reconciled','finished')),
    reason TEXT,
    occurred_at TEXT NOT NULL
) STRICT;
CREATE INDEX scheduler_events_run ON scheduler_events(run_id,event_id);
CREATE TRIGGER scheduler_events_no_update BEFORE UPDATE ON scheduler_events BEGIN
 SELECT RAISE(ABORT,'scheduler events are immutable'); END;
CREATE TRIGGER scheduler_events_no_delete BEFORE DELETE ON scheduler_events BEGIN
 SELECT RAISE(ABORT,'scheduler events must be retained'); END;
CREATE TRIGGER scheduler_generations_no_update BEFORE UPDATE ON scheduler_generations BEGIN
 SELECT RAISE(ABORT,'worker generation history is immutable'); END;
CREATE TRIGGER scheduler_generations_no_delete BEFORE DELETE ON scheduler_generations BEGIN
 SELECT RAISE(ABORT,'worker generation history must be retained'); END;
CREATE TRIGGER scheduler_queue_no_delete BEFORE DELETE ON scheduler_queue BEGIN
 SELECT RAISE(ABORT,'queue history must be retained'); END;
CREATE TRIGGER scheduler_queue_monotonic BEFORE UPDATE ON scheduler_queue
WHEN NEW.queue_id<>OLD.queue_id OR NEW.run_id<>OLD.run_id OR NEW.created_at<>OLD.created_at
 OR NEW.epoch<OLD.epoch OR NEW.revision<>OLD.revision+1 BEGIN
 SELECT RAISE(ABORT,'invalid durable queue revision'); END;
CREATE TRIGGER scheduler_active_capacity BEFORE UPDATE ON scheduler_queue
WHEN NEW.status='ACTIVE' AND OLD.status<>'ACTIVE' AND
 (SELECT count(*) FROM scheduler_queue WHERE status='ACTIVE')>=2 BEGIN
 SELECT RAISE(ABORT,'active execution slot limit reached'); END;
-- A reservation is one place in the same four-context pool used by login windows.
CREATE TRIGGER scheduler_context_capacity BEFORE INSERT ON scheduler_context_reservations
WHEN NEW.session_id IS NULL AND (
 (SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING'))+
 (SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL))>=4 BEGIN
 SELECT RAISE(ABORT,'managed browser context limit reached'); END;
CREATE TRIGGER browser_sessions_reserved_capacity BEFORE INSERT ON browser_sessions
WHEN (SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING'))+
 (SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL)>=4 BEGIN
 SELECT RAISE(ABORT,'managed browser context limit reached'); END;
-- A materialized reservation may only point at its own run and Worker.
CREATE TRIGGER scheduler_context_binding BEFORE UPDATE OF session_id ON scheduler_context_reservations
WHEN OLD.session_id IS NOT NULL AND NEW.session_id IS NOT OLD.session_id BEGIN
 SELECT RAISE(ABORT,'context reservation cannot be rebound'); END;
