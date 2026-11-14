# M9: Wi-Fi and hardening

Working method: `CLAUDE.md`. Roadmap: section 5, M9. Spec: 6 (Wi-Fi, reliability rules,
keep awake), 11 (Settings pages, Logs screen), 15, 16 (faults, performance). Plus the polish
items carried from M7, M8, M11 and M14.

## Features, in build order

1. **Wi-Fi pairing and reconnect** (`device/wifi.py`, migration `0013_wifi.sql`): pair, connect
   and mDNS discovery through the adb server's host services (`host:pair`, `host:connect`,
   `host:mdns:services`), so no adb command line is needed. Paired phones are remembered (mDNS
   name, last address) and reconnected at launch. API under `/api/phone/wifi`.
2. **Wi-Fi rules**: heartbeat every `phone.heartbeat_seconds` on a Wi-Fi connection (two misses:
   offline, jobs pause); `phone.parallel_reads_wifi` readers on Wi-Fi; deleting from the phone
   and app uninstall or clear data refused over Wi-Fi unless `phone.allow_delete_over_wifi`.
3. **Keep awake** (`services/keepawake.py`): the Mac holds a power assertion (`caffeinate -i -w`)
   while a backup or task runs; the phone stays awake (`stay_on_while_plugged_in 3`) only when
   `phone.keep_awake`, its previous value saved and restored at the end, and at the next launch
   after a crash.
4. **Logs and diagnostics**: `GET /api/logs` (level, area, search, newest first, from the log
   file), `POST /api/diagnostics` (zip of logs, config and schema version into ~/Downloads;
   home and backup paths replaced unless the owner ticks "include file paths").
5. **Settings**: `GET /api/settings`, `PUT /api/settings` (a whitelisted set of fields per page,
   validated by the config model, saved to config.toml). Pages: General, Phone, Destinations,
   Backup, Cleanup, Models, Advanced.
6. **Fault sweep and benchmarks**: every FakeTransport fault through backup and delete, with
   the invariants checked after each; a benchmark script for scan, backup and plan on the fake
   phone (not part of the test run).
7. **Polish**: Junk tab keyboard navigation (J/K/arrows), the GB total in the Browse bottom bar,
   the trained junk model cached by its inputs fingerprint, the health task progress kept when
   leaving the page, the checklist tick/date race.

## Exit

The fault sweep passes with every invariant; Wi-Fi pairing, heartbeat and the delete rule are
tested on the fake; Settings saves and reloads. Owner: pair the phone over Wi-Fi once and run a
small backup over it.

## Assumptions

- L1. Pairing uses the adb server's `host:pair:CODE:HOST:PORT` (platform-tools 30+); the reply
  text decides success ("Successfully paired").
- L2. A Wi-Fi heartbeat is the tracker's existing refresh (battery, storage, identity) run every
  `heartbeat_seconds` instead of 60 s; two failures in a row show offline.
- L3. "Delete requires USB" covers phone deletes (all delete runs) and app uninstall / clear
  data; restore pushes and reading are allowed over Wi-Fi.
- L4. Diagnostics go to ~/Downloads as `sift-diagnostics-DATE.zip`; logs are redacted by
  replacing the home folder and the backup roots with placeholders.
- L5. Settings edits only fields that have no dedicated screen; backup folders, SSD and backup
  sets keep their own choosers.

## Outcome

Built: Wi-Fi pairing, connect and mDNS reconnect through the adb server; Wi-Fi heartbeat and
reader count; phone deletes, app removal and cache trimming need USB unless allowed; keep the
Mac (caffeinate) and optionally the phone awake during work, restored per phone after a crash;
Settings (7 pages from the config schema, whitelisted, all-or-nothing, one config writer) and
the Logs screen with diagnostics export (paths, serials, models and drive names hidden unless
asked); the fault sweep over every fake fault and `make bench`; polish (Junk tab keyboard,
Browse size total, health task kept, checklist tick/date race, junk model cached by its inputs).
Review: 0 Critical, 5 Important (keep-awake value of one phone blocking another and a
restore/start race, Settings bypassing the config lock, redaction leaking names with spaces
and identifiers, model cache shared across phones and lexicon edits, log times in 1970) and
the minor trim-caches gate, fake serial, caffeinate wait and IPv6 brackets fixed. Carried
(Minor, M10): settings knobs nothing reads yet, config warnings not refreshed after a save,
the speed test from Settings, launch reconnect timing, Wi-Fi page polling, adb port changes
needing a restart, live log rows mixed with filtered ones, unsaved edits lost when switching
Settings pages. Benchmarks: 2,259 files (21 GB) backed up in 56 s on the fake, an unchanged
second run 0.1 s, a 20,000-file listing in 0.01 s.
Final run: 1749 Python, 277 web, 14 e2e passed; lint clean. Owner: pair over Wi-Fi once and
run a small backup over it.
