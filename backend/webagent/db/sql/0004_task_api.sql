-- M1-04: immutable preparation revisions and transactionally recorded HTTP replies.
CREATE TABLE task_revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    parent_revision INTEGER,
    kind TEXT NOT NULL CHECK(kind IN ('create','clarification','revision')),
    request_json TEXT NOT NULL CHECK(json_valid(request_json) AND json_type(request_json)='object'),
    submitted_json TEXT NOT NULL CHECK(json_valid(submitted_json) AND json_type(submitted_json)='object'),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    missing_fields_json TEXT NOT NULL CHECK(json_valid(missing_fields_json) AND json_type(missing_fields_json)='array'),
    contract_version INTEGER,
    provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json) AND json_type(provenance_json)='array'),
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    PRIMARY KEY(task_id,revision),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(task_id,parent_revision) REFERENCES task_revisions(task_id,revision),
    FOREIGN KEY(task_id,contract_version) REFERENCES contracts(task_id,contract_version),
    CHECK((revision=1 AND parent_revision IS NULL AND kind='create') OR
          (revision>1 AND parent_revision IS NOT NULL AND parent_revision=revision-1 AND kind<>'create')),
    CHECK((contract_version IS NULL AND json_array_length(missing_fields_json)>0) OR
          (contract_version IS NOT NULL AND contract_version=revision AND json_array_length(missing_fields_json)=0))
) STRICT;
CREATE TRIGGER task_revisions_no_update BEFORE UPDATE ON task_revisions BEGIN
 SELECT RAISE(ABORT,'task revisions are immutable');
END;
CREATE TRIGGER task_revisions_no_delete BEFORE DELETE ON task_revisions BEGIN
 SELECT RAISE(ABORT,'task revision history must be retained');
END;
CREATE TRIGGER task_revisions_no_replace BEFORE INSERT ON task_revisions
WHEN EXISTS(SELECT 1 FROM task_revisions WHERE task_id=NEW.task_id AND revision=NEW.revision) BEGIN
 SELECT RAISE(ABORT,'task revision already exists');
END;
CREATE TABLE api_idempotency (
    request_scope TEXT NOT NULL CHECK(length(request_scope) BETWEEN 1 AND 500),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    response_status INTEGER NOT NULL CHECK(response_status IN (200,201)),
    response_json TEXT NOT NULL CHECK(json_valid(response_json) AND json_type(response_json)='object'),
    created_at TEXT NOT NULL CHECK(created_at IS NULL OR (length(created_at)=27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND strftime('%Y-%m-%dT%H:%M:%fZ', substr(created_at,1,23)||'Z') IS substr(created_at,1,23)||'Z')),
    PRIMARY KEY(request_scope,idempotency_key)
) STRICT;
CREATE TRIGGER api_idempotency_no_update BEFORE UPDATE ON api_idempotency BEGIN
 SELECT RAISE(ABORT,'idempotency response is immutable');
END;
CREATE TRIGGER api_idempotency_no_delete BEFORE DELETE ON api_idempotency BEGIN
 SELECT RAISE(ABORT,'idempotency response must be retained');
END;
CREATE TRIGGER api_idempotency_no_replace BEFORE INSERT ON api_idempotency
WHEN EXISTS(SELECT 1 FROM api_idempotency WHERE request_scope=NEW.request_scope AND idempotency_key=NEW.idempotency_key) BEGIN
 SELECT RAISE(ABORT,'idempotency key already used');
END;
