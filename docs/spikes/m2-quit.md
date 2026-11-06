# M2: quitting while a job is running

The M2 plan (Task 1) asks for a hands-on check that every way of quitting Sift stops a running
job cleanly. The check runs the real window with the test-only slow job
(`SIFT_ENABLE_TEST_HOOKS=1` or a source checkout, plus `SIFT_TEST_SLOW_JOB=1`), with
`SIFT_HOME` pointing at a scratch folder under `build/` and `SIFT_TRACKER=off`. The job was
slowed to about 8 s per file so every quit lands mid-file.

After each quit, a checker confirms:

- the process exited with status 0 within a few seconds;
- the database closed cleanly (no `-wal` file left);
- the single-instance lock is free, so the next launch starts normally;
- `.staging` holds no half-written file;
- every file in the job's folder has its database row, with matching size and SHA-256.

## Results

| Route | How it was triggered | Shutdown reason logged | Result |
|---|---|---|---|
| Red close button | by hand, after the job had finished (1000 files) | window closed | pass |
| Cmd-Q | by hand, mid-job (279 files) | quit | pass |
| Red close button | `performClose:` on the window, mid-job | window closed | pass |
| Cmd-Q | `NSApp terminate:` (what Cmd-Q sends), mid-job | quit | pass |
| Ctrl-C | `SIGINT` to the process, mid-job | signal SIGINT | pass |
| `kill <pid>` | `SIGTERM` to the process, mid-job | signal SIGTERM | pass |

## Found and fixed

The first hand-run Cmd-Q logged an ERROR from uvicorn: the window's open event stream
(`/api/events`) kept the server busy until its 2 s graceful-shutdown timeout, and uvicorn then
cancelled it with a traceback. Shutdown now closes the event bus first, which ends every open
stream on its own, so the server stops at once and logs nothing. After the fix, every route
above ran with no ERROR lines in the log.
