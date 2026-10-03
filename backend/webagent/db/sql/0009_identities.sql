-- Login requests are not identities. Only a completed, scoped verification
-- publishes an identity and its encrypted authentication snapshot reference.
CREATE TABLE login_requests (
    login_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 200),
    realm TEXT NOT NULL CHECK(realm IN ('public','webarena')),
    origin TEXT NOT NULL CHECK(length(origin) BETWEEN 1 AND 300),
    expected_account TEXT CHECK(expected_account IS NULL OR length(expected_account) BETWEEN 1 AND 256),
    expected_identity_ref TEXT REFERENCES identities(identity_ref),
    restore_auth_ref TEXT REFERENCES browser_auth_snapshots(auth_ref),
    session_id TEXT UNIQUE REFERENCES browser_sessions(session_id),
    manager_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('OPENING','AWAITING_USER','VERIFYING','VERIFIED','NEEDS_LOGIN','LOST','CLOSED','FAILED')),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    candidate_identity_ref TEXT,
    candidate_account TEXT,
    identity_ref TEXT REFERENCES identities(identity_ref),
    auth_ref TEXT REFERENCES browser_auth_snapshots(auth_ref),
    verification_id TEXT REFERENCES identity_verifications(verification_id),
    reason TEXT CHECK(reason IN ('not_logged_in','wrong_account','wrong_origin','ambiguous_identity',
        'verification_timeout','session_lost','session_closed','manager_restarted','auth_unavailable',
        'verification_failed','operation_cancelled','launch_failed','identity_conflict','unknown',
        'browser_unavailable','navigation_failed','not_authenticated','account_mismatch','unverifiable',
        'verification_changed','storage_unavailable')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((session_id IS NULL)=(manager_id IS NULL)),
    CHECK((expected_identity_ref IS NULL)=(restore_auth_ref IS NULL)),
    CHECK((candidate_identity_ref IS NULL)=(candidate_account IS NULL)),
    CHECK((identity_ref IS NULL)=(auth_ref IS NULL) AND (identity_ref IS NULL)=(verification_id IS NULL)),
    CHECK(state<>'VERIFIED' OR identity_ref IS NOT NULL),
    CHECK(state NOT IN ('AWAITING_USER','VERIFYING','VERIFIED') OR session_id IS NOT NULL)
) STRICT;
CREATE TABLE identities (
    identity_ref TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    realm TEXT NOT NULL CHECK(realm IN ('public','webarena')),
    origin TEXT NOT NULL,
    normalized_account TEXT NOT NULL CHECK(length(normalized_account) BETWEEN 1 AND 256),
    state TEXT NOT NULL CHECK(state IN ('VERIFIED','NEEDS_LOGIN')),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    auth_ref TEXT NOT NULL REFERENCES browser_auth_snapshots(auth_ref),
    last_verification_id TEXT NOT NULL REFERENCES identity_verifications(verification_id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id,realm,normalized_account),
    FOREIGN KEY(identity_ref,last_verification_id,auth_ref)
      REFERENCES identity_verifications(identity_ref,verification_id,auth_ref) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE identity_verifications (
    verification_id TEXT PRIMARY KEY,
    login_id TEXT NOT NULL REFERENCES login_requests(login_id),
    login_state_version INTEGER NOT NULL CHECK(login_state_version>=0),
    identity_ref TEXT NOT NULL REFERENCES identities(identity_ref),
    session_id TEXT NOT NULL REFERENCES browser_sessions(session_id),
    site_id TEXT NOT NULL,
    realm TEXT NOT NULL CHECK(realm IN ('public','webarena')),
    normalized_account TEXT NOT NULL,
    verification_origin TEXT NOT NULL,
    adapter_id TEXT NOT NULL CHECK(length(adapter_id) BETWEEN 1 AND 200),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    auth_ref TEXT NOT NULL REFERENCES browser_auth_snapshots(auth_ref),
    verified_at TEXT NOT NULL,
    UNIQUE(login_id,login_state_version),
    UNIQUE(identity_ref,verification_id,auth_ref)
) STRICT;
CREATE TABLE identity_login_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    login_id TEXT NOT NULL REFERENCES login_requests(login_id),
    state_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    occurred_at TEXT NOT NULL,
    UNIQUE(login_id,state_version)
) STRICT;
CREATE INDEX login_requests_state ON login_requests(state);
CREATE INDEX identity_verifications_identity ON identity_verifications(identity_ref,verified_at);

CREATE TRIGGER login_requests_initial BEFORE INSERT ON login_requests
WHEN NEW.state<>'OPENING' OR NEW.state_version<>0 OR NEW.session_id IS NOT NULL
 OR NEW.candidate_identity_ref IS NOT NULL OR NEW.identity_ref IS NOT NULL OR NEW.reason IS NOT NULL
 OR EXISTS(SELECT 1 FROM login_requests WHERE login_id=NEW.login_id)
 OR (NEW.expected_identity_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM identities i
   WHERE i.identity_ref=NEW.expected_identity_ref AND i.site_id=NEW.site_id AND i.realm=NEW.realm
     AND i.origin=NEW.origin AND i.normalized_account=NEW.expected_account AND i.auth_ref=NEW.restore_auth_ref)) BEGIN
 SELECT RAISE(ABORT,'invalid initial login request');
END;
CREATE TRIGGER login_requests_scope BEFORE UPDATE ON login_requests
WHEN NEW.login_id<>OLD.login_id OR NEW.site_id<>OLD.site_id OR NEW.realm<>OLD.realm OR NEW.origin<>OLD.origin
 OR NEW.expected_account IS NOT OLD.expected_account OR NEW.expected_identity_ref IS NOT OLD.expected_identity_ref
 OR NEW.restore_auth_ref IS NOT OLD.restore_auth_ref
 OR NEW.created_at<>OLD.created_at OR NEW.state_version<>OLD.state_version+1
 OR (OLD.session_id IS NOT NULL AND (NEW.session_id IS NOT OLD.session_id OR NEW.manager_id IS NOT OLD.manager_id))
 OR (OLD.identity_ref IS NOT NULL AND (NEW.identity_ref IS NOT OLD.identity_ref
     OR NEW.auth_ref IS NOT OLD.auth_ref OR NEW.verification_id IS NOT OLD.verification_id))
 OR NOT ((OLD.state='OPENING' AND NEW.state IN ('AWAITING_USER','FAILED','LOST','CLOSED'))
     OR (OLD.state IN ('AWAITING_USER','NEEDS_LOGIN') AND NEW.state IN ('VERIFYING','NEEDS_LOGIN','LOST','CLOSED','FAILED'))
     OR (OLD.state='VERIFYING' AND NEW.state IN ('VERIFYING','VERIFIED','NEEDS_LOGIN','LOST','CLOSED','FAILED'))
     OR (OLD.state='VERIFIED' AND NEW.state IN ('LOST','CLOSED'))) BEGIN
 SELECT RAISE(ABORT,'invalid login request transition');
END;
CREATE TRIGGER login_requests_session BEFORE UPDATE ON login_requests
WHEN NEW.session_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM browser_sessions s
 WHERE s.session_id=NEW.session_id AND s.owner_kind='login' AND s.owner_id=NEW.login_id
 AND s.site_id=NEW.site_id AND s.realm=NEW.realm AND s.identity_ref IS NEW.expected_identity_ref
 AND s.auth_ref IS NEW.restore_auth_ref
 AND s.manager_id=NEW.manager_id) BEGIN
 SELECT RAISE(ABORT,'login session ownership mismatch');
END;
CREATE TRIGGER login_requests_candidate BEFORE UPDATE ON login_requests
WHEN NEW.candidate_identity_ref IS NOT NULL
 AND (NEW.candidate_identity_ref IS NOT OLD.candidate_identity_ref OR NEW.candidate_account IS NOT OLD.candidate_account
      OR NEW.state='VERIFIED') AND
 ((NEW.expected_account IS NOT NULL AND NEW.candidate_account<>NEW.expected_account)
 OR (NEW.expected_identity_ref IS NOT NULL AND NEW.candidate_identity_ref<>NEW.expected_identity_ref)
 OR (EXISTS(SELECT 1 FROM identities i WHERE i.site_id=NEW.site_id AND i.realm=NEW.realm
       AND i.normalized_account=NEW.candidate_account AND i.identity_ref<>NEW.candidate_identity_ref))
 OR (EXISTS(SELECT 1 FROM identities i WHERE i.identity_ref=NEW.candidate_identity_ref AND
     (i.site_id<>NEW.site_id OR i.realm<>NEW.realm OR i.origin<>NEW.origin OR i.normalized_account<>NEW.candidate_account)))) BEGIN
 SELECT RAISE(ABORT,'candidate identity mismatch');
END;
CREATE TRIGGER login_requests_verified BEFORE UPDATE ON login_requests
WHEN NEW.identity_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM identity_verifications v
 WHERE v.verification_id=NEW.verification_id AND v.login_id=NEW.login_id
 AND v.identity_ref=NEW.identity_ref AND v.auth_ref=NEW.auth_ref AND v.session_id=NEW.session_id) BEGIN
 SELECT RAISE(ABORT,'verified login requires immutable verification');
END;
CREATE TRIGGER login_requests_publish BEFORE UPDATE ON login_requests
WHEN NEW.state='VERIFIED' AND (OLD.state<>'VERIFYING' OR NEW.identity_ref IS NOT OLD.candidate_identity_ref
 OR NOT EXISTS(SELECT 1 FROM browser_sessions s WHERE s.session_id=NEW.session_id AND s.state='OPEN')
 OR NOT EXISTS(SELECT 1 FROM identity_verifications v WHERE v.verification_id=NEW.verification_id
   AND v.login_state_version=OLD.state_version AND v.normalized_account=OLD.candidate_account)) BEGIN
 SELECT RAISE(ABORT,'identity publication requires active verification');
END;
CREATE TRIGGER login_requests_no_delete BEFORE DELETE ON login_requests BEGIN
 SELECT RAISE(ABORT,'login request history must be retained');
END;
CREATE TRIGGER login_requests_event_created AFTER INSERT ON login_requests BEGIN
 INSERT INTO identity_login_events(login_id,state_version,state,reason,occurred_at)
 VALUES(NEW.login_id,NEW.state_version,NEW.state,NEW.reason,NEW.updated_at);
END;
CREATE TRIGGER login_requests_event_changed AFTER UPDATE ON login_requests BEGIN
 INSERT INTO identity_login_events(login_id,state_version,state,reason,occurred_at)
 VALUES(NEW.login_id,NEW.state_version,NEW.state,NEW.reason,NEW.updated_at);
END;

CREATE TRIGGER identities_scope BEFORE INSERT ON identities
WHEN NEW.state<>'VERIFIED' OR NEW.state_version<>0
 OR EXISTS(SELECT 1 FROM identities WHERE identity_ref=NEW.identity_ref)
 OR NOT EXISTS(SELECT 1 FROM login_requests l JOIN browser_sessions s ON s.session_id=l.session_id
     WHERE l.state='VERIFYING' AND s.state='OPEN' AND l.candidate_identity_ref=NEW.identity_ref
     AND l.site_id=NEW.site_id AND l.realm=NEW.realm AND l.origin=NEW.origin
     AND l.candidate_account=NEW.normalized_account)
 OR NOT EXISTS(SELECT 1 FROM browser_auth_snapshots a WHERE a.auth_ref=NEW.auth_ref
     AND a.identity_ref=NEW.identity_ref AND a.site_id=NEW.site_id AND a.realm=NEW.realm) BEGIN
 SELECT RAISE(ABORT,'identity requires scoped verification');
END;
CREATE TRIGGER identities_update BEFORE UPDATE ON identities
WHEN NEW.identity_ref<>OLD.identity_ref OR NEW.site_id<>OLD.site_id OR NEW.realm<>OLD.realm
 OR NEW.origin<>OLD.origin OR NEW.normalized_account<>OLD.normalized_account OR NEW.created_at<>OLD.created_at
 OR NEW.state_version<>OLD.state_version+1
 OR NOT EXISTS(SELECT 1 FROM identity_verifications v WHERE v.verification_id=NEW.last_verification_id
   AND v.identity_ref=NEW.identity_ref AND v.auth_ref=NEW.auth_ref)
 OR (NEW.state='VERIFIED' AND NEW.last_verification_id=OLD.last_verification_id) BEGIN
 SELECT RAISE(ABORT,'identity scope and verification binding are immutable');
END;
CREATE TRIGGER identities_no_delete BEFORE DELETE ON identities BEGIN
 SELECT RAISE(ABORT,'identity history must be retained');
END;
CREATE TRIGGER identity_verifications_scope BEFORE INSERT ON identity_verifications
WHEN EXISTS(SELECT 1 FROM identity_verifications WHERE verification_id=NEW.verification_id)
 OR NOT EXISTS(SELECT 1 FROM login_requests l JOIN browser_sessions s ON s.session_id=l.session_id
     JOIN identities i ON i.identity_ref=NEW.identity_ref
   WHERE l.login_id=NEW.login_id AND l.state='VERIFYING' AND l.state_version=NEW.login_state_version
     AND l.session_id=NEW.session_id AND s.state='OPEN' AND s.manager_id=l.manager_id
     AND l.candidate_identity_ref=NEW.identity_ref AND l.candidate_account=NEW.normalized_account
     AND l.site_id=NEW.site_id AND l.realm=NEW.realm AND l.origin=NEW.verification_origin
     AND i.site_id=NEW.site_id AND i.realm=NEW.realm AND i.origin=NEW.verification_origin
     AND i.normalized_account=NEW.normalized_account)
 OR NOT EXISTS(SELECT 1 FROM browser_auth_snapshots a WHERE a.auth_ref=NEW.auth_ref
   AND a.identity_ref=NEW.identity_ref AND a.site_id=NEW.site_id AND a.realm=NEW.realm) BEGIN
 SELECT RAISE(ABORT,'verification scope mismatch');
END;
CREATE TRIGGER identity_verifications_no_update BEFORE UPDATE ON identity_verifications BEGIN
 SELECT RAISE(ABORT,'identity verification is immutable');
END;
CREATE TRIGGER identity_verifications_no_delete BEFORE DELETE ON identity_verifications BEGIN
 SELECT RAISE(ABORT,'identity verification history must be retained');
END;
CREATE TRIGGER identity_login_events_scope BEFORE INSERT ON identity_login_events
WHEN EXISTS(SELECT 1 FROM identity_login_events WHERE event_id=NEW.event_id
 OR (login_id=NEW.login_id AND state_version=NEW.state_version))
 OR NOT EXISTS(SELECT 1 FROM login_requests l WHERE l.login_id=NEW.login_id
   AND l.state_version=NEW.state_version AND l.state=NEW.state AND l.reason IS NEW.reason
   AND l.updated_at=NEW.occurred_at) BEGIN
 SELECT RAISE(ABORT,'login event must match current state');
END;
CREATE TRIGGER identity_login_events_no_update BEFORE UPDATE ON identity_login_events BEGIN
 SELECT RAISE(ABORT,'login events are immutable');
END;
CREATE TRIGGER identity_login_events_no_delete BEFORE DELETE ON identity_login_events BEGIN
 SELECT RAISE(ABORT,'login event history must be retained');
END;
