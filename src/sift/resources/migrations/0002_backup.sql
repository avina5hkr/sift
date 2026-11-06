-- 0002_backup: backup scan staging, set tags, two-part phone identity.
-- Only ALTER TABLE ... ADD COLUMN and new tables: migrations cannot rebuild tables.

-- A phone is known by two identifiers, either of which may be missing or change on its own.
-- `serial` keeps holding ro.serialno (else the Android ID, as before).
ALTER TABLE device ADD COLUMN android_id TEXT;

-- Which backup set a phone file belongs to, and the destination path chosen for it (stored
-- once, so a later change to the set's naming never moves an already-backed-up file).
ALTER TABLE phone_file ADD COLUMN set_name TEXT;
ALTER TABLE phone_file ADD COLUMN rel_path TEXT;
CREATE INDEX ix_phone_file_set ON phone_file(device_id, set_name, present);

-- A scan is staged first and applied only when the mass-disappearance guard passes.
-- `state` defaults to 'applied' because SQLite accepts a CHECK in ADD COLUMN only with a
-- constant default, and the scans that exist before this migration are finished history.
-- The scanner inserts new scans with state 'running' explicitly.
ALTER TABLE scan ADD COLUMN sets TEXT;                    -- JSON: the sets scanned
ALTER TABLE scan ADD COLUMN state TEXT NOT NULL DEFAULT 'applied'
  CHECK (state IN ('running','awaiting_confirmation','applied','discarded'));
ALTER TABLE scan ADD COLUMN warnings TEXT;                -- JSON list

CREATE TABLE scan_entry (                                 -- the staged result of one scan
  scan_id INTEGER NOT NULL REFERENCES scan(id),
  path TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
  set_name TEXT NOT NULL, folder TEXT NOT NULL,
  PRIMARY KEY (scan_id, path));

CREATE TABLE folder_count (                               -- last applied count per folder
  device_id INTEGER NOT NULL REFERENCES device(id),
  folder TEXT NOT NULL, set_name TEXT NOT NULL,
  present_files INTEGER NOT NULL,
  scan_id INTEGER NOT NULL,
  PRIMARY KEY (device_id, folder));
