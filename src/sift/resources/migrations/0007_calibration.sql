-- 0007_calibration: the current calibration sample (spec 9, Calibration), one row per sampled
-- content; verdict is NULL until the owner marks it Correct (1) or Wrong (0). Candidates get
-- one row per phone file and tab, so a file that is an exact duplicate can also be junk.
CREATE TABLE calibration_item (content_hash TEXT PRIMARY KEY REFERENCES content(hash),
  phone_file_id INTEGER, score REAL, verdict INTEGER, model_version INTEGER NOT NULL);

CREATE TABLE candidate_new (
  phone_file_id INTEGER NOT NULL REFERENCES phone_file(id),
  tab TEXT NOT NULL CHECK (tab IN ('exact','near','junk','maybe','similar')),
  tier TEXT NOT NULL CHECK (tier IN ('suggested','maybe','manual')),
  junk_score REAL, support INTEGER,
  reasons TEXT,
  group_id INTEGER, role TEXT,
  default_checked INTEGER NOT NULL DEFAULT 0, model_version INTEGER,
  PRIMARY KEY (phone_file_id, tab));
INSERT INTO candidate_new SELECT phone_file_id, tab, tier, junk_score, support, reasons,
  group_id, role, default_checked, model_version FROM candidate;
DROP TABLE candidate;
ALTER TABLE candidate_new RENAME TO candidate;
