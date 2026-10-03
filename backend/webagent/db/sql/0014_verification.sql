-- Runtime verification is append-only. Only the business aggregator commits a result.
CREATE TABLE run_verifications (
    verification_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state_version INTEGER NOT NULL CHECK(state_version>=0),
    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    created_at TEXT NOT NULL,
    UNIQUE(run_id,verification_id)
) STRICT;
CREATE INDEX run_verifications_run ON run_verifications(run_id,created_at);
CREATE TABLE run_results (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    verification_id TEXT NOT NULL UNIQUE,
    state_version INTEGER NOT NULL CHECK(state_version>0),
    outcome TEXT NOT NULL CHECK(outcome IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,verification_id) REFERENCES run_verifications(run_id,verification_id),
    CHECK(json_extract(result_json,'$.run_id') IS run_id),
    CHECK(json_extract(result_json,'$.outcome') IS outcome),
    CHECK(json_extract(result_json,'$.generated_by') IS 'business_aggregator')
) STRICT;
CREATE TRIGGER run_verifications_no_replace BEFORE INSERT ON run_verifications
WHEN EXISTS(SELECT 1 FROM run_verifications WHERE verification_id=NEW.verification_id)
BEGIN SELECT RAISE(ABORT,'verification identity is immutable'); END;
CREATE TRIGGER run_verifications_no_update BEFORE UPDATE ON run_verifications
BEGIN SELECT RAISE(ABORT,'verification is immutable'); END;
CREATE TRIGGER run_verifications_no_delete BEFORE DELETE ON run_verifications
BEGIN SELECT RAISE(ABORT,'verification history must remain'); END;
CREATE TRIGGER run_verifications_binding BEFORE INSERT ON run_verifications
WHEN NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id AND r.state='VERIFYING'
 AND r.state_version=NEW.state_version AND r.contract_sha256=NEW.contract_sha256)
BEGIN SELECT RAISE(ABORT,'verification run binding mismatch'); END;
CREATE TRIGGER run_results_no_replace BEFORE INSERT ON run_results
WHEN EXISTS(SELECT 1 FROM run_results WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'result identity is immutable'); END;
CREATE TRIGGER run_results_no_update BEFORE UPDATE ON run_results
BEGIN SELECT RAISE(ABORT,'result is immutable'); END;
CREATE TRIGGER run_results_no_delete BEFORE DELETE ON run_results
BEGIN SELECT RAISE(ABORT,'result history must remain'); END;
CREATE TRIGGER run_results_binding BEFORE INSERT ON run_results
WHEN NOT EXISTS(SELECT 1 FROM runs r JOIN run_verifications v USING(run_id)
 WHERE r.run_id=NEW.run_id AND v.verification_id=NEW.verification_id
 AND r.state='VERIFYING' AND v.state_version=r.state_version
 AND NEW.state_version=r.state_version+1 AND v.contract_sha256=r.contract_sha256
 AND json_extract(NEW.result_json,'$.task_id')=r.task_id
 AND json_extract(NEW.result_json,'$.contract_version')=r.contract_version
 AND json_extract(NEW.result_json,'$.assistance_count')=r.assistance_count)
BEGIN SELECT RAISE(ABORT,'result run binding mismatch'); END;
-- Pre-M1-04 structural migration fixtures have no executable contract criteria.
-- Every compiled application contract must use the aggregator, even via raw SQL.
CREATE TRIGGER runs_require_aggregator BEFORE UPDATE OF state ON runs
WHEN NEW.state IN ('SUCCEEDED','PARTIAL') AND EXISTS(
 SELECT 1 FROM contracts c WHERE c.task_id=NEW.task_id AND c.contract_version=NEW.contract_version
 AND json_type(c.content_json,'$.acceptance_criteria')='array')
 AND NOT EXISTS(SELECT 1 FROM run_results x WHERE x.run_id=NEW.run_id
 AND x.state_version=NEW.state_version AND x.outcome=NEW.state)
BEGIN SELECT RAISE(ABORT,'terminal output requires business aggregator'); END;
