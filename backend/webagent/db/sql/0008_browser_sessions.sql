-- M1-08: browser lifecycle metadata and encrypted-state references only.
CREATE TABLE browser_auth_snapshots (
    auth_ref TEXT PRIMARY KEY CHECK(length(auth_ref)=36),
    site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 200),
    identity_ref TEXT CHECK(identity_ref IS NULL OR length(identity_ref) BETWEEN 1 AND 200),
    realm TEXT NOT NULL CHECK(realm IN ('public','webarena')),
    sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL
) STRICT;
CREATE TABLE browser_sessions (
    session_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('run','login','verification')),
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 200),
    run_id TEXT REFERENCES runs(run_id),
    site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 200),
    identity_ref TEXT CHECK(identity_ref IS NULL OR length(identity_ref) BETWEEN 1 AND 200),
    realm TEXT NOT NULL CHECK(realm IN ('public','webarena')),
    manager_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('OPENING','OPEN','CLOSING','CLOSED','LOST')),
    generation INTEGER NOT NULL CHECK(generation>0),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    auth_ref TEXT REFERENCES browser_auth_snapshots(auth_ref),
    requires_identity_check INTEGER NOT NULL DEFAULT 1 CHECK(requires_identity_check=1),
    requires_business_check INTEGER NOT NULL DEFAULT 1 CHECK(requires_business_check=1),
    restored_from_session_id TEXT REFERENCES browser_sessions(session_id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    loss_reason TEXT CHECK(loss_reason IN ('window_closed','context_closed','page_crashed','browser_disconnected',
        'manager_restarted','launch_failed','context_create_failed','auth_unavailable','close_failed',
        'shutdown_failed','operation_cancelled','unknown')),
    CHECK((owner_kind='run' AND run_id IS NOT NULL AND owner_id=run_id) OR (owner_kind<>'run' AND run_id IS NULL)),
    CHECK((state IN ('CLOSED','LOST'))=(closed_at IS NOT NULL)),
    CHECK((state='LOST')=(loss_reason IS NOT NULL))
) STRICT;
CREATE INDEX browser_sessions_manager_state ON browser_sessions(manager_id,state);
CREATE INDEX browser_sessions_run ON browser_sessions(run_id);
CREATE TABLE browser_session_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES browser_sessions(session_id),
    state_version INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('reserved','opened','closing','closed','lost','auth_saved','recheck_required')),
    reason TEXT,
    occurred_at TEXT NOT NULL,
    UNIQUE(session_id,state_version,event_type)
) STRICT;
CREATE TRIGGER browser_sessions_limit BEFORE INSERT ON browser_sessions
WHEN (SELECT COUNT(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING'))>=4 BEGIN
 SELECT RAISE(ABORT,'managed browser context limit reached');
END;
CREATE TRIGGER browser_sessions_auth_insert BEFORE INSERT ON browser_sessions
WHEN NEW.auth_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browser_auth_snapshots a
 WHERE a.auth_ref=NEW.auth_ref AND a.site_id=NEW.site_id AND a.identity_ref IS NEW.identity_ref AND a.realm=NEW.realm) BEGIN
 SELECT RAISE(ABORT,'authentication snapshot scope mismatch');
END;
CREATE TRIGGER browser_sessions_auth_update BEFORE UPDATE OF auth_ref ON browser_sessions
WHEN NEW.auth_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browser_auth_snapshots a
 WHERE a.auth_ref=NEW.auth_ref AND a.site_id=NEW.site_id AND a.identity_ref IS NEW.identity_ref AND a.realm=NEW.realm) BEGIN
 SELECT RAISE(ABORT,'authentication snapshot scope mismatch');
END;
CREATE TRIGGER browser_sessions_replacement BEFORE INSERT ON browser_sessions
WHEN (NEW.restored_from_session_id IS NULL AND NEW.generation<>1) OR
 (NEW.restored_from_session_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browser_sessions p
  WHERE p.session_id=NEW.restored_from_session_id AND p.state IN ('CLOSED','LOST')
   AND p.owner_kind=NEW.owner_kind AND p.owner_id=NEW.owner_id AND p.site_id=NEW.site_id
   AND p.identity_ref IS NEW.identity_ref AND p.realm=NEW.realm AND NEW.generation=p.generation+1)) BEGIN
 SELECT RAISE(ABORT,'replacement must preserve closed session ownership');
END;
CREATE TRIGGER browser_sessions_transition BEFORE UPDATE ON browser_sessions
WHEN NEW.session_id<>OLD.session_id OR NEW.owner_kind<>OLD.owner_kind OR NEW.owner_id<>OLD.owner_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.site_id<>OLD.site_id OR NEW.identity_ref IS NOT OLD.identity_ref
 OR NEW.realm<>OLD.realm OR NEW.manager_id<>OLD.manager_id OR NEW.generation<>OLD.generation
 OR NEW.restored_from_session_id IS NOT OLD.restored_from_session_id OR NEW.created_at<>OLD.created_at
 OR NEW.state_version<>OLD.state_version+1 OR OLD.state IN ('CLOSED','LOST')
 OR NOT ((OLD.state='OPENING' AND NEW.state IN ('OPEN','CLOSING','LOST'))
      OR (OLD.state='OPEN' AND NEW.state IN ('OPEN','CLOSING','LOST'))
      OR (OLD.state='CLOSING' AND NEW.state IN ('CLOSED','LOST'))) BEGIN
 SELECT RAISE(ABORT,'invalid managed session transition');
END;
CREATE TRIGGER browser_sessions_no_delete BEFORE DELETE ON browser_sessions BEGIN
 SELECT RAISE(ABORT,'session history must be retained');
END;
CREATE TRIGGER browser_sessions_no_replace BEFORE INSERT ON browser_sessions
WHEN EXISTS(SELECT 1 FROM browser_sessions WHERE session_id=NEW.session_id) BEGIN
 SELECT RAISE(ABORT,'session already exists');
END;
CREATE TRIGGER browser_auth_no_update BEFORE UPDATE ON browser_auth_snapshots BEGIN
 SELECT RAISE(ABORT,'authentication snapshots are immutable');
END;
CREATE TRIGGER browser_auth_no_delete BEFORE DELETE ON browser_auth_snapshots BEGIN
 SELECT RAISE(ABORT,'authentication snapshot references must be retained');
END;
CREATE TRIGGER browser_auth_no_replace BEFORE INSERT ON browser_auth_snapshots
WHEN EXISTS(SELECT 1 FROM browser_auth_snapshots WHERE auth_ref=NEW.auth_ref) BEGIN
 SELECT RAISE(ABORT,'authentication snapshot already exists');
END;
CREATE TRIGGER browser_session_events_no_update BEFORE UPDATE ON browser_session_events BEGIN
 SELECT RAISE(ABORT,'session events are immutable');
END;
CREATE TRIGGER browser_session_events_no_delete BEFORE DELETE ON browser_session_events BEGIN
 SELECT RAISE(ABORT,'session events must be retained');
END;
CREATE TRIGGER browser_session_events_no_replace BEFORE INSERT ON browser_session_events
WHEN EXISTS(SELECT 1 FROM browser_session_events WHERE event_id=NEW.event_id
 OR (session_id=NEW.session_id AND state_version=NEW.state_version AND event_type=NEW.event_type)) BEGIN
 SELECT RAISE(ABORT,'session event already exists');
END;
