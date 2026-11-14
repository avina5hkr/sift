-- 0011_apps: protected apps the owner explicitly unprotected (spec 10C, invariant I10).
CREATE TABLE app_unprotect (package TEXT PRIMARY KEY, at INTEGER NOT NULL);
