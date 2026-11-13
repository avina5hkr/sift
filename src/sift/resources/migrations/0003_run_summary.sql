-- 0003_run_summary: one row per finished backup run (the "what happened" of the last backup).
-- A new table only: migrations cannot rebuild tables.

CREATE TABLE run_summary (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL UNIQUE,                         -- the `job` row this run was
  device_id INTEGER,                                      -- the phone, when it was known
  sets TEXT,                                              -- JSON list of the set names run
  outcome TEXT NOT NULL CHECK (outcome IN ('done','stopped','failed')),
  started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL,
  new_files INTEGER NOT NULL DEFAULT 0,                   -- copied to the Mac
  duplicates_skipped INTEGER NOT NULL DEFAULT 0,          -- same content already stored
  failed INTEGER NOT NULL DEFAULT 0,
  moved_links INTEGER NOT NULL DEFAULT 0,                 -- moved on the phone, nothing copied
  changed_files INTEGER NOT NULL DEFAULT 0,               -- changed mid-copy, left for next time
  bytes INTEGER NOT NULL DEFAULT 0,                       -- bytes of the new files
  warnings TEXT);                                         -- JSON list, plain words
