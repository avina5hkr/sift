-- 0007_calibration: the current calibration sample (spec 9, Calibration). One row per sampled
-- content; verdict is NULL until the owner marks it Correct (1) or Wrong (0).
CREATE TABLE calibration_item (content_hash TEXT PRIMARY KEY REFERENCES content(hash),
  phone_file_id INTEGER, score REAL, verdict INTEGER, model_version INTEGER NOT NULL);
