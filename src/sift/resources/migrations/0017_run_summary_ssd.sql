-- 0017_run_summary_ssd: how the SSD part of a backup run ended (v0.0.2, plan A2).
-- A run ends `done` when the pull completed, whatever the SSD did; these columns say what that
-- was. ssd_state: none (no SSD chosen), complete, incomplete (unplugged, full, files deferred),
-- stopped, failed. Columns only: migrations cannot rebuild tables.

ALTER TABLE run_summary ADD COLUMN pull_finished_at INTEGER;
ALTER TABLE run_summary ADD COLUMN ssd_state TEXT NOT NULL DEFAULT 'none'
  CHECK (ssd_state IN ('none','complete','incomplete','stopped','failed'));
ALTER TABLE run_summary ADD COLUMN ssd_copied INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_summary ADD COLUMN ssd_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_summary ADD COLUMN ssd_deferred INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_summary ADD COLUMN ssd_pending_after INTEGER;
ALTER TABLE run_summary ADD COLUMN ssd_finished_at INTEGER;
ALTER TABLE run_summary ADD COLUMN ssd_reason TEXT;
