"""SQLite schema for the job store.

The status-transition trigger is the database-level guard for the job state
machine; keep it in sync with JOB_STATES in job_store.py.
"""

JOBS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN (
        'preparing','queued','running','completed','failed','cancelled','interrupted'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    meta_json TEXT NOT NULL CHECK (json_valid(meta_json)),
    desired_action TEXT CHECK (desired_action IN ('run','cancel','adopt','resume') OR desired_action IS NULL),
    worker_id TEXT,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_pending_action_idx ON jobs(created_at) WHERE desired_action IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_pinned_created_id_idx
    ON jobs(coalesce(json_extract(meta_json,'$.pinned'),0) DESC,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS jobs_revision (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO jobs_revision VALUES(1,0);
CREATE TRIGGER IF NOT EXISTS jobs_revision_insert AFTER INSERT ON jobs
BEGIN UPDATE jobs_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER IF NOT EXISTS jobs_revision_update AFTER UPDATE ON jobs
BEGIN UPDATE jobs_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER IF NOT EXISTS jobs_revision_delete AFTER DELETE ON jobs
BEGIN UPDATE jobs_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TABLE IF NOT EXISTS store_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_reports (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS protocol_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    UNIQUE(name, revision)
);
CREATE TABLE IF NOT EXISTS protocol_template_revisions (
    name TEXT PRIMARY KEY,
    last_revision INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS jobs_status_transition
BEFORE UPDATE OF status ON jobs
WHEN OLD.status <> NEW.status AND NOT (
    (OLD.status = 'preparing' AND NEW.status IN ('queued','failed','cancelled')) OR
    (OLD.status = 'queued' AND NEW.status IN ('running','cancelled','interrupted')) OR
    (OLD.status = 'running' AND NEW.status IN ('completed','failed','cancelled','interrupted')) OR
    (OLD.status = 'interrupted' AND NEW.status IN ('queued','running','cancelled')) OR
    (OLD.status = 'failed' AND NEW.status IN ('queued','cancelled'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid job status transition');
END;
"""

# Index identities needing verification without rescanning terminal history.
# Triggers also track writes by older workers, which do not know this registry.
PROCESS_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS active_job_processes (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TRIGGER IF NOT EXISTS job_process_insert AFTER INSERT ON jobs
WHEN json_extract(NEW.meta_json,'$.process_pid') IS NOT NULL
  OR json_array_length(json_extract(NEW.meta_json,'$.process_identities')) > 0
BEGIN INSERT OR IGNORE INTO active_job_processes VALUES(NEW.id); END;
CREATE TRIGGER IF NOT EXISTS job_process_update AFTER UPDATE OF meta_json ON jobs
WHEN json_extract(NEW.meta_json,'$.process_pid') IS NOT json_extract(OLD.meta_json,'$.process_pid')
  OR json_extract(NEW.meta_json,'$.process_pgid') IS NOT json_extract(OLD.meta_json,'$.process_pgid')
  OR json_extract(NEW.meta_json,'$.process_cwd') IS NOT json_extract(OLD.meta_json,'$.process_cwd')
  OR json_extract(NEW.meta_json,'$.process_started_ticks') IS NOT json_extract(OLD.meta_json,'$.process_started_ticks')
  OR json_extract(NEW.meta_json,'$.process_command_fingerprint') IS NOT json_extract(OLD.meta_json,'$.process_command_fingerprint')
  OR json_extract(NEW.meta_json,'$.process_identities') IS NOT json_extract(OLD.meta_json,'$.process_identities')
BEGIN INSERT OR IGNORE INTO active_job_processes VALUES(NEW.id); END;
"""
