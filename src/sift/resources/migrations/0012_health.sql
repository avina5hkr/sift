-- 0012_health: health reports and the pre-reset checklist ticks (spec 10D).
CREATE TABLE health_report (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES device(id),
    created_at INTEGER NOT NULL,
    report TEXT NOT NULL
);
CREATE INDEX health_report_device ON health_report (device_id, created_at);
CREATE TABLE reset_check (
    device_id INTEGER NOT NULL REFERENCES device(id),
    item INTEGER NOT NULL,
    ticked INTEGER NOT NULL DEFAULT 0,
    done_on TEXT,
    note TEXT,
    at INTEGER NOT NULL,
    PRIMARY KEY (device_id, item)
);
