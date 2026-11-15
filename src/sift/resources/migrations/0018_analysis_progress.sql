-- 0018_analysis_progress: a running analysis saves its result so far (plan A5), so the screen
-- can show "Checking now: 2,200 of 51,106" beside the last complete result, and a stopped run
-- keeps what it found apart from it. Columns only.

ALTER TABLE analysis_run ADD COLUMN processed INTEGER;
ALTER TABLE analysis_run ADD COLUMN total INTEGER;
ALTER TABLE analysis_run ADD COLUMN partial TEXT;
