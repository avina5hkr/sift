# Pre-M2 inputs: reference-device measurements

Measured on 2026-11-05 on a reference setup. **Personal details stay out of this repository**: device serial numbers, volume names and IDs, user names and paths, file and app names, and per-folder usage. The maintainer keeps the unredacted notes in a git-ignored `local/` folder.

Reference setup: an Android 16 phone (Sift targets Android phones in general, Android 11 or later); an Apple Silicon Mac; an external SSD formatted as exFAT (GUID partition map, USB 3.1 enclosure).

All checks were read-only. Nothing was written to the phone, and the one temporary copy made on the Mac was deleted.

## Tools on the phone

| Check | Result | Consequence |
| --- | --- | --- |
| `echo hello \| sha256sum` | correct SHA-256 of `hello\n` | Phone-side hashing works (spec 6, I3b) |
| `echo abcdef \| tail -c +3` | `cdef` | `tail -c +N` is **1-based**: to resume after N bytes, run `tail -c +$((N+1))` |
| `toybox --version` | 0.8.12-android; `sha256sum`, `tail`, `stat`, `find` in `/system/bin` | |
| `date +%s%N` | nanoseconds are **not zero-padded** | Never time things with it; use the shell's `time` |
| `find … -exec stat {} +` | fails with "Argument list too long" | Never build listings this way (the spec requires the sync protocol anyway) |
| `wm size` | reports the current resolution setting, not the panel | The screenshot rule checks path and name first |

## Throughput

| Measurement | Result |
| --- | --- |
| USB link | 480 Mb/s (USB 2.0) with the phone's own cable, read from `SPUSBHostDataType` "Link Speed" and from `ioreg` `"Device Speed" = 2`; the SSD enclosure reported `"Device Speed" = 3` (5 Gb/s) |
| `adb pull` of a 475 MB video | **37.5 MB/s**, the reference speed for estimates |
| Phone-side SHA-256 | **300 to 350 MB/s**. 10 large videos (1.04 GB) took 3.03 s cold and 2.97 s warm; a 50-file mix (1.42 GB) took 4.64 s. Cache makes little difference: about two thirds of the time is system time (FUSE) |
| A full listing of about 70k files with size and mtime | about 14 s (`find -print0 \| xargs -0 stat`, used for measurement only) |

Timeouts at these speeds:
- The read stall limit of 30 s is safe: a 1 MiB chunk takes about 28 ms, or about 85 ms with 3 readers.
- A sha256 batch is capped at 40 files or 8 GB. 8 GB takes about 25 s at the measured rate, well inside the timeout of `max(120 s, bytes / 100 MB/s)`.

## Findings that shaped the plans

1. **One adb server per USB phone on macOS.** A second adb server on another port logged `usb_osx.cpp: Could not open interface: e00002c5` (`kIOReturnExclusiveAccess`) once a second and never saw the phone. Sift therefore attaches to the shared server on 5037 and never kills it (spec 6, amended).
2. **`system_profiler SPUSBDataType` prints nothing on current macOS.** Use `SPUSBHostDataType`, with `ioreg -p IOUSB` as the fallback. The measured transfer speed is the truth; "use a USB 3 cable" is a hint.
3. **Hashing is cheap compared with pulling** (about 8 to 9 times faster at USB 2.0), so size-then-hash adoption is affordable for every set.
4. **Camera file names** were almost all `YYYYMMDD_HHMMSS.*`, but a WhatsApp-named image sat in `DCIM/Camera` too. Names are never proof of anything.
5. **A USB link can drop and come back on its own.** adb logged `usb_read failed … e00002ed` and reconnected 2 s later. Reliability rule 2 (pause and resume) is needed.

## Name and tree edge cases seen (no names recorded)

These went into the fake phone and the M2 tests:
- Hundreds of non-ASCII names, all already NFC.
- Hundreds of names ending in a dot, in WhatsApp `Documents/Sent`.
- A WhatsApp folder whose name starts with a space (`" AI Media"`).
- WhatsApp folders beyond the spec's seven: `WhatsApp Backup Excluded Stickers`, `WhatsApp Video Notes`, `WhatsApp AI Media`, `WhatsApp Sticker Packs`, `WhatsApp Bug Report Attachments`.
- Hidden folders under WhatsApp Media: `.Links`, `.Statuses`, `.wamocache`, `.Wamo`, and one with a random name.
- A legacy `/sdcard/WhatsApp/Media` tree left from before Android 11.
- Thousands of files in `Pictures/.thumbnails`.
- Loose files directly in `/sdcard`.
- Zero-byte files, and one file over 4 GiB.
- No exFAT-forbidden characters, no component over 255 bytes, no case-only collisions, and no mtimes in the future.
