# Changelog

All notable changes to Sift are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). While Sift is 0.x, a minor version may change
behaviour.

## [Unreleased]

## [0.0.2] - 2026-11-16

### Added

- The copy to the SSD runs alongside the copy from the phone instead of after it, with its own
  progress bar ("Mac → SSD"), files left over from earlier runs counted in, and "Copying to SSD:
  N left" once the phone is done. Pause and Stop work in every phase.
- A check when the SSD is chosen that Sift may write to it: one small test file, removed again.
  A drive macOS has not allowed says where to allow it. During a backup, a first write held by the
  macOS prompt says to look for it.
- One status line for the last backup everywhere (Backup, the status bar, the end-of-run
  message): "Mac complete · SSD: 120 files pending" until the SSD really has the files, never
  "complete" and never "failed" for a good Mac backup.
- The analysis card, now "Files in your selected backup folders": the last complete result with
  when it was checked and whether a later backup made it out of date, apart from a check going on
  ("Checking now: 2,200 of 51,106"); a "Not checked yet" group; a stacked bar and a badge in
  words per set, problems first; Hide details folds it.
- The analysis says where a file really is: "On the Mac, not on the SSD yet" (or the other way)
  instead of "Not backed up", which now means on no backup at all. Only files on every required
  backup count as backed up and verified, as before.
- Times in one format ("today at 18:47"), with the full date on hover; each phase's finish time;
  each set's last copied file, apart from its confirmation.
- Quarantine: Clear expired now, and Delete everything in quarantine now (you type the number of
  files; their backup copies are then gone for good). The number of days files wait can be set on
  the Quarantine page as well as in Settings, and a change applies to the files already there.
- The Backup screen remembers which sets you ticked under "This run", also after Sift restarts.
  Settings is not changed by it.
- Settings, General, Storage locations: the data folder, settings file, database, Mac backup
  folder and SSD this Sift uses, with Reveal in Finder.

### Changed

- One writer per backup drive at a time (the drive lease), including verification repairs and the
  database copy to the SSD. A copy stuck inside macOS keeps the drive busy until it returns, so
  Eject waits, and nothing suggests unplugging meanwhile.
- A drive is followed by its UUID: a renamed SSD is followed, a different drive under the same
  name gets nothing.

### Fixed

- Once background work starts, the profile stays locked until the app process exits, even if
  shutdown has finished and an SSD write is still blocked. Restarting cannot overlap that
  writer. Early startup failures still release the profile for another attempt.

## [0.0.1] - 2026-11-15

The first public build, a beta.

### Added

- Backup of an Android phone over USB or Wi-Fi to a Mac folder and an SSD folder, by backup
  sets (camera, screenshots, WhatsApp media and chat backups, documents, downloads,
  recordings), with SHA-256 verification, resumable transfers, crash-safe publishing and a
  dated backup confirmation. Existing backup folders can be adopted in place.
- Free space: exact duplicates, near-duplicate photos and videos, junk suggestions from an
  on-device CLIP model and Apple Vision text reading (English and Hindi), teaching by Junk/Keep
  labels, calibration, browse-and-delete, and Clean slate.
- Deletion only after every required backup copy and the phone file are re-hashed, with phone
  identity checks, typed confirmation for large deletes, an optional quarantine, and restore to
  the phone.
- Apps: sizes, last use and safe uninstall, with WhatsApp and authenticators protected.
  WhatsApp chat backups are kept until you choose to remove old ones.
- Phone health report and a factory-reset checklist.
- An app icon and logo (an inverted pyramid of photo tiles ending in one kept, checked
  tile), in the Dock, the sidebar, setup and About; drawn by `make logo`.
- A macOS app (Apple Silicon, macOS 14+, ad-hoc signed) built by `make build`, with a crash
  dialog, logs and a self-check; release builds from version tags on GitHub Actions.

### Security

- Restore uploads go to a temporary name of Sift's own and move into place with a no-replace
  rename, so a restore can never remove or replace another file on the phone.
- Model suggestions are not ticked for deletion unless you turn that on (beta) and a
  calibration with at least 50 items passes on the current pipeline.

[Unreleased]: https://github.com/avina5hkr/sift/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/avina5hkr/sift/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/avina5hkr/sift/releases/tag/v0.0.1
