# M2: real-phone backup check

The M2 exit check asks for a backup of about 100 real files into an empty scratch folder,
checked by hand. It ran headless (`--no-window`) against the real phone over USB, with
`SIFT_HOME` and the backup folder both under the repository's ignored `build/` folder, never
an existing backup folder. Only the Screenshots set was ticked.

| | Result |
|---|---|
| Files copied | 75 (30.95 MB), 0 failed, 0 warnings |
| First run | 2.6 s wall, including the scan: about 12 MB/s end to end |
| First-run reader test | best with 4 readers, 40.6 MB/s (reference: 37.5 MB/s) |
| Second run, no phone changes | 0 files, 0 bytes transferred (P6 on a real phone) |
| Checksums | SHA-256 of all 75 Mac copies equals the phone's `sha256sum` of each original |
| adb server | same process id before and after; nothing was written to the phone |

The end-to-end figure is below the reader-test speed because the files are small (about
400 KB each): per-file work (a full flush to disk, the read-back check, the folder flush)
dominates, not the USB link. The reader test measures the link itself.

The first attempt surfaced one defect, fixed before the run above: a set's folder that does not
exist on the phone was reported as "could not read". It is now treated as empty, with no
warning; a folder that had files and goes missing still trips the mass-disappearance guard.

The scratch folder was deleted afterwards.
