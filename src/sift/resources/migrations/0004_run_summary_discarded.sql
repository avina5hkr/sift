-- 0004_run_summary_discarded: a run whose scan the owner discarded copied nothing on purpose.
-- It is a flag, not a new `outcome`: SQLite cannot widen the CHECK on `outcome` without
-- rebuilding the table, and migrations only add. Such a run is stored as outcome 'stopped' with
-- discarded = 1, and the API shows its outcome as 'discarded'.

ALTER TABLE run_summary ADD COLUMN discarded INTEGER NOT NULL DEFAULT 0;
