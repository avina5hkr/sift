# M3: Analyse phone on the real phone (read-only)

Headless app, real phone over USB, scratch `SIFT_HOME` and backup folder under the ignored
`build/`. One small set (screenshots, 75 files, 31 MB) was backed up first, then Analyse phone
ran over that set and the camera set.

| Set | Backed up and verified | Not verified yet | Not backed up | Problems |
|---|---|---|---|---|
| screenshots | 75 (0.03 GB) | 0 | 0 | 0 |
| camera | 0 | 0 | 7,594 (65.95 GB) | 0 |

- The phone hashed 75 candidate files (31 MB); the whole analysis took 6 s, including listing
  7,669 files. The pre-run estimate for the hashing was 0.8 s at the measured 40 MB/s.
- Nothing was written to the phone; the adb server pid was the same before and after. The
  only database rows written were the analysis run's own.
