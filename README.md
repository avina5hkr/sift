# Sift

Sift is a local-only macOS app that backs up an Android phone to your Mac and an external SSD,
checks every copy, and then helps you free space on the phone safely. Nothing leaves your Mac.

- **Backup**: copies the phone's photos, videos, WhatsApp media and chat backups, documents and
  recordings over USB (or Wi-Fi) into a Mac folder and an SSD folder, and verifies every copy.
- **Free space**: suggests duplicates, near-duplicates and junk (it learns from your choices),
  lets you browse and delete, or keep a short list and clear the rest ("Clean slate"). A file
  is deleted from the phone only after its verified copies are on every backup folder, and
  only after you confirm.
- **Apps and health**: app sizes and last use, safe uninstall (WhatsApp and authenticators are
  protected), a phone health report and a checklist for a factory reset you do yourself.

Sift never changes or deletes a file in your backup folders, except old WhatsApp chat backups
or quarantined files you choose to remove.

## Install

1. Open `Sift-VERSION.dmg` and drag Sift to Applications.
2. The first time, right-click Sift and choose **Open** (Sift is signed for personal use, not
   notarised, so macOS asks once). Or: System Settings, Privacy and Security, Open Anyway.
3. Follow the setup: turn on USB debugging on the phone, connect it, choose the Mac folder and
   the SSD folder. Sift asks before it downloads adb (Google's platform-tools) and the image
   models; after that it works offline. macOS asks once for access to the external drive.

Requires an Apple Silicon Mac with macOS 14 or later, and an Android phone (Wi-Fi needs
Android 11+).

## Remove

Drag Sift to the Bin, then delete `~/Library/Application Support/Sift` and
`~/Library/Caches/Sift`. Your backups are never touched.

## Build

```bash
make build          # web UI, locked Python dependencies, PyInstaller, ad-hoc signing, .dmg
```

The result is `build/package/Sift-VERSION.dmg` (arm64). `build/package/dist/Sift.app/Contents/
MacOS/Sift --self-check` checks that every native part loads. To start a build with your own
teaching, save it from the app (Teach, Save teaching) and build with
`SIFT_TEACHING=path/to/teaching.json make build`; it is bundled into that build only and loaded
once on a fresh install. Nothing is bundled unless you set it. A teaching file holds your
junk/keep labels by file hash, not a trained model: it helps only on the same pictures.

## Develop

- `make dev` runs Sift against a fake phone (an invented file tree, no real device) and keeps
  its data in the isolated `.dev` folder.
- `make test`, `make lint`, `make web-test`, `make e2e`; `make bench` for the benchmarks.
- `make fake-phone N=500` prints a summary of the fake phone. Fake mode needs `SIFT_HOME`, so a
  fake phone can never touch your real data.
- Read `CLAUDE.md` and `docs/CODEMAP.md` before changing code.

Third-party licences: `THIRD_PARTY.md`.
