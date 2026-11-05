CREATE TABLE device (
  id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL,      -- app-generated
  serial TEXT, label TEXT, model TEXT, android_version TEXT, last_seen_at INTEGER);

CREATE TABLE destination (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('mac','ssd')),
  root_path TEXT NOT NULL, volume_uuid TEXT, label TEXT,
  required INTEGER NOT NULL DEFAULT 1);

CREATE TABLE content (                                    -- one row per distinct file content
  hash TEXT PRIMARY KEY,                                  -- SHA-256 hex
  size INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('image','video','audio','doc','other')),
  mime TEXT, width INTEGER, height INTEGER, duration_ms INTEGER,
  has_camera_exif INTEGER NOT NULL DEFAULT 0,
  is_screenshot INTEGER NOT NULL DEFAULT 0,
  unreadable INTEGER NOT NULL DEFAULT 0,
  dhash INTEGER, phash INTEGER,                           -- 64-bit, stored signed
  vframes TEXT,                                           -- JSON list of 5 dHashes (video)
  ocr_text TEXT, ocr_lang TEXT, text_density REAL,
  embedding BLOB,                                         -- float16 x 512, L2-normalised
  analysis_version INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);

CREATE TABLE phone_file (                                 -- one row per path on the phone
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES device(id),
  remote_path TEXT NOT NULL, origin TEXT NOT NULL,        -- camera, screenshot, whatsapp_*, other
  size INTEGER NOT NULL, mtime INTEGER NOT NULL,
  content_hash TEXT REFERENCES content(hash),
  state TEXT NOT NULL CHECK (state IN
    ('discovered','pulling','done','failed','skipped','deleted')),
  present INTEGER NOT NULL DEFAULT 1,                     -- seen in the latest scan
  last_scan_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
  UNIQUE (device_id, remote_path));
CREATE INDEX ix_phone_file_hash  ON phone_file(content_hash);
CREATE INDEX ix_phone_file_state ON phone_file(device_id, state, present);

CREATE TABLE replica (                                    -- verified copy on a destination
  content_hash TEXT NOT NULL REFERENCES content(hash),
  dest_id INTEGER NOT NULL REFERENCES destination(id),
  rel_path TEXT NOT NULL,
  rel_key TEXT NOT NULL,                                  -- NFC + casefold of rel_path
  size INTEGER NOT NULL, verified_at INTEGER NOT NULL,
  PRIMARY KEY (content_hash, dest_id),
  UNIQUE (dest_id, rel_key));

CREATE TABLE scan (id INTEGER PRIMARY KEY, device_id INTEGER, started_at INTEGER,
  finished_at INTEGER, stats TEXT);
CREATE TABLE job (id INTEGER PRIMARY KEY, type TEXT NOT NULL, state TEXT NOT NULL,
  params TEXT, progress TEXT, started_at INTEGER, finished_at INTEGER, error TEXT);

CREATE TABLE near_group (id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                                     -- image | video
  group_type TEXT NOT NULL,                               -- wa_forward | mixed | camera_burst
  algo_version INTEGER NOT NULL);
CREATE TABLE near_member (group_id INTEGER REFERENCES near_group(id),
  content_hash TEXT REFERENCES content(hash),
  role TEXT NOT NULL CHECK (role IN ('keeper','copy')), score REAL,
  PRIMARY KEY (group_id, content_hash));

CREATE TABLE cluster (id INTEGER PRIMARY KEY, algo_version INTEGER NOT NULL,
  size INTEGER NOT NULL, medoid_hash TEXT);
CREATE TABLE cluster_member (cluster_id INTEGER REFERENCES cluster(id),
  content_hash TEXT REFERENCES content(hash), sim REAL,
  PRIMARY KEY (cluster_id, content_hash));

CREATE TABLE label (content_hash TEXT PRIMARY KEY REFERENCES content(hash),
  label TEXT NOT NULL CHECK (label IN ('junk','keep','protected')),
  tag TEXT,                                               -- greeting, quote, meme, devotional, other
  source TEXT NOT NULL CHECK (source IN ('cluster','manual','feedback','calibration')),
  cluster_id INTEGER, created_at INTEGER NOT NULL);

CREATE TABLE calibration (id INTEGER PRIMARY KEY, created_at INTEGER, sample_size INTEGER,
  errors INTEGER, threshold REAL, model_version INTEGER, passed INTEGER);

CREATE TABLE candidate (                                  -- scoring output, rebuilt per analysis
  phone_file_id INTEGER PRIMARY KEY REFERENCES phone_file(id),
  tab TEXT NOT NULL CHECK (tab IN ('exact','near','junk','maybe','similar')),
  tier TEXT NOT NULL CHECK (tier IN ('suggested','maybe','manual')),
  junk_score REAL, support INTEGER,
  reasons TEXT,                                           -- JSON list of {code, text}
  group_id INTEGER, role TEXT,                            -- keeper | copy for duplicate groups
  default_checked INTEGER NOT NULL DEFAULT 0, model_version INTEGER);

CREATE TABLE selection (phone_file_id INTEGER PRIMARY KEY REFERENCES phone_file(id),
  checked INTEGER NOT NULL);

CREATE TABLE deletion_log (id INTEGER PRIMARY KEY, phone_file_id INTEGER,
  remote_path TEXT NOT NULL, content_hash TEXT, size INTEGER,
  deleted_at INTEGER, result TEXT NOT NULL, preflight TEXT);

CREATE TABLE quarantine (id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL,
  dest_id INTEGER NOT NULL, orig_rel TEXT NOT NULL, q_rel TEXT NOT NULL,
  moved_at INTEGER NOT NULL, purge_after INTEGER NOT NULL,
  restored_at INTEGER, purged_at INTEGER);

CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
