-- 0009_label_import: teaching loaded from a file for pictures Sift has not seen yet. Each row
-- becomes a label as soon as its content is known (after a backup and analysis).
CREATE TABLE label_import (content_hash TEXT PRIMARY KEY,
  label TEXT NOT NULL CHECK (label IN ('junk','keep','protected')),
  tag TEXT, created_at INTEGER NOT NULL);
