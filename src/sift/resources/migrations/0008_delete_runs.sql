-- 0008_delete_runs: one row per delete run (preflight, then execute) and one per planned file
-- (spec 10, 10E, 12). check_result is 'ok' or the reason the preflight took the file off.
CREATE TABLE delete_run (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('cleanup', 'browse', 'slate', 'analysis')),
  device_id INTEGER NOT NULL REFERENCES device(id),
  selection_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('preflight', 'ready', 'blocked', 'running', 'done', 'stopped', 'failed')),
  blocked_reason TEXT,
  disposition TEXT NOT NULL CHECK (disposition IN ('archive', 'quarantine')),
  typed_required INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  preflight_done_at INTEGER,
  started_at INTEGER,
  finished_at INTEGER,
  manifests TEXT,                                          -- JSON list of written manifest paths
  passed INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  bytes_freed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE delete_item (
  run_id INTEGER NOT NULL REFERENCES delete_run(id),
  phone_file_id INTEGER NOT NULL REFERENCES phone_file(id),
  remote_path TEXT NOT NULL,
  content_hash TEXT,
  size INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  check_result TEXT NOT NULL DEFAULT 'pending',
  state TEXT NOT NULL DEFAULT 'planned'
    CHECK (state IN ('planned', 'deleted', 'failed', 'gone', 'changed')),
  error TEXT,
  PRIMARY KEY (run_id, phone_file_id));
ALTER TABLE deletion_log ADD COLUMN run_id INTEGER;
