-- Preparation calls have no Run: reserve once before HTTP, retain unknown outcomes.
CREATE TABLE task_compilations (
    call_id TEXT PRIMARY KEY,
    request_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    reserved_task_id TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(task_id),
    expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
    settings_version INTEGER NOT NULL REFERENCES model_settings_versions(version),
    model_config_sha256 TEXT NOT NULL CHECK(length(model_config_sha256)=64),
    runtime_config_sha256 TEXT NOT NULL CHECK(length(runtime_config_sha256)=64),
    prompt_version TEXT NOT NULL CHECK(prompt_version='m1-06-compiler-v1'),
    status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','FAILED','CANCELLED')),
    provider_request_id TEXT,
    usage_json TEXT CHECK(usage_json IS NULL OR (json_valid(usage_json) AND json_type(usage_json)='object')),
    extracted_fields_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(extracted_fields_json) AND json_type(extracted_fields_json)='array'),
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
    error_class TEXT CHECK(error_class IN ('timeout','rate_limit','invalid_credentials','invalid_output','provider_error','cancelled','state_conflict','invalid_parameter')),
    response_status INTEGER,
    response_json TEXT CHECK(response_json IS NULL OR (json_valid(response_json) AND json_type(response_json)='object')),
    retry_after_seconds REAL CHECK(retry_after_seconds IS NULL OR retry_after_seconds>=0),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(request_scope,idempotency_key),
    CHECK((status='STARTED' AND finished_at IS NULL AND response_status IS NULL AND response_json IS NULL)
       OR (status<>'STARTED' AND finished_at IS NOT NULL AND response_status IS NOT NULL AND response_json IS NOT NULL)),
    CHECK(status<>'SUCCEEDED' OR (task_id IS NOT NULL AND error_class IS NULL AND response_status IN (200,201))),
    CHECK(status NOT IN ('FAILED','CANCELLED') OR (error_class IS NOT NULL AND response_status>=400))
) STRICT;
CREATE UNIQUE INDEX task_compilations_one_pending ON task_compilations(task_id) WHERE status='STARTED' AND task_id IS NOT NULL;
CREATE TRIGGER task_compilations_snapshot BEFORE INSERT ON task_compilations
WHEN NOT EXISTS(SELECT 1 FROM model_settings_versions WHERE version=NEW.settings_version
 AND model_config_sha256=NEW.model_config_sha256 AND runtime_config_sha256=NEW.runtime_config_sha256) BEGIN
 SELECT RAISE(ABORT,'compilation snapshot must match its settings version');
END;
CREATE TRIGGER task_compilations_no_delete BEFORE DELETE ON task_compilations BEGIN
 SELECT RAISE(ABORT,'compilation records must be retained');
END;
CREATE TRIGGER task_compilations_no_replace BEFORE INSERT ON task_compilations
WHEN EXISTS(SELECT 1 FROM task_compilations WHERE call_id=NEW.call_id OR
 (request_scope=NEW.request_scope AND idempotency_key=NEW.idempotency_key)) BEGIN
 SELECT RAISE(ABORT,'compilation reservation already exists');
END;
CREATE TRIGGER task_compilations_final_once BEFORE UPDATE ON task_compilations
WHEN OLD.status<>'STARTED' OR NEW.status='STARTED'
 OR NEW.call_id<>OLD.call_id OR NEW.request_scope<>OLD.request_scope
 OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.request_sha256<>OLD.request_sha256
 OR NEW.reserved_task_id<>OLD.reserved_task_id OR NEW.expected_revision<>OLD.expected_revision
 OR NEW.settings_version<>OLD.settings_version OR NEW.model_config_sha256<>OLD.model_config_sha256
 OR NEW.runtime_config_sha256<>OLD.runtime_config_sha256 OR NEW.prompt_version<>OLD.prompt_version
 OR NEW.created_at<>OLD.created_at
 OR (OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id)
 OR (NEW.task_id IS NOT NULL AND NEW.task_id<>NEW.reserved_task_id) BEGIN
 SELECT RAISE(ABORT,'compilation can only finish its reserved request once');
END;
