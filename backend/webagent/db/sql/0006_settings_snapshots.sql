-- M1-07: append-only nonsecret settings; credentials live in the OS store.
CREATE TABLE model_settings_versions (
    version INTEGER PRIMARY KEY CHECK(version>0),
    model_json TEXT NOT NULL CHECK(json_valid(model_json) AND json_type(model_json)='object'),
    model_config_sha256 TEXT NOT NULL CHECK(length(model_config_sha256)=64 AND model_config_sha256 NOT GLOB '*[^0-9a-f]*'),
    credential_ref TEXT CHECK(credential_ref IS NULL OR length(credential_ref)=36),
    runtime_json TEXT NOT NULL CHECK(json_valid(runtime_json) AND json_type(runtime_json)='object'),
    runtime_config_sha256 TEXT NOT NULL UNIQUE CHECK(length(runtime_config_sha256)=64 AND runtime_config_sha256 NOT GLOB '*[^0-9a-f]*'),
    disclosure_version TEXT NOT NULL CHECK(disclosure_version='model-data-v1'),
    created_at TEXT NOT NULL,
    UNIQUE(version,model_config_sha256,runtime_config_sha256),
    CHECK(json_extract(runtime_json,'$.settings_version') IS version),
    CHECK(json_extract(runtime_json,'$.credential_ref') IS credential_ref),
    CHECK(json_extract(runtime_json,'$.model_config_sha256') IS model_config_sha256),
    CHECK(json_extract(runtime_json,'$.disclosure_version') IS disclosure_version)
) STRICT;
CREATE TRIGGER model_settings_version_sequence BEFORE INSERT ON model_settings_versions
WHEN NEW.version<>(SELECT COALESCE(MAX(version),0)+1 FROM model_settings_versions)
BEGIN SELECT RAISE(ABORT,'settings versions must be consecutive'); END;
CREATE TRIGGER model_settings_versions_no_update BEFORE UPDATE ON model_settings_versions
BEGIN SELECT RAISE(ABORT,'settings snapshots are immutable'); END;
CREATE TRIGGER model_settings_versions_no_delete BEFORE DELETE ON model_settings_versions
BEGIN SELECT RAISE(ABORT,'settings history must be retained'); END;
CREATE TRIGGER model_settings_versions_no_replace BEFORE INSERT ON model_settings_versions
WHEN EXISTS(SELECT 1 FROM model_settings_versions WHERE version=NEW.version)
BEGIN SELECT RAISE(ABORT,'settings version already exists'); END;
CREATE TABLE run_config_snapshots (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES runs(run_id),
    settings_version INTEGER NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    runtime_config_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(settings_version,model_config_sha256,runtime_config_sha256)
        REFERENCES model_settings_versions(version,model_config_sha256,runtime_config_sha256)
) STRICT;
CREATE TRIGGER run_config_snapshots_match BEFORE INSERT ON run_config_snapshots
WHEN NOT EXISTS(SELECT 1 FROM runs WHERE run_id=NEW.run_id
 AND model_config_sha256=NEW.model_config_sha256 AND runtime_config_sha256=NEW.runtime_config_sha256)
BEGIN SELECT RAISE(ABORT,'run configuration hashes do not match snapshot'); END;
CREATE TRIGGER run_config_snapshots_no_update BEFORE UPDATE ON run_config_snapshots
BEGIN SELECT RAISE(ABORT,'run configuration binding is immutable'); END;
CREATE TRIGGER run_config_snapshots_no_delete BEFORE DELETE ON run_config_snapshots
BEGIN SELECT RAISE(ABORT,'run configuration binding must be retained'); END;
CREATE TRIGGER run_config_snapshots_no_replace BEFORE INSERT ON run_config_snapshots
WHEN EXISTS(SELECT 1 FROM run_config_snapshots WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'run configuration already bound'); END;
