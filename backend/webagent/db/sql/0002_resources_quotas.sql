-- M1-02 / v2: additive resource and quota ledgers; no scheduler yet.
CREATE TABLE quota_buckets (
    quota_date TEXT NOT NULL CHECK(length(quota_date)=10 AND date(quota_date) IS quota_date),
    quota_type TEXT NOT NULL CHECK(quota_type IN ('public')),
    capacity INTEGER NOT NULL DEFAULT 50 CHECK(capacity>=0),
    monitor_reserved INTEGER NOT NULL DEFAULT 8 CHECK(monitor_reserved>=0 AND monitor_reserved<=capacity),
    used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0 AND used<=capacity),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0),
    PRIMARY KEY(quota_date,quota_type)
) STRICT;
CREATE TABLE quota_debits (
    debit_id TEXT NOT NULL CHECK(length(debit_id) BETWEEN 1 AND 200) PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) UNIQUE,
    quota_date TEXT NOT NULL,
    quota_type TEXT NOT NULL CHECK(quota_type IN ('public')),
    debit_kind TEXT NOT NULL CHECK(debit_kind IN ('ordinary','monitoring')),
    amount INTEGER NOT NULL DEFAULT 1 CHECK(amount=1),
    debited_at TEXT NOT NULL CHECK(debited_at IS NULL OR (length(debited_at)=27 AND debited_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(debited_at,1,23)||'Z') IS substr(debited_at,1,23)||'Z')),
    UNIQUE(run_id,debit_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id),
    FOREIGN KEY(quota_date,quota_type) REFERENCES quota_buckets(quota_date,quota_type)
) STRICT;
CREATE TABLE run_budgets (
    budget_record_id TEXT NOT NULL CHECK(length(budget_record_id) BETWEEN 1 AND 200) PRIMARY KEY,
    run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200) UNIQUE,
    quota_debit_id TEXT,
    actions_used INTEGER NOT NULL DEFAULT 0 CHECK(actions_used>=0),
    content_pages_used INTEGER NOT NULL DEFAULT 0 CHECK(content_pages_used>=0),
    active_ms INTEGER NOT NULL DEFAULT 0 CHECK(active_ms>=0),
    ci_wait_ms INTEGER NOT NULL DEFAULT 0 CHECK(ci_wait_ms>=0),
    observations_used INTEGER NOT NULL DEFAULT 0 CHECK(observations_used>=0),
    screenshots_used INTEGER NOT NULL DEFAULT 0 CHECK(screenshots_used>=0),
    model_calls_used INTEGER NOT NULL DEFAULT 0 CHECK(model_calls_used>=0),
    recovery_counts_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(recovery_counts_json) AND json_type(recovery_counts_json)='object'),
    last_persisted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')||'000Z') CHECK(last_persisted_at IS NULL OR (length(last_persisted_at)=27 AND last_persisted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(last_persisted_at,1,23)||'Z') IS substr(last_persisted_at,1,23)||'Z')),
    active_interval_started_at TEXT CHECK(active_interval_started_at IS NULL OR (length(active_interval_started_at)=27 AND active_interval_started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(active_interval_started_at,1,23)||'Z') IS substr(active_interval_started_at,1,23)||'Z')),
    heartbeat_at TEXT CHECK(heartbeat_at IS NULL OR (length(heartbeat_at)=27 AND heartbeat_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(heartbeat_at,1,23)||'Z') IS substr(heartbeat_at,1,23)||'Z')),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0),
    UNIQUE(run_id,budget_record_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id),
    FOREIGN KEY(run_id,quota_debit_id) REFERENCES quota_debits(run_id,debit_id)
) STRICT;
CREATE TABLE resource_leases (
    resource_key TEXT NOT NULL CHECK(length(resource_key) BETWEEN 1 AND 200) PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('active_slot','site_identity','repository_write','webarena_environment','browser_context')),
    holder_run_id TEXT NOT NULL CHECK(length(holder_run_id) BETWEEN 1 AND 200) ,
    worker_id TEXT NOT NULL CHECK(length(worker_id) BETWEEN 1 AND 200) ,
    epoch INTEGER NOT NULL CHECK(epoch>0),
    expires_at TEXT NOT NULL CHECK(expires_at IS NULL OR (length(expires_at)=27 AND expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(expires_at,1,23)||'Z') IS substr(expires_at,1,23)||'Z')),
    heartbeat_at TEXT NOT NULL CHECK(heartbeat_at IS NULL OR (length(heartbeat_at)=27 AND heartbeat_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(heartbeat_at,1,23)||'Z') IS substr(heartbeat_at,1,23)||'Z')),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0),
    control_owner TEXT NOT NULL DEFAULT 'worker' CHECK(control_owner IN ('worker','human','none')),
    logical_hold INTEGER NOT NULL DEFAULT 0 CHECK(logical_hold IN (0,1)),
    FOREIGN KEY(holder_run_id) REFERENCES runs(run_id),
    CHECK(expires_at>=heartbeat_at)
) STRICT;
CREATE TABLE resource_quarantines (
    resource_key TEXT NOT NULL CHECK(length(resource_key) BETWEEN 1 AND 200) ,
    operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 200) ,
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    PRIMARY KEY(resource_key,operation_id),
    FOREIGN KEY(operation_id) REFERENCES write_intents(operation_id)
) STRICT;
CREATE TABLE site_gates (
    site_id TEXT NOT NULL CHECK(length(site_id) BETWEEN 1 AND 200) PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'OPEN' CHECK(state IN ('OPEN','BLOCKED','COOLDOWN')),
    blocked_reason TEXT,
    next_eligible_at TEXT CHECK(next_eligible_at IS NULL OR (length(next_eligible_at)=27 AND next_eligible_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(next_eligible_at,1,23)||'Z') IS substr(next_eligible_at,1,23)||'Z')),
    updated_at TEXT NOT NULL CHECK(updated_at IS NULL OR (length(updated_at)=27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(updated_at,1,23)||'Z') IS substr(updated_at,1,23)||'Z')),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version>=0)
) STRICT;
CREATE INDEX leases_expiry ON resource_leases(expires_at);
CREATE INDEX leases_run ON resource_leases(holder_run_id);
CREATE INDEX debits_bucket ON quota_debits(quota_date,quota_type,debit_kind);
CREATE INDEX gates_due ON site_gates(state,next_eligible_at);
CREATE TRIGGER quota_debits_no_update BEFORE UPDATE ON quota_debits
BEGIN SELECT RAISE(ABORT, 'quota debit is immutable'); END;
CREATE TRIGGER quota_debits_no_delete BEFORE DELETE ON quota_debits
BEGIN SELECT RAISE(ABORT, 'quota debit is immutable'); END;
CREATE TRIGGER quota_debits_no_replace BEFORE INSERT ON quota_debits
WHEN EXISTS(SELECT 1 FROM quota_debits WHERE run_id=NEW.run_id OR debit_id=NEW.debit_id)
BEGIN SELECT RAISE(ABORT, 'run already debited'); END;
CREATE TRIGGER run_budgets_monotonic BEFORE UPDATE ON run_budgets
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.budget_record_id IS NOT OLD.budget_record_id
 OR (OLD.quota_debit_id IS NOT NULL AND NEW.quota_debit_id IS NOT OLD.quota_debit_id)
 OR NEW.actions_used<OLD.actions_used OR NEW.content_pages_used<OLD.content_pages_used
 OR NEW.active_ms<OLD.active_ms OR NEW.ci_wait_ms<OLD.ci_wait_ms
 OR NEW.observations_used<OLD.observations_used OR NEW.screenshots_used<OLD.screenshots_used
 OR NEW.model_calls_used<OLD.model_calls_used
 OR EXISTS(SELECT 1 FROM json_each(OLD.recovery_counts_json) AS previous
           WHERE NOT EXISTS(SELECT 1 FROM json_each(NEW.recovery_counts_json) AS current
                            WHERE current.key=previous.key AND current.value>=previous.value))
BEGIN SELECT RAISE(ABORT, 'budget identity and usage cannot regress'); END;
CREATE TRIGGER run_budgets_no_replace BEFORE INSERT ON run_budgets
WHEN EXISTS(SELECT 1 FROM run_budgets WHERE run_id=NEW.run_id OR budget_record_id=NEW.budget_record_id)
BEGIN SELECT RAISE(ABORT, 'existing run budget'); END;
CREATE TRIGGER run_budgets_no_delete BEFORE DELETE ON run_budgets
BEGIN SELECT RAISE(ABORT, 'run budget cannot be deleted'); END;
CREATE TRIGGER run_budgets_recovery_shape_insert BEFORE INSERT ON run_budgets
WHEN EXISTS(SELECT 1 FROM json_each(NEW.recovery_counts_json)
            WHERE type<>'integer' OR value<0 OR length(key) NOT BETWEEN 1 AND 200)
 OR (SELECT count(*) FROM json_each(NEW.recovery_counts_json))<>(SELECT count(DISTINCT key) FROM json_each(NEW.recovery_counts_json))
BEGIN SELECT RAISE(ABORT, 'invalid per-obstacle recovery count'); END;
CREATE TRIGGER run_budgets_recovery_shape_update BEFORE UPDATE ON run_budgets
WHEN EXISTS(SELECT 1 FROM json_each(NEW.recovery_counts_json)
            WHERE type<>'integer' OR value<0 OR length(key) NOT BETWEEN 1 AND 200)
 OR (SELECT count(*) FROM json_each(NEW.recovery_counts_json))<>(SELECT count(DISTINCT key) FROM json_each(NEW.recovery_counts_json))
BEGIN SELECT RAISE(ABORT, 'invalid per-obstacle recovery count'); END;
