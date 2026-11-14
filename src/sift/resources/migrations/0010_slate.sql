-- 0010_slate: Clean slate plans (spec 10A). A plan keeps its rules and the owner's manual keeps;
-- the decision for each file is recomputed from them, so a resumed plan skips what is gone.
CREATE TABLE slate_plan (
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES device(id),
  rules TEXT NOT NULL,                                     -- JSON
  created_at INTEGER NOT NULL,
  run_id INTEGER);                                         -- the latest delete run, if any
CREATE TABLE slate_keep (
  plan_id INTEGER NOT NULL REFERENCES slate_plan(id),
  phone_file_id INTEGER,
  folder TEXT,                                             -- a phone folder kept whole
  CHECK ((phone_file_id IS NULL) != (folder IS NULL)));
CREATE INDEX ix_slate_keep_plan ON slate_keep (plan_id);
ALTER TABLE delete_item ADD COLUMN quarantine INTEGER NOT NULL DEFAULT 0;
