-- 0015: a failed text reading is not a reading (external review, P2.5). Until now a reader
-- failure was stored like "read, no text" (ocr_version set, ocr_text and ocr_lang empty), so
-- document protection counted as checked. Those images are read again; failures are recorded.
ALTER TABLE content ADD COLUMN ocr_error TEXT;
UPDATE content SET ocr_version = 0, ocr_error = 'not read (earlier failure)'
WHERE kind = 'image' AND ocr_version >= 1 AND ocr_text IS NULL AND ocr_lang IS NULL;
