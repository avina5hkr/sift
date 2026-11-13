# M2: real-phone backup into a scratch folder

Task 18 of the M2 plan. The app ran headless against the real phone over USB (read-only), with
`SIFT_HOME` and `backup.mac_root` in a scratch folder under the ignored `build/` directory. The
owner's existing backup folders were not used.

## Run

| | Run 1 | Run 2 (nothing changed on the phone) |
|---|---|---|
| Set | one small set (screenshots) | same |
| New files | 75 | 0 |
| Bytes copied | 30,952,777 (31.0 MB) | 0 |
| Time | about 2 s (whole seconds recorded) | under 1 s |
| Failed / duplicates | 0 / 0 | 0 / 0 |

- Effective speed about 15 MB/s for 75 small files (about 0.4 MB each), where per-file overhead
  dominates (stat, open, F_FULLFSYNC, read-back). The first-run reader test measured 40.2 MB/s on
  large files with 4 parallel readers, above the 37.5 MB/s reference.
- Run 2 transferred 0 bytes (P6 holds on the real phone).

## Checks

- All 75 Mac files re-hashed (SHA-256) and matched their database rows; no `.part` file left.
- 5 random files hashed on the phone (`sha256sum`) matched both the Mac copy and the database.
- A sampled file opens as a JPEG with its Exif data.
- The adb server's pid was the same before and after; nothing was written to the phone.

Sample size note: the plan asked for about 100 files; the chosen set held 75.
