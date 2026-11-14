-- 0014: a quarantine restore records where the file is going before it moves, so a crash
-- between the move and the database update is repaired at start (external review, P1.4).
ALTER TABLE quarantine ADD COLUMN restore_to TEXT;
