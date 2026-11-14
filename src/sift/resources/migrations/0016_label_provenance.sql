-- 0016: loaded teaching keeps where each label came from (external review, P3.12): a label the
-- owner gave a whole group stays one group decision instead of becoming hundreds of single ones.
ALTER TABLE label_import ADD COLUMN source TEXT;
ALTER TABLE label_import ADD COLUMN group_key TEXT;
