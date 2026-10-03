-- M1-05: reserve every provider attempt before dispatch, finalize at most once.
-- Neither raw prompts nor raw provider responses belong in these tables.
CREATE TABLE model_generations (
    call_id TEXT PRIMARY KEY NOT NULL CHECK(length(call_id) BETWEEN 1 AND 200),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    config_sha256 TEXT NOT NULL CHECK(length(config_sha256)=64 AND config_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_version TEXT NOT NULL,
    repair_limit INTEGER NOT NULL CHECK(repair_limit BETWEEN 0 AND 2),
    run_state_version INTEGER NOT NULL CHECK(run_state_version>=0),
    created_at TEXT NOT NULL,
    UNIQUE(call_id,run_id)
) STRICT;
CREATE TRIGGER model_generations_no_update BEFORE UPDATE ON model_generations
BEGIN SELECT RAISE(ABORT,'model generation is immutable'); END;
CREATE TRIGGER model_generations_no_delete BEFORE DELETE ON model_generations
BEGIN SELECT RAISE(ABORT,'model generation must be retained'); END;
CREATE TRIGGER model_generations_no_replace BEFORE INSERT ON model_generations
WHEN EXISTS(SELECT 1 FROM model_generations WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT,'model generation already exists'); END;
CREATE TABLE model_attempts (
    request_id TEXT PRIMARY KEY NOT NULL CHECK(length(request_id) BETWEEN 1 AND 200),
    call_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 3),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'STARTED' CHECK(status IN ('STARTED','VALID','INVALID','ERROR','CANCELLED')),
    record_json TEXT CHECK(record_json IS NULL OR (json_valid(record_json) AND json_type(record_json)='object')),
    diagnostic_subtype TEXT,
    FOREIGN KEY(call_id,run_id) REFERENCES model_generations(call_id,run_id),
    UNIQUE(call_id,attempt_number),
    CHECK((status='STARTED' AND finished_at IS NULL AND record_json IS NULL AND diagnostic_subtype IS NULL)
       OR (status<>'STARTED' AND finished_at IS NOT NULL AND finished_at>=started_at AND record_json IS NOT NULL)),
    CHECK(record_json IS NULL OR COALESCE((
        json_extract(record_json,'$.run_id') IS run_id
        AND json_extract(record_json,'$.request_id') IS request_id
        AND json_extract(record_json,'$.format_repairs') IS attempt_number-1
        AND json_type(record_json,'$.duration_ms') IS 'integer'
        AND json_extract(record_json,'$.duration_ms')>=0
        AND json_type(record_json,'$.usage') IS 'object'
        AND ((status IN ('VALID','CANCELLED') AND json_type(record_json,'$.error_class') IS 'null')
          OR (status='INVALID' AND json_extract(record_json,'$.error_class') IS 'invalid_output')
          OR (status='ERROR' AND json_extract(record_json,'$.error_class') IN ('timeout','rate_limit','invalid_credentials','provider_error')))
    ),0))
) STRICT;
CREATE UNIQUE INDEX model_attempts_one_pending_per_run ON model_attempts(run_id) WHERE status='STARTED';
CREATE INDEX model_attempts_run ON model_attempts(run_id,started_at,request_id);
CREATE TRIGGER model_attempts_insert_guard BEFORE INSERT ON model_attempts
WHEN NEW.status<>'STARTED'
 OR NEW.attempt_number<>(SELECT count(*)+1 FROM model_attempts WHERE call_id=NEW.call_id)
 OR NEW.attempt_number>(SELECT repair_limit+1 FROM model_generations WHERE call_id=NEW.call_id)
 OR (NEW.attempt_number>1 AND NOT EXISTS(SELECT 1 FROM model_attempts WHERE call_id=NEW.call_id AND attempt_number=NEW.attempt_number-1 AND status='INVALID'))
BEGIN SELECT RAISE(ABORT,'invalid model attempt sequence'); END;
CREATE TRIGGER model_attempts_finalize_guard BEFORE UPDATE ON model_attempts
WHEN OLD.status<>'STARTED' OR NEW.status='STARTED'
 OR NEW.request_id IS NOT OLD.request_id OR NEW.call_id IS NOT OLD.call_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.attempt_number IS NOT OLD.attempt_number
 OR NEW.started_at IS NOT OLD.started_at
 OR json_extract(NEW.record_json,'$.config_sha256') IS NOT (SELECT config_sha256 FROM model_generations WHERE call_id=OLD.call_id)
 OR json_extract(NEW.record_json,'$.prompt_version') IS NOT (SELECT prompt_version FROM model_generations WHERE call_id=OLD.call_id)
BEGIN SELECT RAISE(ABORT,'model attempt can only be finalized once'); END;
CREATE TRIGGER model_attempts_no_delete BEFORE DELETE ON model_attempts
BEGIN SELECT RAISE(ABORT,'model attempt must be retained'); END;
CREATE TRIGGER model_attempts_no_replace BEFORE INSERT ON model_attempts
WHEN EXISTS(SELECT 1 FROM model_attempts WHERE request_id=NEW.request_id OR (call_id=NEW.call_id AND attempt_number=NEW.attempt_number))
BEGIN SELECT RAISE(ABORT,'model attempt already exists'); END;
