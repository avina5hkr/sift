-- 0005_adopt: adoption of existing backup folders, the Problems list and backup confirmations.

-- Every file found by adoption or a rebuild walk. A file counts as backed up only once it is
-- hashed and has a `replica` row (I2); until then it is 'provisional'. Other copies of content
-- that already has a replica on the same destination stay 'duplicate' and are never touched.
CREATE TABLE adopted_file (
  id INTEGER PRIMARY KEY,
  dest_id INTEGER NOT NULL REFERENCES destination(id),
  rel_path TEXT NOT NULL,
  rel_key TEXT NOT NULL,                                  -- NFC + casefold of rel_path
  size INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  hash TEXT,                                              -- SHA-256 hex once hashed
  state TEXT NOT NULL DEFAULT 'provisional'
    CHECK (state IN ('provisional', 'indexed', 'duplicate', 'unreadable')),
  found_at INTEGER NOT NULL,
  UNIQUE (dest_id, rel_key));
CREATE INDEX ix_adopted_file_state ON adopted_file (dest_id, state);
CREATE INDEX ix_adopted_file_size ON adopted_file (size);

-- Backup, Problems: things the owner should know about. Nothing here is ever deleted by Sift.
CREATE TABLE problem (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL
    CHECK (kind IN ('hash_mismatch', 'missing_file', 'older_copy_differs', 'unreadable')),
  dest_id INTEGER REFERENCES destination(id),
  rel_path TEXT,
  content_hash TEXT,
  detail TEXT,
  created_at INTEGER NOT NULL,
  resolved_at INTEGER);
CREATE INDEX ix_problem_open ON problem (resolved_at, kind);

-- A dated snapshot: the set was fully backed up and verified when it was confirmed.
CREATE TABLE confirmation (
  id INTEGER PRIMARY KEY,
  set_name TEXT NOT NULL,
  device_id INTEGER REFERENCES device(id),
  created_at INTEGER NOT NULL,
  files INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  confirmed INTEGER NOT NULL,                             -- 1 only when every check passed
  report TEXT,                                            -- JSON of the per-set report
  report_path TEXT);                                      -- the Markdown copy, when written
CREATE INDEX ix_confirmation_set ON confirmation (device_id, set_name, created_at);

CREATE INDEX IF NOT EXISTS ix_replica_dest ON replica (dest_id);
CREATE INDEX IF NOT EXISTS ix_content_size ON content (size);

-- When the phone's own SHA-256 of this file last equalled content_hash, and the file's mtime
-- then; a confirmation re-asks the phone only for files changed since (or never checked).
ALTER TABLE phone_file ADD COLUMN phone_hash_at INTEGER;
ALTER TABLE phone_file ADD COLUMN phone_hash_mtime INTEGER;

-- Analyse phone (read-only): one row per run, with its per-set groups as JSON.
CREATE TABLE analysis_run (
  id INTEGER PRIMARY KEY,
  device_id INTEGER REFERENCES device(id),
  sets TEXT NOT NULL,                                     -- JSON list of set names
  state TEXT NOT NULL CHECK (state IN ('running', 'done', 'stopped', 'failed')),
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  result TEXT,                                            -- JSON: per-set groups, counts, bytes
  error TEXT);
