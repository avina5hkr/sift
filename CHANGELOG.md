# Changelog

All notable changes to Sift are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). While Sift is 0.x, a minor version may change
behaviour.

## [Unreleased]

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

[Unreleased]: https://github.com/avina5hkr/sift/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/avina5hkr/sift/releases/tag/v0.0.1
