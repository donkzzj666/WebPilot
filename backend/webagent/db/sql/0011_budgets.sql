-- M1-11: immutable ceilings and dispatch ledger, monotonic timing checkpoints.
-- v2 ledger rows stay intact; these additive tables give them runtime semantics.
CREATE TABLE budget_limits (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    max_actions INTEGER NOT NULL CHECK(max_actions BETWEEN 1 AND 150),
    max_content_pages INTEGER NOT NULL CHECK(max_content_pages BETWEEN 1 AND 25),
    max_active_seconds INTEGER NOT NULL CHECK(max_active_seconds BETWEEN 1 AND 1200),
    max_recoveries_per_obstacle INTEGER NOT NULL CHECK(max_recoveries_per_obstacle BETWEEN 0 AND 3),
    min_site_interval_seconds INTEGER NOT NULL CHECK(min_site_interval_seconds>=3),
    max_ci_wait_seconds INTEGER NOT NULL CHECK(max_ci_wait_seconds BETWEEN 0 AND 1200),
    min_ci_poll_seconds INTEGER NOT NULL CHECK(min_ci_poll_seconds>=30),
    max_handoff_seconds INTEGER NOT NULL CHECK(max_handoff_seconds BETWEEN 1 AND 86400),
    action_timeout_seconds INTEGER NOT NULL CHECK(action_timeout_seconds BETWEEN 1 AND 30),
    max_model_format_repairs INTEGER NOT NULL CHECK(max_model_format_repairs BETWEEN 0 AND 2),
    created_at TEXT NOT NULL CHECK(length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')
) STRICT;
CREATE TABLE budget_timers (
    run_id TEXT PRIMARY KEY REFERENCES budget_limits(run_id),
    mode TEXT NOT NULL DEFAULT 'inactive' CHECK(mode IN ('inactive','active','ci')),
    anchor_utc TEXT NOT NULL CHECK(length(anchor_utc)=27 AND anchor_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(anchor_utc,1,23)||'Z') IS substr(anchor_utc,1,23)||'Z'),
    anchor_mono_ns INTEGER NOT NULL CHECK(anchor_mono_ns>=0),
    clock_domain TEXT NOT NULL CHECK(length(clock_domain) BETWEEN 1 AND 200),
    active_remainder_ns INTEGER NOT NULL DEFAULT 0 CHECK(active_remainder_ns BETWEEN 0 AND 999999),
    ci_remainder_ns INTEGER NOT NULL DEFAULT 0 CHECK(ci_remainder_ns BETWEEN 0 AND 999999),
    handoff_deadline TEXT CHECK(handoff_deadline IS NULL OR (length(handoff_deadline)=27 AND handoff_deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(handoff_deadline,1,23)||'Z') IS substr(handoff_deadline,1,23)||'Z')),
    handoff_started_utc TEXT CHECK(handoff_started_utc IS NULL OR (length(handoff_started_utc)=27 AND handoff_started_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(handoff_started_utc,1,23)||'Z') IS substr(handoff_started_utc,1,23)||'Z')),
    handoff_started_mono_ns INTEGER CHECK(handoff_started_mono_ns IS NULL OR handoff_started_mono_ns>=0),
    handoff_clock_domain TEXT,
    last_ci_poll_utc TEXT CHECK(last_ci_poll_utc IS NULL OR (length(last_ci_poll_utc)=27 AND last_ci_poll_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(last_ci_poll_utc,1,23)||'Z') IS substr(last_ci_poll_utc,1,23)||'Z')),
    last_ci_poll_mono_ns INTEGER CHECK(last_ci_poll_mono_ns IS NULL OR last_ci_poll_mono_ns>=0),
    last_ci_poll_domain TEXT,
    stop_reason TEXT CHECK(stop_reason IN ('active_time','ci_wait','handoff','site_wait_exceeds_budget','action_limit','content_page_limit','recovery_limit')),
    stopped_at TEXT CHECK(stopped_at IS NULL OR (length(stopped_at)=27 AND stopped_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(stopped_at,1,23)||'Z') IS substr(stopped_at,1,23)||'Z')),
    CHECK((stop_reason IS NULL)=(stopped_at IS NULL)),
    CHECK((handoff_deadline IS NULL)=(handoff_started_utc IS NULL)),
    CHECK((handoff_deadline IS NULL)=(handoff_started_mono_ns IS NULL)),
    CHECK((handoff_deadline IS NULL)=(handoff_clock_domain IS NULL)),
    CHECK((last_ci_poll_utc IS NULL)=(last_ci_poll_mono_ns IS NULL)),
    CHECK((last_ci_poll_utc IS NULL)=(last_ci_poll_domain IS NULL))
) STRICT;
CREATE TABLE budget_attempts (
    run_id TEXT NOT NULL REFERENCES budget_limits(run_id),
    attempt_id TEXT NOT NULL CHECK(length(attempt_id) BETWEEN 1 AND 200),
    kind TEXT NOT NULL CHECK(kind IN ('action','observation','screenshot','recovery','ci_poll')),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    epoch INTEGER NOT NULL CHECK(epoch>0),
    actions INTEGER NOT NULL CHECK(actions IN (0,1)),
    content_pages INTEGER NOT NULL CHECK(content_pages IN (0,1)),
    recovery_key TEXT CHECK(recovery_key IS NULL OR (length(recovery_key)=64 AND recovery_key NOT GLOB '*[^0-9a-f]*')),
    consumed_at TEXT NOT NULL CHECK(length(consumed_at)=27 AND consumed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(consumed_at,1,23)||'Z') IS substr(consumed_at,1,23)||'Z'),
    PRIMARY KEY(run_id,attempt_id)
) STRICT;
CREATE TABLE quota_monitor_sources (
    debit_id TEXT PRIMARY KEY REFERENCES quota_debits(debit_id),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('security_community','cisa_kev'))
) STRICT;
CREATE TABLE site_pacing (
    site_id TEXT PRIMARY KEY CHECK(length(site_id) BETWEEN 1 AND 200),
    last_utc TEXT NOT NULL CHECK(length(last_utc)=27 AND last_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(last_utc,1,23)||'Z') IS substr(last_utc,1,23)||'Z'),
    last_mono_ns INTEGER NOT NULL CHECK(last_mono_ns>=0),
    clock_domain TEXT NOT NULL CHECK(length(clock_domain) BETWEEN 1 AND 200),
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds>=3)
) STRICT;
CREATE INDEX budget_timers_due ON budget_timers(mode,stop_reason,handoff_deadline);
CREATE TRIGGER budget_limits_no_update BEFORE UPDATE ON budget_limits
BEGIN SELECT RAISE(ABORT,'budget ceilings are immutable'); END;
CREATE TRIGGER budget_limits_no_delete BEFORE DELETE ON budget_limits
BEGIN SELECT RAISE(ABORT,'budget ceilings are immutable'); END;
CREATE TRIGGER budget_limits_no_replace BEFORE INSERT ON budget_limits
WHEN EXISTS(SELECT 1 FROM budget_limits WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'budget ceilings are immutable'); END;
CREATE TRIGGER budget_attempts_no_update BEFORE UPDATE ON budget_attempts
BEGIN SELECT RAISE(ABORT,'budget attempt is immutable'); END;
CREATE TRIGGER budget_attempts_no_delete BEFORE DELETE ON budget_attempts
BEGIN SELECT RAISE(ABORT,'budget attempt is immutable'); END;
CREATE TRIGGER budget_attempts_no_replace BEFORE INSERT ON budget_attempts
WHEN EXISTS(SELECT 1 FROM budget_attempts WHERE run_id=NEW.run_id AND attempt_id=NEW.attempt_id)
BEGIN SELECT RAISE(ABORT,'budget attempt is immutable'); END;
CREATE TRIGGER quota_monitor_sources_no_update BEFORE UPDATE ON quota_monitor_sources
BEGIN SELECT RAISE(ABORT,'monitor debit source is immutable'); END;
CREATE TRIGGER quota_monitor_sources_no_delete BEFORE DELETE ON quota_monitor_sources
BEGIN SELECT RAISE(ABORT,'monitor debit source is immutable'); END;
CREATE TRIGGER quota_monitor_sources_no_replace BEFORE INSERT ON quota_monitor_sources
WHEN EXISTS(SELECT 1 FROM quota_monitor_sources WHERE debit_id=NEW.debit_id)
BEGIN SELECT RAISE(ABORT,'monitor debit source is immutable'); END;
CREATE TRIGGER budget_timers_no_delete BEFORE DELETE ON budget_timers
BEGIN SELECT RAISE(ABORT,'budget timing history cannot be deleted'); END;
CREATE TRIGGER budget_timers_no_replace BEFORE INSERT ON budget_timers
WHEN EXISTS(SELECT 1 FROM budget_timers WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'budget timing history cannot be replaced'); END;
CREATE TRIGGER budget_timers_deadline_monotonic BEFORE UPDATE ON budget_timers
WHEN NEW.run_id IS NOT OLD.run_id
 OR (OLD.handoff_deadline IS NOT NULL AND (NEW.handoff_deadline IS NULL OR NEW.handoff_deadline>OLD.handoff_deadline))
 OR (OLD.handoff_started_utc IS NOT NULL AND (NEW.handoff_started_utc IS NOT OLD.handoff_started_utc OR NEW.handoff_started_mono_ns IS NOT OLD.handoff_started_mono_ns OR NEW.handoff_clock_domain IS NOT OLD.handoff_clock_domain))
 OR (OLD.stop_reason IS NOT NULL AND (NEW.stop_reason IS NOT OLD.stop_reason OR NEW.stopped_at IS NOT OLD.stopped_at))
BEGIN SELECT RAISE(ABORT,'budget stop and handoff deadline cannot regress'); END;
-- Deadline failures require direct terminal edges without fabricated execution.
DROP TRIGGER run_transitions_no_insert;
INSERT INTO run_transitions VALUES ('RECONCILING','FAILED'),('WAITING_SITE','FAILED'),('PAUSED','FAILED');
CREATE TRIGGER run_transitions_no_insert BEFORE INSERT ON run_transitions BEGIN
 SELECT RAISE(ABORT,'transition matrix is immutable');
END;
