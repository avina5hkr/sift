# v0.0.2 and follow-ups: parallel SSD copy and a clearer Backup page; then SSD-only and profiles; then app names

Working method: `CLAUDE.md`. Source: the owner's first real 117 GB backup (the SSD copy ran only
after the phone copy, showed no progress, ignored Pause, and waited silently behind the macOS
removable-volume prompt; an old analysis looked current; app rows showed package ids), and a
plan review. Three milestones, each reviewable and releasable on its own.

| Milestone | Scope | Why this order |
| --- | --- | --- |
| **A. v0.0.2** | Parallel SSD copy, progress and outcomes, Pause/Stop/shutdown, SSD access check, analysis card, times, Storage locations | Fixes what the owner hit; touches only the copy pipeline and the UI, with no change to the destination policy |
| **B. Follow-up** | SSD-only destination policy; profile migration | Changes which copies are required (I3a) and moves state between profiles: separate review |
| **C. Later** | App display names (APK parsing); performance work deferred by the hardening pass | Independent and lower priority; needs a benchmark first |

---

## Milestone A: v0.0.2

### A1. Coordinator and SSD worker

- **Roles.** The job thread is the **coordinator**. It owns `JobControl` and every job-state
  change from start to end, and runs three phases: *pull* (the existing `Puller` and its reader
  threads), *drain* (waiting for the SSD worker), and *reverse* (SSD → Mac, as `replicate_union`
  does today). One **SSD worker** thread copies Mac → SSD from the start of the pull until it
  completes or is stopped. It never changes job state.
- **The queue is the database.** Work is a Mac replica with no SSD replica. The worker reads it in
  batches of 500 ordered by replica `rowid` after a cursor (`pending_after(cursor, limit)`), so
  new arrivals appear after the cursor and nothing loads the whole backlog. A `threading.Event`
  (`more`) is only a wake-up: the producer rings it after each committed file, and the worker
  waits on it with a 2 s timeout.
- **Eligible versus deferred.** A file that fails on the SSD this run (I/O error, source hash
  mismatch, target clash) goes into the run's `deferred` set with its reason, and is counted and
  reported, never retried in this run. It stays pending in the database for the next run or the
  next plug-in. Eligible work is pending work that is not deferred.
- **Completion: main pass, then one bounded final pass.** `pull_done` is set in a `finally`,
  however the pull ends.
  - *Main pass:* the cursor only moves forward. It ends when `pull_done` is set and a query after
    the cursor returns no rows.
  - *Final pass* (catches a reused `rowid` or a replica re-recorded mid-run): take `high =
    max(rowid)` of the Mac's replicas, start a fresh cursor at 0, and page through `(cursor,
    high]` in batches of 500. Every returned row moves the cursor past it, whether it is copied,
    deferred or already copied, so each row up to `high` is visited exactly once. The pass ends on
    an empty page.
  - *Rows above `high`:* after `pull_done`, nothing else records Mac replicas: the coordinator
    holds the job and the drive lease, and the reverse phase starts only after the drain. As a
    guard, if `max(rowid)` grew during the pass, the worker runs a tail pass over the new rows
    only, at most 3 times. After that it ends `incomplete`, with the remainder counted as pending.
  - *Bound:* at most ⌈rows ≤ `high` / 500⌉ + 1 queries in the final pass, and each file is
    attempted at most once per run, because a success leaves the pending set and a failure is
    deferred.
- **The deferred set cannot grow without limit.** It is keyed by content hash. `ENOSPC` ends the
  worker as `full`. 20 failures in a row end it as `failed` ("the SSD is not accepting writes"),
  so a dying drive never defers the whole backlog.
- **Drive identity follows the UUID, never the name.**
  - The worker resolves the mount from `backup.ssd_volume_uuid` at start. This is what
    `ensure_destination` already does: it follows a renamed mount and keeps the folder on the
    drive.
  - It records the root's `st_dev` and checks it before each batch and before each publish. A
    change triggers a fresh lookup by UUID:
    - the same UUID at a new mount (a renamed or remounted drive): continue at the new path;
    - no such volume: `unplugged`;
    - a different volume at the old path: stop with "A different drive is mounted with this
      name", writing nothing to it.
- **Backlog from earlier runs** (an SSD that was unplugged last time) is part of this run's SSD
  work. The SSD bar shows it as "including 1,200 from earlier runs", and its totals grow as the
  pull adds files ("so far").
- **One writer: the drive lease.** `REPLICATION_LOCK` becomes a `DriveLease` that records its
  holder for display. It covers every writer and mover in the backup folders: the backup's SSD
  worker, plug-in replication, quarantine moves and chat-backup moves.
  - The coordinator acquires the lease before the worker starts and keeps it through the reverse
    phase.
  - When a worker is abandoned (A3), ownership passes to that worker thread, which releases the
    lease in its own `finally`. The lease is therefore held for as long as any thread that can
    write to the drive is alive.
  - A plug-in replication does not start while a backup job runs. If one is already running, the
    coordinator waits for the lease in 1 s steps that honour Stop.
  - The other holders stop blocking forever: today they use a bare `with REPLICATION_LOCK`. They
    wait at most 30 s, honouring Stop, then report "The SSD is busy: an earlier copy is not
    answering. Quit and reopen Sift to clear it." Sift never suggests unplugging a drive that
    a thread may still be writing to.
  - **Eject consults the lease.** `ssd_busy` is true while the lease is held by anyone,
    including an abandoned worker thread after its job has ended, as well as during any
    `ssd_writing` section or active job. Eject and "Safe to unplug" stay refused until the
    thread exits, with the reason "An earlier copy to the SSD has not finished. Quit and reopen
    Sift before ejecting."

### A2. Progress and persisted outcomes

- **Job progress** gains an `ssd` block: state (`starting`, `waiting_access`, `copying`,
  `caught_up`, `paused`, `stopping`, `unplugged`, `full`, `done`, `failed`), files and bytes done,
  known so far, backlog at start, deferred count, the current file with its bytes, and speed.
- **`run_summary` gains** (migration): `pull_finished_at`, `ssd_state` (`none`, `complete`,
  `incomplete`, `stopped`, `failed`), `ssd_copied`, `ssd_bytes`, `ssd_deferred`,
  `ssd_pending_after`, `ssd_finished_at`. The job ends `done` when the pull completed, whatever
  the SSD did. The summary then says which of these it was:
  - "Complete: Mac and SSD";
  - "Mac complete · SSD: 120 files pending (drive unplugged)";
  - "Stopped";
  - "Failed: reason".
- **The UI** has two bars, "Phone → Mac" and "Mac → SSD". A run in the drain or reverse phase
  says so ("Copying to SSD: 2,300 left") instead of showing a full bar with Pause.
- **An incomplete SSD copy is never reported as complete, anywhere.** Owner decision 1: it is
  not a failure either. One function, `backup_outcome(run, live_pending)`, feeds every surface:
  - the Backup box headline and "Last backup" line;
  - the run history and Logs entries;
  - the per-set rows: "Mac copy · SSD pending 120", never "Backed up" while the SSD lacks
    files;
  - Health;
  - any notification;
  - `GET /api/status`;
  - Storage locations' SSD line.
- **How the status changes.** A run's stored `ssd_state` never changes after it ends. The *current*
  status comes from the live pending count, so it reads "Complete" only once a later run or
  plug-in replication has copied the remainder. Confirmations (I3a) still require the SSD copy,
  as today.

### A3. Pause, Stop and shutdown

| When | Pause | Stop |
| --- | --- | --- |
| Pull phase | Readers finish the files in flight (shown with their names and bytes left), then the coordinator pauses; the worker pauses at its next 8 MB chunk | As today for the phone; the worker abandons its current SSD file (the staged copy is discarded: the Mac copy is the source, so nothing is lost) |
| Drain phase | The coordinator calls `checkpoint()` in its 0.5 s wait loop; the worker pauses at its next chunk | The worker abandons its current file and exits |
| Reverse phase | The coordinator, between files | The coordinator, between files |
| Waiting for the lock | Pause shows; nothing to stop yet | Returns at once |
| Blocked OS call (permission prompt, hung drive) | Shows "Paused; the SSD is not answering" | See below |

- **Pausing without changing state.** The worker uses a new `JobControl.wait_while_paused()`,
  which waits without a state change. Only the coordinator calls `checkpoint()`.
- **Closing the window.** This requests Stop through the existing `jobs` shutdown step, which
  runs first: usually under 1 s (abandoning a file is immediate), bounded by the step budget.

#### Worker safety: abandonment, drive ownership, commits and shutdown

**Each file follows one sequence.** Only the worker touches the SSD.

1. Check stop and the abandoned flag. Create a unique staging file (`.staging/<uuid>.part`) with
   an exclusive create.
2. Copy from the Mac copy in 8 MB chunks, checking stop, pause and abandonment between chunks.
   Then fsync, and require the staged hash to equal the Mac replica's recorded hash (I2).
3. Check abandonment and the drive identity (A1), then publish with the existing
   `StagedFile.commit`, unchanged. Its steps:
   - full fsync, then an exclusive, no-overwrite rename;
   - the **disk read-back verification** with the cache off (`F_NOCACHE`). A mismatch removes
     only the file this commit just created and raises `ChecksumMismatch`, which defers the
     file;
   - a flush of the folder.

   An existing name is hashed: identical content is reused, different content gets a new name.
   Nothing is ever overwritten (I1).
4. `with commit_lock:` if abandoned, do nothing. Otherwise one `db.tx` records the SSD replica.

**Abandonment.** If the worker has not ended within 10 s of Stop, or within the shutdown step's
budget, the coordinator:

- sets `abandoned = True` under `commit_lock`;
- ends the run with `ssd_state = stopped`;
- skips the reverse phase, which would need the lease;
- hands the lease to the worker thread rather than releasing it.

**Guarantees:**

- **G1. Commit and abandonment are ordered.** The worker holds `commit_lock` only around one
  `db.tx`, which is bounded by the existing busy retries. The coordinator never takes
  `commit_lock` inside a transaction, and the lock order is always `commit_lock` → write lock. So
  the coordinator's acquire is bounded and cannot deadlock. Once it returns, that worker writes
  no further rows.
- **G2. Every row is true whenever it is written.** A replica row is written only after step 3
  succeeded on the drive checked by UUID. Even a late row describes a verified, published file.
- **G3. An abandoned thread whose blocked call returns** closes its file and removes only its own
  staging file, by the exact name it created. It then releases the lease and exits. It never
  publishes and never commits. If the blocked call *was* the publish, the published file stays
  (I1), without a row: an **orphan**.
- **G4. Orphans heal without writes.** The next SSD copy meets the name taken, hashes it, finds
  it identical, and records it. This is the existing `TargetExists` path in
  `Replicator._commit`; no file is written.
- **G5. A crash at any point leaves exactly one of four states:**
  - nothing;
  - a staging file, removed at start by the lease holder;
  - an orphan (G4);
  - a recorded replica.

  It never leaves a row without a file.
- **G6. Database close is safe against late writers.** `Database.close()` already takes the write
  lock, so it waits for a transaction in flight.
  - Today `tx()` checks `_writer` before taking the lock, so a transaction that races a close can
    fail with an `AttributeError`. A moves that check under the lock and raises a typed
    `DatabaseClosedError`.
  - The worker and coordinator treat that error as abandonment.
- **G7. Shutdown order.**
  - Shutdown runs `jobs` (stop, then abandon) before `db` (close).
  - If `jobs` overruns its budget, `db` still closes safely (G6).
  - A run whose summary was never written is marked `stopped`, with `ssd_state = stopped`, at the
    next start, as delete runs already are (`app.py`, start-up recovery).
- **G8. No second writer on the drive.** The lease is released only by a thread that can no
  longer write (A1), so this process never starts a second writer on that drive. After a quit, a
  new process meets at most staging files and orphans (G5), whatever the old thread does in its
  last moments.
- **G9. No lock is held across a wait.** No thread holds the database write lock, the job's
  `cond`, `commit_lock` or a transaction while waiting, joining, sleeping or ringing the
  doorbell. The coordinator joins the worker holding only the lease.
- **G10. A later backup while an abandoned worker holds the lease** runs the pull normally, and
  its SSD part ends `incomplete` with the reason "SSD busy".
- **If a worker dies.** A worker exception is recorded; the pull continues (the Mac copy is
  unaffected), and the summary says "Mac complete · SSD copy stopped: reason". A coordinator
  failure stops the worker the same way, and the job fails as today.

### A4. SSD access check

- **Setup and Destinations probe.** Choosing the SSD runs an access probe on the selected
  volume, checked by volume UUID at the time of the probe:
  - it records a unique name `.sift-access-<random>` in the database;
  - it creates that folder exclusively, writes one file inside with an exclusive create, then
    removes only those two;
  - a probe interrupted by a quit is cleaned up at the next start, from the recorded name.
- **Results are told apart:** `EPERM`/`EACCES` ("macOS has not allowed Sift to use this drive:
  System Settings → Privacy & Security → Files and Folders"), `ENOSPC` (full), `EROFS`
  (read-only), and other errors (in plain words).
- **During a run.** A first SSD operation that has not returned within 3 s shows "Waiting for
  SSD access: check for a macOS permission prompt". This is tentative wording, because a slow
  drive looks the same. Stop and shutdown follow the abandon rule in A3.

### A5. "What is on the phone"

- **Coverage, not "the whole phone".** The heading becomes "Files in your selected backup
  folders". A coverage line names the sets left out, the skipped files (skip rules), and the
  folders that could not be read.
- **Run identity.** `analysis_run` stores scope (sets), state, processed and total counts, and a
  partial result committed per batch. A fifth group, **unchecked**, holds files not yet hashed:
  "not backed up" means checked and missing.
- **Partial versus complete.**
  - The last **completed** result keeps its own block, with "Checked at …".
  - An active run shows separately as "Checking now: 2,200 of 51,106 · started 19:08", with
    counts marked "so far" and never a percentage.
  - Events and queries carry the run id, and the UI never lets an older run replace a newer one.
- **Freshness.** A completed result is marked stale after a backup, a destination change or a
  change of sets. A stopped run shows as "Stopped at 2,200 of 51,106" and does not replace the
  last complete result. A failed run shows its error, and the last complete result stays.
- **Presentation.** One summary bar and line for the completed result, then a compact row per
  set:
  - a stacked bar (verified, not verified, unchecked, not backed up, problems) and a badge with
    an icon and words;
  - colour is never the only signal, and sets that need attention come first;
  - Show details / Hide details folds the card to its summary, with each set expandable and the
    choice remembered.

### A6. Times

- **Format.** One shared format: "today at 18:47", "yesterday at 09:12", "15 Nov at 18:47", with
  the full date and time on hover.
- **Backup box:**
  - "Last backup: today at 18:47 · Complete" in every state;
  - the summary gives each phase's finish time ("Phone → Mac 17:39 · Mac → SSD 18:47 · took
    1 h 58 min");
  - a running backup says when it started.
- **Per set:** "Confirmed …" (the confirmation, as today) and, separately, "Last file copied …"
  (newest verified copy). A set is never called "backed up" because of one recent file.
- **Analysis:** "Checked at …" and "Started at …" as in A5.

### A7. Storage locations (Settings → General, read-only)

- **What it shows:**
  - the active data folder, noting when `SIFT_HOME` overrides it;
  - the settings file and the database;
  - the Mac backup folder (present or missing, free space);
  - the SSD: its name, its volume id shortened, its folder, and whether it is connected.
- **Reveal in Finder** for the folders that exist, limited to these known paths. Changes still
  happen in Destinations.
- **Why now.** A development data folder can hold older labels than the installed app; this
  makes visible which data a running Sift uses. Labels already move between data folders with
  Teach → Save/Load teaching. A full profile move is B2.

### A: interfaces and persisted state

- `replicator.pending_after(db, mac, ssd, cursor, limit)`;
- `SsdWorker(ctx, mac, ssd, control_view, more, pull_done, lease, commit_lock)`;
- `DriveLease` (replaces the bare `REPLICATION_LOCK`: `acquire(timeout, should_stop)`,
  `hand_to(thread)`, `release()`, `holder`);
- `DatabaseClosedError`, raised by `tx()` after `close()`;
- `backup_outcome(run, live_pending)`, used by every status surface;
- `JobControl.wait_while_paused()`;
- the job progress `ssd` block;
- the `run_summary` columns (migration);
- the `analysis_run` progress and partial columns (migration);
- `GET /api/storage`;
- the probe record (setting).

No change to destinations, required copies or delete checks.

### A: invariants

- **I1:** the worker only adds files, under staged unique names.
- **I2:** the SSD receives only hash-checked Mac files, through staging, hash and exclusive
  publish.
- **I3a:** unchanged; deletes still need every required copy.
- **I5:** every SSD copy commits per file, and an abandoned worker never commits (G1–G5).

### A: tests (fake phone, temporary folders, forced interleavings with barriers, fixed clocks)

- **Timing:**
  - a slow phone with a fast SSD (the worker waits on the doorbell and finishes after
    `pull_done`);
  - a fast phone with a slow SSD (the drain phase shows; the run is done only after the drain);
  - a backlog from an earlier run, included and labelled.
- **Failed files stay queued:** a file that fails on the SSD is deferred, the run completes, the
  summary counts it, and the next run copies it.
- **Races:**
  - a file committed just before `pull_done`, and a reused `rowid`: both copied by the final
    pass;
  - the final pass with 1,234 pending rows, 7 of them deferred: every row visited exactly once,
    query count within the bound, and the pass ends;
  - a row recorded above `high` during the pass (forced): a tail pass copies it. Rows that keep
    appearing stop after 3 tail passes as `incomplete`;
  - 20 failures in a row end the worker as `failed`, and `ENOSPC` as `full`;
  - a drive renamed mid-run (same UUID, new mount) is followed; a different UUID at the old path
    gets no writes;
  - a worker exception mid-run (Mac complete, SSD stopped, recorded);
  - a coordinator failure (the worker stops, no commits after).
- **Pause and Stop:**
  - Pause and resume in each phase, including the drain-only phase, with the state correct after
    resume;
  - Stop during lock wait, pause, a large file (the staged copy is discarded) and a blocked write
    (abandoned, no later commit, staging cleaned at start);
  - worker safety, each with a barrier at the named point:
    - abandonment racing a commit: either the row exists and is counted, or no row is written;
    - a blocked publish that returns after abandonment leaves an orphan, which the next run
      adopts with no write;
    - the lease stays held until the abandoned thread exits, and quarantine and a plug-in
      replication get "SSD busy" within 30 s;
    - a `tx()` after `close()` raises `DatabaseClosedError`;
    - a run with no summary is marked `stopped` at start;
  - `backup_outcome`: an incomplete SSD run reads "SSD pending" on every surface until the
    pending count reaches 0;
  - Hypothesis over Stop and Pause timing.
- **Shutdown and drives:**
  - window close during a blocked write, within budget, with the database closed under the
    worker;
  - **bounded shutdown under lock contention:** shutdown starts while another thread holds the
    database write lock (a slow transaction) and the worker is waiting on `commit_lock`, and
    again with the worker holding `commit_lock` inside a slow `tx`. Each shutdown step stays
    within its budget, and the whole shutdown stays within the sum of the budgets. Afterwards
    no SSD row is written and the database closes cleanly;
  - the read-back: a staged file whose final copy reads back differently is removed, deferred
    and never recorded;
  - Eject refused while an abandoned thread holds the lease after its job ended, and the
    message never suggests unplugging;
  - unplug mid-copy, a full SSD, and a plug-in replication holding the lock;
  - the probe: denied, full, read-only, interrupted, and never touching another file.
- **Analysis:** partial results with unchecked files, a stale result, a stopped run kept apart,
  and an older run's late event ignored.
- **Times:** formats, "Last file copied" kept separate from "Confirmed", and Storage locations
  with and without `SIFT_HOME`.

### A: owner checks

- A backup with the SSD attached, watching both bars move together.
- Pause and Resume during the SSD drain.
- Stop mid-video, then the next run completes.
- Approve or deny the access prompt on a new drive.
- Run Analyse phone after a backup: the old result is marked stale and the new one fills in live.
- Check the Storage locations section.

---

## Milestone B: SSD-only policy and profile migration

### B1. SSD-only destination policy

A destination policy (`backup.policy = "mac_and_ssd" | "mac_only" | "ssd_only"`), not a change of
target. The audit, and what each part does under `ssd_only`:

| Area | Behaviour under `ssd_only` |
| --- | --- |
| Backup target | The SSD, found by volume UUID only. A renamed drive (same UUID) is followed to its new mount, keeping its folder, as `ensure_destination` already does. A backup refuses to start, with the reason, when the drive is absent or another drive (a different UUID) has taken its name |
| Space check | On the SSD |
| Staging | The SSD's `.staging` |
| Folder creation | Never under `/Volumes` unless that volume is mounted |
| Required copies and delete checks (I3a) | SSD only |
| Confirmations | Every confirmation made under another policy becomes invalid; confirm again |
| Analysis, verification, restore, adoption, archive | Use the SSD (analysis already prefers the Mac when present) |
| Recovery at start (I5) | Targets the SSD's staging and quarantine |
| Background replication | Off: plugging the SSD in never recreates Mac copies |
| Reports, manifests (I9), app exports, quarantine | Written to the SSD |
| Eject | Refused during any write |
| Full or unplugged mid-run | The run stops, and every file copied so far is kept |

**For an existing install:**

- the switch explains that there will then be one required copy, and needs a typed
  acknowledgement;
- nothing is moved or deleted, and every record is kept;
- the Destinations page lists what exists only on the Mac (count and size), for the owner to
  keep or copy;
- switching back restores the previous policy, and confirmations are invalidated again.

### B2. Profile migration

- **Picking the source.** Discover the data folders Sift has used (the standard one and any
  passed through `SIFT_HOME`), or let the owner choose one. Show each folder's label count, last
  backup and schema version.
- **Locking both profiles** (owner decision 3: a start-up screen).
  - Each data folder has its own instance lock (`SingleInstanceLock`, `flock` on its lock file).
    The migration takes **both** without waiting, the source's and the target's.
  - It takes them in a fixed order, by the resolved real path, so two migrations in opposite
    directions cannot deadlock. Each uses non-blocking `flock`; on a failure it releases what it
    holds and reports which folder is in use ("A Sift using …/support is running; quit it
    first"). It never waits for or breaks another process's lock.
  - Source and target must resolve to different real paths, and neither may sit inside the
    other.
  - Both locks are held from the first read until the end of the copy or the rollback. The app
    then starts on the target holding only the target's lock.
  - A Sift started meanwhile on either folder gets the existing "already running" message.
- **Copying.** The source database is copied with SQLite's online backup API
  (consistent with its WAL), never by copying files. The settings file and the teaching and
  learning state move together.
- **Safety:**
  - the source folder is kept untouched;
  - the target is refused if it is already populated, unless the owner chooses "keep it as a
    backup", in which case it is renamed aside;
  - rollback restores the renamed folder.
- **Backups stay put.** Migration never moves backup files, which stay where the settings point.
  The owner can change Destinations or adopt the folder afterwards.
- **Versions.** If the source schema is newer than this app, the migration is refused. If it is
  older, the migration runs it forward and says the older app can no longer open the result.
- **Privacy.** Documentation and fixtures use placeholder paths only.

### B: tests and owner checks

- **Tests:**
  - policy switch: confirmations invalidated, no files touched, and replication not recreating
    Mac copies;
  - drives: a wrong drive with the same name (refused), missing (refused), full (stops), and the
    right drive renamed (followed, with the same folder and no new destination row);
  - migration locking: the source in use by a running Sift, the target in use, two migrations
    racing in opposite directions (no deadlock, one refused), the same folder or nested folders,
    and a symlink to the source;
  - the delete checks requiring only the SSD;
  - migration: WAL consistency with live writes before the close, a populated target, rollback,
    and newer and older schemas.
- **Owner checks:**
  - switch a test install to SSD-only and back;
  - migrate the development data folder into a fresh standard one, and check the labels and
    settings arrived.

---

## Milestone C: later

### C1. App display names

- **Measure first.** Benchmark on the owner's phone before building: offset reads use
  `tail -c +N`, which reads through every byte before the offset on the phone.
- **Choose the parser on evidence.** Compare a maintained parser (for example `androguard` or
  `pyaxmlparser`) with a small custom one: licence, size, maintenance, and behaviour on malformed
  input (fuzzing).
- **Bounds:**
  - only `base.apk` (a split APK's label lives in the base);
  - central directory ≤ 1 MB;
  - manifest ≤ 1 MB compressed and ≤ 4 MB expanded;
  - `resources.arsc` ≤ 32 MB compressed and ≤ 64 MB expanded, with a compression-ratio guard;
  - every offset checked against the file size;
  - 2 s of parsing per app and 1 GB of reads per refresh;
  - one connection slot, never during a backup job, and cancellable.
- **Cache key.** Phone, package, `versionCode` (the `versions` probe gains it from `dumpsys
  package`), and display language.
- **Fallback.** The package id in full, plus a tidied form. A name is never guessed from a
  partial parse.

### C2. Performance

The deferred items from the hardening pass: clustering time, and the near-duplicate buckets.

---

## Assumptions

- P1. One SSD writer; more only if measurements show a drive benefits.
- P2. Doorbell wait 2 s; batch size 500; SSD chunk 8 MB; abandon after 10 s; the access hint
  after 3 s.
- P3. Stopping an SSD copy mid-file is safe because the Mac copy is the source; phone reads keep
  finishing the files in flight.
- P4. A run is `done` when the pull completed; the SSD result is reported beside it, so a missing
  drive never fails a good Mac backup.

- P5. Lease waits for other holders: 30 s; tail passes after the final pass: at most 3; failures in
  a row before the worker gives up: 20.

- P6 (implementation). The worker takes the drive lease on its own thread and releases it
  itself, instead of the coordinator acquiring it and handing it over. The lease is then held
  exactly while a thread that can write is alive, with no hand-off race. Abandonment only marks
  it stuck.
- P7. `abandon()` waits at most 2 s for `commit_lock`. If a slow write holds it longer (the
  bounded-shutdown requirement), the flag is set anyway: only the one write already begun can
  land, and its row is true (G2). G1 holds whenever the lock is had.
- P8. The SSD copy reads in the existing 4 MB chunks (not 8 MB): a finer stop.
- P9. The verifier's repair copy and the after-run database copy to the SSD also take the lease
  (repair waits up to 30 s; the database copy skips when the drive is busy). Staging folders are
  never made with missing parents, so a swapped or unplugged drive fails instead of a folder
  appearing at the old path.
- P10. A running analysis saves its partial result at most every 2 s. A result marked out of date
  offers no delete (the delete's preflight re-checks everything anyway).
- P11. The version is 0.0.2 on `develop`; no tag until the owner has tested the dmg.

## Implementation notes that touch the safety guarantees

- **D1 (fixed locally; not released).** Once background work starts, the profile's
  instance guard is retained until process exit, rather than unlocked at logical shutdown.
  `tests/core/test_instance_lifetime.py` reproduces the old overlap and checks headless/window
  quit, timed-out cleanup, writer completion before process exit, normal exit and forced exit.
  Early port/database startup errors keep their explicit release behavior. This prevents a new
  same-profile Sift overlapping an abandoned writer in the exFAT fallback; it does not provide
  filesystem-wide atomicity against other profiles or unrelated programs.
- **D2 (fixed).** The verifier repair and the after-run database copy wrote to the SSD without
  the lease (P9).
- **D3 (fixed).** A drive renamed in the middle of a file wrote that file under the old path, and
  the staging folder was made with missing parents before any drive check. The worker now checks
  the drive before each file and starts the file again at the new mount.

- **Review (one pass, fixed).**
  - The worker finds the drive again by UUID after waiting for the lease.
  - It stops before the sweep and self-heal when it was given up on during the first operation, and runs self-heal under `commit_lock`.
  - A missing Mac folder ends the copy instead of dropping Mac rows.
  - A deliberate Pause never reads "not answering".
  - An analysis left running by a quit is stopped at start.
  - The analysis answer is kept per phone, and an older reply never brings back an ended run.
  - One access probe runs at a time.
  - The reverse phase waits for the lease, or says it skipped.
  - Stale is per phone and only after a backup that copied something.
  - A plug-in copy never starts during a backup.
  - Per-set "SSD: N pending".
  - "SSD copy stopped: reason".
- Not done in A: a Hypothesis test over Stop and Pause timing (the forced interleavings are covered
  by barrier tests), and "SSD pending" on Health (Health shows no backup status today).

## Product decisions (accepted by the owner, 2026-11-15)

1. A run with the SSD unplugged or incomplete reads "Mac complete · SSD pending N", not a
   failure. The incomplete status is kept on every surface (A2, `backup_outcome`) until the SSD
   really has the files.
2. B1 offers `mac_only` as well as `ssd_only`.
3. B2 runs from a start-up screen, holding both profiles' locks.
