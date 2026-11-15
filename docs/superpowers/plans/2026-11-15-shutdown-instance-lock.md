# Shutdown instance-lock retention implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this focused fix natively. Follow `CLAUDE.md`: no per-task subagents, one whole-change review. Checkboxes track implementation; this document does not authorize a release.

**Goal:** Prevent a second Sift using the same profile from starting after logical shutdown but before the previous process, including any abandoned SSD writer, has exited.

**Architecture:** Give the application's instance guard an explicit, irreversible process-lifetime retention mode. Enable it before background writers can start; subsequent shutdown cleanup must not unlock or close its descriptor. The kernel releases the guard when the process exits. Ordinary, explicitly managed `SingleInstanceLock` users retain their current acquire/release behavior.

**Tech stack:** Python 3.14, macOS `flock`, existing shutdown and drive-lease machinery, pytest subprocess tests.

**Spec:** D1 under “Implementation notes that touch the safety guarantees” in [the v0.0.2 plan](2026-11-15-v002-parallel-ssd.md), plus the owner's request to plan the release-blocking fix. This plan refines D1's suggested conditional release into explicit process-lifetime ownership.

## Handoff context

- At planning time, `develop` is at `b335d51b0a128de493a82dabbddb6e1f98d92f2d`, 13 commits ahead of `origin/develop`; the last public version is v0.0.1. Recheck these facts when resuming.
- Version 0.0.2 is prepared locally. Additional, uncommitted disconnect-handling work modifies `app.py`, drive leases, replication, cleanup, and tests. Preserve it; do not stage it into this fix accidentally.
- The original handoff was plan-only. The owner subsequently authorized implementation after cross-checking application behavior; see the execution record below.
- An isolated snapshot of that committed candidate passed 1,897 Python tests (1 skipped, 29 deselected), 301 web tests, Python lint/type checks, and the web build. Browser tests ended with 13 passed and one performance test passing on retry. These are historical results, not verification of this future fix.

## Root cause and evidence

1. `app.main()` acquires `SingleInstanceLock` and registers `lock.release` as the last shutdown cleanup action.
2. `Shutdown._run_step()` runs cleanup on daemon threads and proceeds when a step exceeds its budget. Logical shutdown can finish while a worker remains blocked in the OS.
3. The drive lease correctly remains held by an abandoned SSD thread, but that lease is process-local.
4. `SingleInstanceLock.release()` explicitly calls `LOCK_UN` and closes the descriptor before the process necessarily exits.
5. A new process can then acquire the profile guard while the old worker is still alive. The exFAT fallback in `backup/staging.py::_rename_no_replace` checks absence and calls `os.rename` under a process-local lock. Two such processes do not share that lock, creating a possible overwrite window.

Relevant code: `src/sift/app.py`, `core/instance.py`, `core/shutdown.py`, `backup/lease.py`, `backup/ssdcopy.py`, and `backup/staging.py`. Read their current versions before implementing.

## Global constraints

- I1: never overwrite or remove existing backup content to solve this race.
- Preserve bounded shutdown; do not wait indefinitely for a hung worker.
- Preserve staged writes, disk read-back verification, and the abandoned worker's drive lease.
- Do not add `os._exit`, forcibly kill threads, unlink the instance-lock file, or use PID-file existence as locking.
- Do not close the retained descriptor from an `atexit` handler or object finalizer: Python cleanup can precede complete thread/process termination.
- Tests use temporary profiles, fake phones, and synthetic blocked calls. Never touch the real phone, ADB server, backup folders, or real Application Support data.
- No release, push, tag, dependency change, database migration, or broader disconnect-handling implementation belongs to this fix.

## Decision and alternatives

Use process-lifetime retention once asynchronous application startup begins. Do not condition release solely on `DRIVE_LEASE.stuck`: it is a snapshot and does not establish that every timed-out cleanup/background writer has stopped. Do not extend shutdown joins indefinitely. Early startup errors before background work begins may continue releasing normally.

The guarantee is **same-profile process exclusion**. Different `SIFT_HOME` profiles or unrelated programs writing the same backup folder are outside this fix; do not claim filesystem-wide atomic no-replace behavior on exFAT.

## Review focus

- Shutdown has returned but an old writer remains alive: the competing process is refused.
- The writer subsequently finishes while its process remains alive: the guard remains retained.
- The owner exits normally or is killed: a fresh process can immediately acquire the guard.
- Startup fails before asynchronous writers start: normal explicit cleanup still releases it.
- Repeated cleanup or loss of the local lock-object reference cannot release a retained descriptor.

## Task 1: Lock lifecycle and application integration

**Modify:** `src/sift/core/instance.py`, `src/sift/app.py`.
**Tests:** `tests/core/test_instance.py`, `tests/test_app.py`, `tests/test_quit_paths.py`.

**Interface:** add `SingleInstanceLock.retain_until_process_exit(self) -> None`.

- [x] First reproduce the unsafe lifecycle in a child process: acquire the real profile guard, run the real `Shutdown` cleanup including its release action, signal that shutdown finished, and deliberately keep the child alive. A competing child currently acquires the guard; the regression should require refusal.
- [x] Implement retention as an idempotent, irreversible lifecycle transition on an acquired guard. Calling it before acquisition raises `RuntimeError`. Once retained, `release()` does not unlock, close, or detach the descriptor.
- [x] Keep the retained descriptor in explicit module-level process-lifetime ownership so lock-object garbage collection does not change the guarantee. Do not register automatic Python-level cleanup for it. Keep ordinary guards' acquire/release behavior unchanged.
- [x] Enable retention in `app.main()` immediately before the first asynchronous application worker can start, including startup quarantine purge. Audit all startup branches to establish this boundary. Keeping the existing shutdown release callback is acceptable because release becomes a no-op for a retained guard.
- [x] Preserve existing early-error behavior before that boundary: port-in-use, database open/migration failure, and already-running paths must not introduce a retained guard unnecessarily. Errors after the boundary retain it until exit.
- [x] Add subprocess tests named `test_retained_lock_survives_release_and_object_cleanup`, `test_retained_lock_is_released_on_process_exit`, and `test_retained_lock_is_released_on_process_kill`. Assert real competing-process behavior, not just method-call counts. Keep retention tests out of the parent pytest process to avoid leaking test-profile descriptors for its lifetime.
- [x] Run the focused tests and confirm the new regression fails on the original implementation and passes with the fix. Existing ordinary lock and early-startup tests must remain meaningful; do not simply remove release assertions.

## Task 2: Prove the shutdown race stays closed

**Tests:** extend `tests/test_quit_paths.py` or add `tests/core/test_instance_shutdown.py`; reuse existing fake-worker/barrier patterns from `tests/backup/test_ssdcopy.py`.

- [x] Run the application shutdown path in a child process with a synthetic SSD operation blocked by an explicit barrier. Use shortened test budgets and a pipe/stdin handshake; do not depend on probabilistic sleeps or a real hung disk.
- [x] After shutdown has finished but before process exit, start a competitor on the same profile. Assert `AlreadyRunningError` and that no backup writer is started by that competitor.
- [x] Resume the abandoned writer and wait for its completion while deliberately keeping its process alive. The competitor must still be refused. This pins process lifetime rather than merely worker lifetime.
- [x] Let the owner process exit, then assert immediate successful acquisition by a fresh process. Repeat with forced termination; the file remaining on disk must not act as a stale lock.
- [x] Exercise both a normal cleanup path and a timed-out/failed shutdown step. Verify shutdown completes within the existing test budget and that retained locking does not add an unbounded wait.
- [x] Confirm the existing SSD tests still prove no database commit after abandonment and lease retention until the worker exits. The instance guard complements those rules; it does not replace them.
- [x] Ensure every test terminates its child processes and closes pipes in `finally`, including assertion failures. No production test-only shutdown escape hatch is needed.

## Task 3: Documentation, verification, and release handoff

**Modify:** relevant instance/shutdown docstrings; the v0.0.2 plan's D1 entry; `CHANGELOG.md` only for this fix's accurate release note. Regenerate `docs/CODEMAP.md` only if its generated entries change.

- [x] Document that logical shutdown and process exit differ. A lingering process legitimately keeps the profile busy. Do not advise deleting the lock file or unplugging a drive that may still be writing.
- [x] Mark D1 fixed only after the subprocess regression passes, with test references; record the focused fix in its own commit when authorized. Keep the profile-scoped and exFAT limitations explicit.
- [x] Run focused checks first:
  `SIFT_TRACKER=off uv run pytest -q tests/core/test_instance.py tests/core/test_shutdown.py tests/test_app.py tests/test_quit_paths.py tests/backup/test_ssdcopy.py`
  Include the new test file if one was added. Use temporary `SIFT_HOME` for spawned apps; fixtures already isolate test profiles.
- [x] Run the full required checks once: `make test`, `make lint`, `make web-test`, and `make e2e`. Use the project's Node version. Separate environment failures and flaky performance results from clean passes.
- [x] Review the final diff once. If the existing dirty workspace is still present, prepare this fix in an isolated checkout and carry only the focused changes. Do not stash, discard, or commit the owner's unrelated work.
- [x] Report the regression evidence, exact candidate commit, remaining working-tree changes, and validation results. Keep all release actions pending a separate owner instruction; updating this plan does not authorize publishing v0.0.2.

## Acceptance criteria

The same-profile competitor cannot acquire the guard at any point between background-worker startup and actual process exit, including after shutdown completes and after an abandoned worker later resumes. Normal process exit and forced termination release it automatically. Ordinary explicit locks and early startup failures retain their documented behavior. No backup content, schema, dependencies, or shutdown budgets change.

## Scope and resumption

Implement this as one focused safety fix, independently of the uncommitted disconnect-handling plan. The owner originally requested a handoff, then authorized implementation after cross-checking the plan. The focused fix is now applied locally. When resuming, read this record and the latest workspace state first; do not assume the historical branch or test results are current.


## Execution record (2026-11-15)

- Implemented in managed worktree `b81a/sift` from the recorded base, then applied only the focused patch to the original checkout. Unrelated disconnect-handling edits are preserved. The owner subsequently authorized a focused local commit; no merge, push, tag, or release is included.
- Added `SingleInstanceLock.retain_until_process_exit()`. `app.main()` calls it immediately before startup quarantine/background work. Ordinary locks and startup port/database failures keep explicit release behavior.
- The four actual-app headless/window and normal/timed-out cleanup cases failed before the fix because a competing process acquired the profile after logical shutdown. All pass after the fix.
- The seven new parameterized tests in `tests/core/test_instance_lifetime.py` cover those four cases, object cleanup and repeated release/retention, normal/forced exit, and retention before acquisition. They consolidate the proposed individual test names.
- Isolated focused fix: 1,904 Python tests passed, 1 skipped, 29 deselected; 301 web tests passed; 14 browser tests passed; Python lint, formatting, mypy, TypeScript, and web build passed. Web lint completed with existing React warnings.
- Original working-copy compatibility: all 99 focused instance/shutdown/app/quit/SSD tests passed with the unrelated disconnect edits still present. Those unrelated edits retain their previously identified import/format lint issues; the isolated fix's clean lint result is not a clean bill for the whole unfinished working tree.
- One independent whole-change review found no blocking issues. Native AppKit and real SSD hardware were not exercised; window lifecycle and blocked SSD work are simulated while actual app startup, shutdown, kernel locks, and competing processes are exercised.
- D1 is closed locally; public release remains separate. The guarantee remains profile-scoped, not a cross-profile or arbitrary-external-writer guarantee for exFAT.
