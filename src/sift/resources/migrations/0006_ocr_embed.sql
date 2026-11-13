-- 0006_ocr_embed: which version of OCR (stage F) and of the embedding model (stage G) produced a
-- content's text and embedding, so each stage reruns only when its own version changes.
ALTER TABLE content ADD COLUMN ocr_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE content ADD COLUMN embed_version INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_cluster_member_hash ON cluster_member (content_hash);
