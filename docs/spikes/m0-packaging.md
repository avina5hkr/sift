# M0 packaging spike: PyInstaller with uv-managed CPython 3.14.7

Date: 2026-11-05. Purpose: de-risk milestone M10 early. No packaging code is committed; this is findings only.

## Result

**Works: yes.** PyInstaller bundles the app into a windowed, ad-hoc-signed `Sift.app` using the uv-managed
python-build-standalone CPython 3.14.7 (arm64, macOS 27.2). No hidden imports, no `--collect-submodules`, and no
hooks were needed: the brief's command worked on the first attempt. The bundle starts headless, serves the bundled
UI, applies the bundled migrations, and shuts down cleanly on SIGTERM.

**Scope of this result.** It covers only M0's runtime dependencies (fastapi, uvicorn, pywebview, tomlkit,
pydantic) and the standard library. It does not predict M10: PyInstaller usually needs hooks, hidden imports, or
dylib collection and signing work for native and ML packages. M10 still has to prove each of these in the frozen app:

- onnxruntime (native dylibs, model files)
- scikit-learn and scipy (native extensions, data files)
- numpy (native extensions; see the exclusion note under "Bundle size")
- Pillow and pillow-heif (native libs such as libheif, codec plugins)
- PyObjC Vision (the `Vision` framework wrapper, beyond the AppKit/WebKit/Foundation modules already seen)
- the bundled ffmpeg, ffprobe and adb binaries (placement under `Contents/Frameworks` or `Resources`, executable bits,
  nested code signing with the same identity, and hardened-runtime/notarization behaviour when spawned as subprocesses)

Not verified here: the visible pywebview window (see "Owner's manual check").

## Environment

| Item | Value |
| --- | --- |
| Python | 3.14.7 (uv-managed python-build-standalone) |
| PyInstaller | 6.22.3 (contrib hooks 2026.9), run via `uv run --with pyinstaller` (not added to `pyproject.toml` / `uv.lock`) |
| Platform | macOS 27.2, arm64 |
| Node (for `make web`) | 24.22.2 |

## Command that worked

```bash
make web
uv run --with pyinstaller pyinstaller --noconfirm --windowed --onedir --name Sift \
  --add-data "web/dist:web/dist" --add-data "src/sift/resources:sift/resources" \
  --distpath build/spike/dist --workpath build/spike/work src/sift/__main__.py
codesign --force --deep --sign - build/spike/dist/Sift.app
codesign --verify --deep --strict build/spike/dist/Sift.app
```

Build time is about 48 s. Gotcha: PyInstaller writes `Sift.spec` into the current directory by default. Pass
`--specpath build/...` (or clean it up) so it does not land in the repo root.

## Hidden imports

None needed **for M0's dependencies only** (see "Scope of this result"). Static analysis picked up everything
the M0 app needs at runtime:

- uvicorn loops and protocols (`uvicorn.loops.auto/asyncio`, `uvicorn.protocols.http.h11_impl`,
  `uvicorn.lifespan.on`, websocket impls) are all present in the PYZ archive, and the server answered HTTP from the
  frozen build.
- `webview` and `webview.platforms.cocoa` are in the PYZ archive, plus the pyobjc extension modules `objc`,
  `AppKit`, `Foundation`, `CoreFoundation` and `WebKit` under `Contents/Frameworks/` (confirmed in the
  `codesign --verify --verbose` output and the PyInstaller TOC). The non-macOS webview backends (winforms, gtk, qt,
  cef, android) were bundled as dead code; their missing-module warnings are harmless.
- `sift` package modules and the `--add-data` resources (`web/dist`, `sift/resources/migrations`) resolve through
  `sys._MEIPASS` and `importlib.resources` as designed.

Caveat: `webview.platforms.cocoa` is imported lazily when a window is created, so `--no-window` does not exercise it.
Its presence in the bundle was confirmed, its runtime behaviour was not.

## Bundle size

| Build | `du -sh Sift.app` |
| --- | --- |
| Brief's command, as is | 41 MB |
| Same, with `--exclude-module mypy --exclude-module hypothesis --exclude-module pytest --exclude-module setuptools --exclude-module numpy` | 32 MB |

`numpy` appears in that exclude list only because M0 does not use it (it came in via the dev environment). It must
NOT be excluded from M2 onwards, when numpy-using features arrive; copy the dev-tool excludes (`mypy`, `hypothesis`,
`pytest`, `setuptools`) into an M10 recipe, not the `numpy` one.

The 41 MB build contains dev-only packages (`mypy`, `hypothesis`, `setuptools`, `pytest` and friends) because
`uv run` uses the project `.venv`, which includes the `dev` dependency group. `uv run --no-dev` does not help: uv's
default sync is inexact and does not uninstall them from the existing `.venv`. The slim build was signed and passed
the same headless checks (status 200, bundled `index.html`, SIGTERM exit).

## Code signing

- `codesign --force --deep --sign - build/spike/dist/Sift.app` succeeded ("replacing existing signature"; PyInstaller
  already ad-hoc signs the bundle).
- `codesign --verify --deep --strict --verbose=2`: "valid on disk" and "satisfies its Designated Requirement".
- This is ad-hoc only: no Developer ID, no hardened runtime, no notarization. Gatekeeper behaviour for a downloaded
  copy was not tested.

## Headless verification (R24: no window, real app data untouched)

Run with `SIFT_HOME` set to a fresh temp folder (`build/spike/home`) so nothing touched
`~/Library/Application Support/Sift` or `~/Library/Caches/Sift` (the former did not exist afterwards; the temp folder
was deleted at the end).

```text
$ SIFT_HOME=$PWD/build/spike/home SIFT_DEV_TOKEN=spike \
    build/spike/dist/Sift.app/Contents/MacOS/Sift --no-window --port 0
Sift running at http://127.0.0.1:51799/?t=spike

$ curl -i -H 'X-Sift-Token: spike' $U/api/status
HTTP/1.1 200 OK   server: uvicorn   content-type: application/json
{"app":{"version":"0.1.0","schema_version":1,"config_warnings":[]},"device":null,"ssd":null,
 "mac_folder":{"path":"~/PhoneBackup","exists":false,"free_bytes":...},"jobs":[]}
```

- `schema_version` 1 proves the bundled migrations were found and applied.
- `curl $U/?t=spike` returned 200 with the bundled `index.html` (`<title>Sift</title>`, hashed asset links), not the
  "UI is not built" page; `/assets/index-*.js` (218798 bytes) and `/favicon.svg` also returned 200. `SIFT_WEB_DIST` was
  unset, and the frozen lookup order checks `sys._MEIPASS` before the repo's `web/dist`, so this came from the bundle.
- `/api/status` without the token returned 401; `/` without a token returned 200 (static shell).
- Data location: `build/spike/home/support/` contained `state.db` (135168 bytes), `sift.lock`, `logs/`, `backups/`,
  `models/`, `platform-tools/`; `build/spike/home/cache/` contained `thumbs/`. Nothing was created in `~/Library`.
- SIGTERM: process exited within 3 s.
- Lock release and single-instance: a second headless start succeeded after the SIGTERM (new URL printed, status 200,
  stopped again by SIGTERM). A third start while the second was still running printed `Sift is already running.`
  and exited with code 1.
- The slim build (32 MB) passed the same start / status / index / SIGTERM checks.

## Owner's manual check (not done here)

Nobody opened a window. Remaining, by the owner, on a real screen:

1. `open build/spike/dist/Sift.app` (left in place; `build/` is git-ignored). Expect the Sift window with the shell,
   "No phone" and a green events dot. Use a throwaway `SIFT_HOME` if the real app data should stay untouched (a
   Finder/`open` launch does not inherit shell env; use `open --env SIFT_HOME=... build/spike/dist/Sift.app` or run
   the binary from Terminal without `--no-window`).
2. Confirm `import webview` and the Cocoa/WKWebView backend initialise inside the frozen app (the only part that
   `--no-window` cannot exercise), that the window closes cleanly, and that quitting from the Dock/menu stops the
   server and releases the lock.
3. Optionally check Gatekeeper behaviour for a quarantined copy.

## Recommendations for M10

1. Keep PyInstaller `--onedir --windowed` with the uv-managed CPython; no python.org framework build was needed, so
   that fallback was not tried (none is installed here).
2. Build from a runtime-only environment. Either build in a fresh venv created with `uv sync --no-dev --exact`
   (preferred, reproducible) or pass `--exclude-module` for dev tools (never `numpy`; see "Bundle size"); the
   excludes took the bundle from 41 MB to about 32 MB. PyInstaller has to get into the runtime-only environment
   somehow: add a `build` dependency group containing `pyinstaller` to `pyproject.toml` and run
   `uv sync --no-dev --exact --group build`, or run `uv pip install pyinstaller` into the fresh venv. Both are
   untested in this spike (the spike used `uv run --with pyinstaller` against the dev `.venv` and the exclude list).
   Add a `.spec` file to the repo at M10 instead of a long command line, and pass `--specpath` so no stray `.spec`
   lands in the root.
3. Prefer an explicit allow-list: restrict the webview backends to cocoa (e.g. `--exclude-module` for the winforms,
   gtk, qt, cef and android platforms) to cut dead code and warnings.
4. Add a headless smoke test to the M10 CI/release script mirroring the verification above (binary
   `--no-window --port 0` + `SIFT_HOME` temp dir + `SIFT_DEV_TOKEN`; assert `/api/status` 200 with `schema_version`,
   `/` serves the real `index.html`, SIGTERM exits). It needs no display.
5. Spec 14 chose ad-hoc signing for personal use, so notarisation is out of scope. A Developer ID signature, hardened
   runtime with the entitlements PyInstaller extension modules need (e.g.
   `com.apple.security.cs.disable-library-validation` if bundled `.so` files are not signed by the same team), and
   notarisation would only be needed if distribution is ever wanted; none of this was tried in the spike.
   PyInstaller's `--codesign-identity` and `--osx-entitlements-file` options cover it if that day comes.
6. Spec 14 already decided how external tools ship: ffmpeg and ffprobe are bundled (static arm64 builds), while adb
   (platform-tools) and the CLIP models are downloaded on first use, with the user's consent and a pinned SHA-256, into
   the app-data folder (the app already has `platform-tools/` and `models/` folders under `SIFT_HOME`). Nothing is left
   to decide at M10; it only has to implement that.
7. Set `CFBundleIdentifier`, version, icon, and `LSUIElement`/Dock behaviour via `--osx-bundle-identifier` / `--icon`
   (the spike uses PyInstaller defaults).
8. `multiprocessing.freeze_support()` in `sift.app.main()` is already in place; keep it first for any future worker
   processes in the frozen app.
