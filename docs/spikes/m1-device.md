# M1 real-phone results

Run on 2026-11-06 against a reference phone (Android 16, USB 2.0 cable at 480 Mb/s) with the M1 device layer (`AdbTransport`, `Sync2`, `DeviceTracker`). Every step was read-only: nothing was written to or deleted from the phone. Only totals, timings and yes/no answers are recorded here. There are no serial numbers, model names, file names or per-folder usage.

The owner allowed one hands-on step, unplug and replug. Revoking and re-allowing USB debugging, and reading a file while it changes, stay covered by the fake-phone tests.

## Contract suite on the real phone

`SIFT_REALPHONE_SERIAL=… uv run pytest tests/contract -m realphone`: **15 passed, 4 skipped**. The skips are the fake-only cases, which need a symlink, a zero-byte file, odd names, or a file that shrinks mid-read. Sift never creates any of these on a phone.

## Protocol and tool checks

| # | Check | Result |
|---|---|---|
| 1 | `exec:` hash script on a missing path | Returns `None` (no name, no extra line) |
| 2 | Phone `sha256sum` against host SHA-256 | 10 of 10 match |
| 3 | `tail -c +N`, time to first byte | Offset 1: 0.07 s. Offset 1 GB: 6.9 s. Offset 3.5 GB: 22.3 s. The phone reads and discards up to the offset at about 150 MB/s, so the resume allowance is needed. Bytes after the offset match a full read. |
| 4 | Full RECV of the largest file (3.84 GiB, over 2^31) | Complete at 42 MB/s, and the hash matches the phone. This phone had no file over 4 GiB, so the sizes above 4 GiB stay covered by the unit and fake tests. |
| 5 | `/sdcard` symlink | LST2 reports a symlink. Listing through it works, and children are reported as `/sdcard/…`. |
| 6, 14 | Walk of every default set root | About 68k files in about 17 s (about 4k files/s). One sync connection per walk; no new TIME_WAIT entries were seen on port 5037. |
| 7 | Another app's `Android/data/<pkg>` | The folder check fails with `RemoteError` (it is not silently empty) |
| 8 | Read interrupted by unplugging | `DeviceDisconnected` at the moment of unplug (no 30 s wait). No watchdog threads left, and the connection slot was freed. |
| 9 | Shared adb server | Same server pid before and after every step, including running the app. With 6 streams open, a 7th call times out cleanly, and calls work again once they close. |
| 10 | Large request | A 60 KB `exec:` request is accepted; the default `max_request_bytes` stays as is |
| 11 | `info()` fields | Serial, model, Android version, battery, `df` and screen size are all filled in |
| 12 | `link_health` | `usb` |
| 13 | Non-UTF-8 names | None present on this phone, so the fake tests cover them |
| 15 | Real app (`sift --no-window`, real tracker, temp data folder) | `connected_usb` within 0.3 s, with the USB 2 hint, battery and free space shown. The adb server pid did not change. |
| 16 | Phone-side hashing rate | Cold mixed batch of 60 files (319 MB): about 86 MB/s. Large videos earlier: 300 to 350 MB/s. `Limits.sha_assumed_bps` is set to 40 MB/s, about half the lower figure. |
| 17 | Small files | 1,000 files under 200 KB at about 84 files/s, with no TIME_WAIT build-up |
| 18 | Per-entry errors | No set root walk was aborted |
| 19 | Identity | `ro.serialno` equals the adb serial and the USB serial number (yes) |
| 20 | Replug | Unplug: `no_device` within about 0.1 s. Replug: `connected_usb` 0.8 s after adb first reported the phone. |
| 21 | Request size | Not needed (see 10) |
| 22 | File changing during a read | Skipped by the owner; the fake-phone tests cover it |

## Follow-ups

- `Limits.sha_assumed_bps` is lowered to 40 MB/s. It drives both the sha256 batch timeout and the resume allowance.
- The resume allowance is confirmed necessary: a resume deep into a large video would otherwise hit the 30 s stall limit.
