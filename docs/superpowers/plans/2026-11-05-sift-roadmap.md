# Sift — Implementation Roadmap (M0 to M14)

> **For agentic workers:** This is the master roadmap. It is not executed directly. Each milestone gets its own
> bite-sized TDD plan (`docs/superpowers/plans/YYYY-MM-DD-mN-<name>.md`), written when the previous milestone's
> gate passes, because later interfaces depend on what earlier spikes find. M0 is already planned in detail:
> `2026-11-05-m0-skeleton.md`. Measured inputs: `docs/spikes/pre-m2-inputs.md`.

**Goal:** Build Sift, a local-only Mac app that backs up an Android phone to a Mac folder and an exFAT SSD with verified copies, then frees phone space through reviewed, preflighted deletes.

**Architecture:** One Python 3.14 process (managed by uv) hosts FastAPI on 127.0.0.1 plus a pywebview window around a React UI. Three engines (backup, analysis, cleanup) reach the phone only through a `Transport` protocol; a `FakeTransport` with fault injection drives every test. SQLite (WAL) is the index; the disk is the truth.

**Tech Stack:** uv + CPython 3.14.7, FastAPI/uvicorn, pywebview, SQLite (stdlib sqlite3), adbutils, Pillow + pillow-heif + numpy, ffmpeg/ffprobe, PyObjC Vision, ONNX Runtime (CLIP), scikit-learn; React 18 + TypeScript + Vite + Tailwind + TanStack Query/Virtual + Zustand; pytest, hypothesis, Playwright (WebKit); PyInstaller + create-dmg.

**Spec:** the maintainer's private product spec. It is kept out of this repository; section numbers in this plan refer to it, and every requirement a task needs is restated in the task itself.

---

## 1. Toolchain decisions (verified 2026-11-05 on this Mac)

| Decision | Value | Evidence / reason |
| --- | --- | --- |
| Python | **CPython 3.14.7**, uv-managed, pinned in `.python-version`; `requires-python = ">=3.14,<3.15"` | Latest stable (3.15 is rc1). Spec floor is 3.11+. Dry-run resolve on 3.14 succeeded with arm64 wheels for onnxruntime 1.31.1, scikit-learn 1.9.1, numpy 2.5.4, pillow 13.0.0, pillow-heif 1.8.1, pyobjc-framework-vision 13.0, pywebview 6.2.1, adbutils 2.12.0, pyinstaller 6.22.3 |
| Python packaging | `uv init --package` src layout, `uv_build` backend, `uv.lock` committed, deps added per milestone with `uv add` (YAGNI) | Spec 14: "install the pinned dependencies from a lock file"; CI uses `uv sync --locked` |
| Dev tools | uv dependency group `dev`: pytest, pytest-timeout, hypothesis, httpx, ruff, mypy | Spec 14 developer workflow |
| Node | **Node 24 LTS**, pinned in `web/.nvmrc` and `engines` | Installed Node 20.18.3 is past EOL and below current Vite's floor (20.19) |
| Web versions | React **18.3.1** (spec pins React 18); everything else `npm install --save-exact` at scaffold time; `package-lock.json` committed | Spec 14: "Pin every version" |
| Router | react-router (declarative mode, `BrowserRouter`) + server SPA fallback | Cleanup expands to six sub-pages (spec 11) |
| E2E browser | Playwright **WebKit** project | pywebview uses WKWebView |
| adb | Android SDK adb 1.0.41 / platform-tools 37.0.1 is installed at `~/Library/Android/sdk/platform-tools/adb`; the managed download (spec 6) stays the default for a fresh Mac | `adb version` |
| Dev ffmpeg | Homebrew `/opt/homebrew/bin/ffmpeg` for dev and fixtures; static arm64 builds bundled in M10 | Present on this Mac |
| Tesseract | Not installed; only needed if the M6 Vision-Hindi check fails | Spec 8 OCR |

**Known risk:** PyInstaller with uv-managed (python-build-standalone) CPython. M0 Task 12 is a spike that builds a windowed `.app` early. If it fails, fall back to a python.org 3.14 framework build for packaging only.

## 2. Measured baseline (from `docs/spikes/pre-m2-inputs.md`)

USB 2.0 is the **normal case**: the owner keeps the phone's own cable. No plan, default, estimate or timeout may assume USB 3.

| Quantity | Value | Used for |
| --- | --- | --- |
| Phone link | 480 Mb/s (`SPUSBHostDataType` "Link Speed: 480 Mb/s"; ioreg `"Device Speed" = 2`) | USB 2.0 hint in the UI |
| Pull speed (reference) | **37.5 MB/s** (one 475 MB file, `adb pull`) | Every time estimate and ETA default until the app measures its own |
| Phone-side SHA-256 | ≈ 254 MB/s (one run; may be cache-flattered, so treat as an upper bound) | Adoption / analysis / preflight estimates; batch timeouts |
| SSD link | USB 3.1 enclosure, ioreg `"Device Speed" = 3` (5 Gb/s); exFAT; identified by volume UUID (never committed) | Mac→SSD copies are not the bottleneck |
| Mac | Apple Silicon, 24 GB RAM | Worker counts, embedding speed |
| Reference phone | **Android 16**, nearly full (Sift targets any Android 11+ phone); `wm size` reports the current resolution setting | Parsers, screenshot rule, restore free-space check |
| Library | tens of thousands of files, about 100 GB | Scale tests, estimates: 100 GB first pull ≈ 45 min at 37.5 MB/s |

Rules that follow:
- **Link-speed detection:** `system_profiler SPUSBDataType` prints nothing on this macOS. Read `SPUSBHostDataType` first, then `ioreg -p IOUSB` (`Device Speed`: 2 = 480 Mb/s, 3 = 5 Gb/s) as the fallback. The measured transfer rate is the truth; "Use a USB 3 cable in a direct port" is a hint only and never blocks or slows anything.
- **Parallel reads** stay configurable (`phone.parallel_reads_usb`, default 3 until measured). The first-run reader test (1, 2, 4 readers × 3 s, spec 6) picks the number and stores it.
- **ETA and progress** use the 5 s moving average of real throughput; before any bytes move, estimates use the stored measured speed, else 37.5 MB/s.
- **Timeouts checked at this speed:** read stall 30 s is fine (a 1 MiB chunk takes ≈ 28 ms alone, ≈ 85 ms with 3 readers). The **sha256 batch timeout (120 s)** is not safe by file count alone: 40 large DCIM videos (≈ 600 MB each, ≈ 24 GB) need ≈ 95 s at the optimistic hash rate. Batches are therefore capped at 40 files **or 8 GB**, whichever comes first, and the timeout is `max(120 s, batch_bytes / 100 MB/s)`. M1 records the cold hash rate over many files and adjusts the cap.

## 3. Cross-cutting architecture decisions (locked here so every milestone plan agrees)

1. **SQLite access** (`sift.core.db.Database`): thread-local read connections; all writes through `Database.tx()` = `BEGIN IMMEDIATE` with busy retry, serialised by a process-wide lock. Worker processes (spawn pool) never open the DB. Pragmas are set per connection in `Database.open()`, not inside migrations.
2. **Migrations:** `src/sift/resources/migrations/NNNN_name.sql`. `0001_init.sql` holds the full spec section 5 schema. Later migrations, each designed in its milestone plan:
   - `0002_adopt` (M3): `adopted_file` (files found in an existing backup folder, provisional until hashed), plus the set-confirmation and phone-analysis result tables.
   - `0003_delete_runs` (M8): preflight job, selection hash, manifest path, source (`cleanup | browse | slate | analysis`).
   - `0004_slate` (M12).
   - `0005_apps_health` (M13/M14): unlock records, checklist ticks.
3. **Auth token transport:** `X-Sift-Token` header on every `/api` call; `?token=` accepted **only** on `GET /api/events` and `GET /api/media/*` (spec 12). Static UI files need no token. The token never appears in any log (uvicorn access log off; request logging records method and path only).
4. **Events:** `sift.core.events.EventBus` is thread-safe and never blocks a publisher; each SSE subscriber has a bounded drop-oldest buffer and at most one pending wake-up. Event names: `hello`, `device`, `job`, `progress`, `log`, `toast`. Progress is throttled to one event per 500 ms per job.
5. **Jobs** (M2): `sift.core.jobs.JobManager` persists to the `job` table; I/O jobs on threads, CPU work on a spawn `ProcessPoolExecutor`; one backup and one delete job at a time; pause/stop at file boundaries; `waiting_for_phone` and "waiting for SSD" are pauses, never failures. `multiprocessing.freeze_support()` runs first in `sift.app.main`.
6. **Crash simulation for property tests:** engines call `sift.core.crashpoints.hit(name)` at every durable step; tests raise `SimulatedCrash` at a random hit, rebuild the context from disk + DB, and run startup recovery. One slower test per invariant uses a real `SIGKILL`.
7. **Manifests from M8:** every delete run (cleanup, browse, slate, analysis) writes its manifest first (I9), one code path.
8. **Clock:** an injectable `sift.core.clock.Clock` (`now() -> int`) drives settle, min-age, quarantine, confirmation staleness and calibration rules.
9. **mypy:** strict for `sift.core.*`, `sift.backup.*`, `sift.cleanup.*`.
10. **adb server (spec 6, owner decision 2026-11-05):** use the shared server on **port 5037**. If a server already answers there (Android Studio's, or one left by a manual `adb` command), attach to it through adbutils' server protocol (no version restart). If none answers, start one with the configured adb. Never call `kill-server`. If the phone is absent although USB shows a device with an adb interface, the UI says another tool may be holding it and offers Retry. A private port (`phone.adb_server_port`) is an advanced setting for Wi-Fi only. Reason: macOS grants one process exclusive access to the phone's USB interface (`kIOReturnExclusiveAccess`, measured).
11. **Sets drive everything:** the scanner, confirmation, Analyse phone, Delete later, Browse and Clean slate all read `backup.sets`; `deletable = false` (Documents by default) removes a set from every delete path, and `cleanup.never_delete` still wins over `deletable = true`.
12. **No autonomous deletes (I7, spec 10E):** only `POST /delete/execute` (with a fresh preflight and `confirm: true`) reaches `Transport.delete`. Schedulers, reminders, purges and analysis runs never call it; a test pins this.
13. **Matching is size-then-hash (spec 7, decided):** file names are never proof. `adopt_mode` accepts `hash` (default) and the reserved `fast`, which warns and behaves as `hash`. No name-based matching ships in v1.

## 4. Build order and gates

M0 → M1 → M2 → M3 → **Gate A (backup trustworthy)** → M4 → M5 → M6 → M7 → **Gate B (calibration works)** → M8 → **Gate C (delete trustworthy)** → M11 → M12 → **Gate D (clean slate trustworthy)** → M13 → M14 → M9 → M10.

No code path calls `Transport.delete` on a real phone before Gate C. `AdbTransport.delete` raises `NotImplementedError` until M8. Manual tests before M3 use an empty scratch backup folder, never the owner's existing backup folders (paths not yet known; no test may point at them).

## 5. Milestones

### M0 Skeleton [4, 5, 12, 13, 14] — detailed plan: `2026-11-05-m0-skeleton.md`
Repo, uv project, paths, config (all spec 13 keys including `[[backup.sets]]` and the addendum keys), DB + migration runner + `0001_init.sql`, event bus, logging, crash file, FastAPI app with Host/token/error shape, `/api/status`, `/api/events`, single-instance lock, launcher with pywebview, React shell, `make dev`, Playwright smoke, CI, PyInstaller spike.
**Exit:** the app opens and shows status from the API; `make test` and `make e2e` pass locally and in CI; no test finds the token in any log.

### M1 Device layer [6]
- `device/transport.py`: `DeviceInfo`, `RemoteEntry(path, size, mtime, is_dir, is_symlink)`, `LinkHealth(kind: Literal["usb","wifi"], speed_class: str, latency_ms: float)`, `Transport` Protocol exactly as spec 6, errors `DeviceDisconnected`, `DeviceTimeout`, `RemoteError`.
- `device/fake_transport.py`: `FakePhone` + `FaultPlan` (disconnect_after_bytes, hang, slow_read_bps, size_change_mid_read, wrong_sha256, refuse_delete, offline_for_s, identity, garbage_output) + a call journal (every mutating call recorded, for "read-only" assertions later).
- `device/adb_manager.py`: decision 10 (attach to 5037 or start one; never kill-server); managed platform-tools download with consent; `phone.adb_path` override.
- `device/adb_transport.py`: sync LIST/STAT, streamed reads, resume via `exec-out tail -c +$((N+1))` (1-based, measured), batched `sha256sum` capped at 40 files or 8 GB with the size-aware timeout (section 2), `shlex.quote` on every shell path, per-call timeouts (stat 10 s, list 60 s, read stall 30 s, delete 15 s), semaphore of 6 connections.
- `device/tracker.py`: `ConnectionState` (no_adb, no_device, unauthorized, offline, wrong_device, connected_usb, connected_wifi, plus the "held by another tool" hint); identity from `ro.serialno` → `android_id`; backoff 1, 2, 4, 8, 16, then 30 s.
- `services/usblink.py`: link speed from `SPUSBHostDataType`, fallback `ioreg -p IOUSB`; parsers tested on captured output from this Mac.
- Contract tests for FakeTransport and (marker `realphone`) AdbTransport. `scripts/spike_device.py` measures cold sha256 rate over ≥ 50 files and sync LIST speed → appended to `docs/spikes/`.
**Exit:** both transports pass the contract tests; connection states, including "held by another tool", show in the status bar.

### M2 Backup to Mac and backup sets [7]
- **Task 1, before any job code: one clean quit path** (owner decision, 2026-11-05; fixes rulings R19/R26). Cmd-Q, closing the window, Ctrl-C and SIGTERM all go through one shutdown routine. It asks running jobs to stop at the next file boundary (a partly copied file stays a `.part` in `.staging/`, never under a final name), waits a bounded time, then stops the server, closes the database and releases the single-instance lock.
  - Window mode: register SIGINT/SIGTERM with `PyObjCTools.MachSignals` (pywebview's `installMachInterrupt` otherwise replaces the SIGINT handler), and route `applicationShouldTerminate:` / window-closed through the same routine. AppKit's `terminate:` must not call `exit()` before it finishes.
  - Tests (non-window paths, automated): `test_sigterm_stops_job_at_file_boundary_and_releases_lock` and `test_sigint_…` run `sift --no-window` in a subprocess with a FakeTransport backup in progress, send the signal, and assert: exit 0; no file under a final name without its replica row; DB closed cleanly (a new process opens it without recovery warnings); the lock can be taken immediately. Plus a unit test that the shutdown routine is idempotent (signal + window-close together run it once).
  - Manual check (window path): with a slow fake backup running, try Cmd-Q, the red close button, Ctrl-C in the launching terminal, and `kill <pid>`. Each must stop within a few seconds and leave the next launch clean ("already running" must not appear). Record the result in `docs/spikes/m2-quit.md`.
- `backup/sets.py`: resolves `backup.sets` (`paths`, `shallow_paths`, `exclude`, `enabled`, `deletable`, `adopt_mode`) into scan roots; a run takes the ticked set names. `GET/PUT /backup/sets`. Default sets (spec 7, owner 2026-11-05): Camera, Screenshots, Pictures and Movies, WhatsApp media (Android/media path **and** the legacy `/sdcard/WhatsApp/Media`), WhatsApp Business (on), Documents, Downloads, **Recordings** (`/sdcard/Recordings`, on, deletable), **Loose files** (`shallow_paths = ["/sdcard"]`, on, not deletable).
- Scanner rule for `shallow_paths`: list the folder's direct child **files** only; never descend into subfolders (so `/sdcard` as a shallow path never pulls in DCIM, Android, etc.). Tests: `test_shallow_path_takes_direct_files_only`, `test_loose_files_and_folder_sets_never_overlap` (a file belongs to at most one ticked set across the defaults).
- `backup/skiprules.py`: spec 7 list **plus `.Links`, `.wamocache`, `.Wamo` and every other dot-folder under WhatsApp Media**. `core/origin.py`. `backup/scanner.py` (only ticked sets; upsert, `present=0`, settle 120 s / 2 s double-stat, space check +2%).
- `backup/layout.py` with the name rules; the owner's real folder names are unit cases: `" AI Media"` (leading space survives sanitising, collides correctly by `rel_key`), `"WhatsApp Backup Excluded Stickers"`, `"WhatsApp AI Media"`, `"WhatsApp Video Notes"`.
- `backup/puller.py`, `core/jobs.py`, `core/crashpoints.py`, `core/clock.py`, `backup/recovery.py`; first-run reader test picks `parallel_reads_usb` (section 2).
- API `/backup/start` (body: sets, dry_run) and the rest of spec 12's backup routes. UI: Backup screen with "This run: Camera, WhatsApp media", per-set status rows and per-set Back up now; wizard step 6 ticks sets. ETA per section 2.
- Tests: layout unit tests, skip-rule tests for every WhatsApp dot-folder, `test_run_touches_only_ticked_sets`, `test_excluded_subfolder_not_scanned` (Camera excludes DCIM/Screenshots), P1, P2, P6.
**Exit:** the quit-path tests pass and the four-way manual quit check is recorded (gate: no M2 job code merges without it); P1, P2, P6 pass; a run with one set ticked writes rows only for that set; a 100-file real-phone backup into an **empty scratch folder** is verified by hand, and its MB/s is recorded against the 37.5 MB/s reference.

### M3 SSD, verify, adoption, confirmation, Analyse phone (read-only) [7, 10E]
- `services/volumes.py` (owner decision: live SSD detection lands here, not in M1): find the SSD by volume UUID with `diskutil info -plist`, require a real mount point (`os.path.ismount`), watch plug/eject (poll `/Volumes` every 2 s or DiskArbitration) and publish `ssd` events; the status chip goes live in this milestone (until then it reads "SSD not connected"). `backup/replicator.py` (both directions: each destination ends with the union), self-heal, `backup/verifier.py` + Problems/Repair, `backup/rebuild.py`, `core/dbbackup.py`, eject, "Safe to unplug".
- Migration `0002_adopt`. `backup/adopt.py`: walk a chosen existing folder **without modifying, moving or renaming anything**; create `adopted_file` rows (provisional), hash in the background (F_NOCACHE), and only then create `content` + `replica` rows that keep the existing relative paths; extra copies listed as duplicates; "older copy differs" listed under Problems; nothing deleted. `backup/match.py`: size lookup, then batched phone-side SHA-256; a match links with no transfer. `POST /backup/adopt`.
- `backup/confirm.py`: per-set report = preflight checks 1–3 read-only; Markdown to `DEST_ROOT/.sift/reports/`; Stale on set change or after `confirmation_valid_days` (30). `GET /backup/confirmation?set=`, `POST /backup/confirm`.
- `cleanup/analyse.py`: Analyse phone, read-only: scan without pulling, size-then-hash match, verify matches (backup hash vs phone sha256, batches per section 2), groups **Backed up and verified / Backed up, not verified yet / Not backed up / Problems** per set. `POST /analyse/phone` (body: sets), `GET /analyse/phone/{id}`. UI: summary screen with counts and sizes only (lists wait for M4; no delete button until M8).
- Tests:
  - P3; SSD-unplug; `test_bidirectional_replication_yields_union`.
  - `test_adoption_never_modifies_existing_files` (bytes, mtime, inode and names of every pre-existing file unchanged; hypothesis over random trees).
  - `test_provisional_adoption_is_verified_before_counting`: an `adopted_file` row not yet hashed is reported as provisional, is absent from "verified" in confirmation and analysis, and counts only after its hash is stored and equals the phone-side hash.
  - `test_fast_adopt_mode_warns_and_hashes`: `adopt_mode = "fast"` logs the warning and produces exactly the `hash` result.
  - `test_name_match_is_never_proof`: same name and size, different bytes → not linked.
  - `test_analyse_phone_is_read_only`: the FakeTransport journal records zero mutating calls; the only DB writes are the analysis tables.
  - `test_confirmation_goes_stale` (new file in set; 31 days via Clock).
**Exit:** P3 passes; SSD-unplug passes; both destinations identical (union); adoption of a 20k-file fixture folder changes no existing file; Analyse phone shows correct four-group counts on the fake phone and, read-only, on the real phone with a time estimate based on the measured hash rate. **→ Gate A**.

### M4 Index and exact duplicates [8 A–B, 10, 11 Review]
Indexer, thumbnails, `analysis/exact.py`, `cleanup/candidates.py` (keeper priority), media routes (HTTP Range), `/cleanup/candidates`, `/selection`. UI: Cleanup home, Review shell. Screenshot rule (spec 8, amended): path and name first, then size match against **both** `wm size` physical and override sizes in either orientation. The Analyse phone screen gains its file lists from the new grid.
**Exit:** Exact duplicates tab works end to end with previews; 20k-item scroll benchmark recorded.

### M5 Near-duplicates [8 C–E]
Hashing, banding, union-find, group types, keeper score, video fingerprints, `scripts/make_fixtures.py`. **Exit:** fixture accuracy tests pass.

### M6 OCR, embeddings, clusters [8 F–H]
OCR providers + Vision Hindi check, consented model download with pinned SHA-256, CLIP ONNX embeddings, clustering, Teach the app, search. **Exit:** clusters browsable; "good morning" finds greeting fixtures; Hindi OCR result recorded.

### M7 Classifier and calibration [9]
Features, lexicons, kNN + LR, tiers, cold start, reasons, calibration flow. Files from the **Recordings** set are never classified or suggested as junk (owner rule; test `test_recordings_never_junk`). **Exit:** tier tests and P5 pass; calibration stores its row. **→ Gate B**.

### M8 Delete, quarantine, Delete later, Delete only [10, 10E, 11 dialogs]
- Migration `0003_delete_runs`. `cleanup/preflight.py` (six checks; phone hash re-read **in this run** for every file; keeper-still-present check; source `cleanup | browse | slate | analysis`), `cleanup/manifest.py` (written before the first delete; missing required destination blocks), `cleanup/deleter.py`, `cleanup/mediascan.py`, `cleanup/quarantine.py` (restore checks phone free space: selection + 2 GB must fit).
- `/delete/execute` refuses unless the preflight finished < 10 min ago for the same selection hash and `confirm` is true; typed count when required.
- **Delete only:** the Analyse phone screen gets **Delete the backed-up files (X GB)** (selection = the verified group of deletable sets only), **Back up the N new files first**, **Not now**; Choose files arrives with M11/M12. Disposition defaults to **archive** for source `analysis`. If a required destination (e.g. the SSD) is unmounted, the delete step waits for it ("Waiting for SSD"); no per-run override exists.
- **Ready to free card + Delete later** on backup results and Cleanup home ("Ready to free: 40 GB in Camera (confirmed 7 Nov)", Review and delete / Not now); only confirmed, deletable sets; `cleanup.delete_reminder_days` (0 = off) drives an optional notification that only opens the app.
- Tests:
  - P4 (all cases).
  - `test_stale_analysis_can_never_cause_a_delete`: after an analysis marks files verified, mutate each precondition in turn (phone file edited, replica corrupted, replica deleted, SSD unmounted, device swapped, file now under min age, set made non-deletable). Expected: preflight blocks exactly those files and `Transport.delete` is never called for them.
  - `test_provisional_entry_never_satisfies_a_preflight_gate`: an `adopted_file` that is provisional (unhashed) fails check I3a even if its size and name match.
  - `test_unbacked_files_never_offered`: the delete-only selection never contains a Not-backed-up file.
  - `test_delete_waits_for_missing_ssd`.
  - `test_nothing_deletes_without_execute`: run every scheduler, reminder, purge and analysis job against a journaling FakeTransport; zero delete calls.
**Exit:** P4 and the five tests pass; manual checklist item 8 on the real phone, plus one delete-only run of 20 verified files through Analyse phone. **→ Gate C**.

### M11 Browse and delete [10B, 10A archive browser]
Storage explorer, smart lists, folder-selection expansion that skips and lists un-backed-up files, typed confirmation (outside delete roots or > 200 files), disposition (default quarantine), empty-folder removal, Archive browser + restore (free-space check), **Choose files** from Analyse phone opens Browse filtered to the verified group. P8, P9.

### M12 Clean slate [10A]
Migration `0004_slate`. Keep rules, plan tree, backup-gap step, adjust, disposition, manifest + typed count, chunked execute (500), resume; **Choose files** from Analyse phone opens Clean slate filtered to the verified group. P7 over random trees and rules; 50k-file plan on the fake phone. **→ Gate D**.

### M13 Apps tab [10C]
Migration `0005_apps_health`. Probes, actions, server-side protection and unlock rules, FakeTransport app registry, P10. Any `dumpsys diskstats` capture used as a fixture has package names redacted before commit.

### M14 Health report and pre-reset checklist [10D]
Optional probes, verdict rules, export, bugreport, checklist, restore helper. Parser fixtures include **Android 16** output (redacted as in M13).

### M9 Wi-Fi and hardening [6 Wi-Fi, 15, 16 perf]
Pairing, reconnect, heartbeat, Wi-Fi reader count, private-port option (decision 10), full fault-injection sweep, benchmarks, Logs screen, diagnostics.

### M10 Package and polish [14]
PyInstaller spec, Info.plist, ad-hoc signing, dmg, THIRD_PARTY.md, README, crash dialog, accessibility pass. **Exit:** a fresh Mac installs from the dmg; all acceptance criteria met.

## 6. Open decisions

- Phone screen usable for one-time prompts (spec 18): still to confirm before M1's real-phone run.
- The owner's existing backup folder paths and layout: needed before the real-phone adoption run in M3.
- Hindi OCR outcome: decided by the M6 check.

## 7. M0 outcome and carry-over (2026-11-05)

M0 is complete on branch `m0-skeleton`: 12 tasks, each spec- and quality-reviewed, then a whole-branch review and one fix wave. Suites: 137 pytest (warnings are errors), 22 vitest, 3 Playwright WebKit e2e, ruff + mypy clean. The packaging spike works with uv-managed CPython 3.14.7 (`docs/spikes/m0-packaging.md`).

Decisions made during M0 that later plans must respect:
- adb uses the shared server on 5037 (owner decision; spec amended).
- Backup-set paths: must start with `/sdcard/` (`/storage/emulated/0/` rewritten), no `..`/`.`/empty segments, no trailing slash, excludes strictly inside the set's paths. Invalid entries are dropped with warnings. After any bad or unreadable config, the default sets are used with **deleting turned off** until the owner fixes the settings. An unparseable `config.toml` is set aside and a fresh one is written with deleting off.
- Static UI is served from the 404 handler (GET/HEAD, non-`/api` only); `make e2e` runs Playwright from `web/`; `make dev` runs `scripts/dev.sh`.
- react-router 7 (React 18 pinned); the Vite template's TypeScript 7 and oxlint are kept.

Carry-over, by milestone (from the final review triage):
- **M2 (start):** quit path. pywebview replaces the SIGINT handler and Cmd-Q exits through AppKit, so `main()`'s cleanup does not run in window mode. This is now **M2 Task 1 and an M2 exit gate** (see the M2 entry above).
- **M2 (start):** serialise event payloads to JSON once at publish time, outside the bus lock (freezes payloads; a bad payload fails at the publisher).
- **M2:** decide the `never_delete` semantics. Effective list = built-in floor ∪ `cleanup.never_delete` ∪ `browse.never_delete_extra`; the floor (Android/data, Android/obb, WhatsApp and WhatsApp Business Databases/Backups) cannot be removed; warn on omission. Validate and normalise every config path list (`never_delete`, `delete_roots`, `slate.keep_folders`, `browse.never_delete_extra`) with the set-path rules, and require an absolute `backup.mac_root`.
- **M2:** `save_config` must handle a non-table section; set-aside file names need sub-second uniqueness; `DEFAULT_SETS` becomes immutable; set names trimmed and case-folded for uniqueness; phone paths compared case-insensitively. Per-thread read connections must be released when job threads end. Make `Database.write_connection()` private or migration-only. Harden `ServerHandle.stop()` and `main()`'s `finally`. Name the Settings milestone in the UI. Add regression tests for bus cancellation, closed-loop publish and multi-thread publish.
- **M3:** migration-runner hardening: catch BaseException in `_apply`, verify the WAL mode result, retry on busy, narrow "damaged" to real corruption, add `tx()`-after-failed-migration and duplicate-version tests. Migrations cannot rebuild FK tables (pragmas are banned), so design 0002+ accordingly.
- **M4:** security tests with `/api/media` (missing Host, empty/wrong header + query token, HEAD, `/api/media` without slash, raw-ASGI traversal).
- **M8 (Gate C blocker):** `test_user_never_delete_cannot_drop_builtin_protection`; pin `delete_roots`/`never_delete` defaults in tests; `required_destinations = []` must never satisfy I3(a).
- **M9:** argparse range check for `--port`; uvicorn double-logging; accurate bind-error messages.
- **M10:** build from a runtime-only environment (PyInstaller via a `build` group); `--specpath build/`; `LSUIElement` via `info_plist`; crash dialog; prove the ML/image stack (onnxruntime, scikit-learn, numpy, pillow-heif, PyObjC Vision) and the bundled binaries.
- **First push:** CI `trace: 'retain-on-failure'` for Playwright artefacts; `persist-credentials: false` on checkout.
