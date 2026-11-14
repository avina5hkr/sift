# Releasing Sift

Releases are built and published by GitHub Actions
([.github/workflows/release.yml](../.github/workflows/release.yml)) when a version tag is pushed.
Nothing is uploaded by hand.

## Versions

Sift uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`, with a `v` prefix on
the tag (`v0.2.0`). The version lives in one place, `pyproject.toml`. The app reads it from the
package metadata (About, logs), and the build writes it into `Info.plist`. The release workflow
refuses a tag that does not match `pyproject.toml`.

## Steps

1. **Start from a green `main`.** CI must pass on the commit you will release.
2. **Bump the version** in `pyproject.toml`, then run `uv lock` (the lock file records Sift's
   own version, and `uv sync --locked` fails if they differ).
3. **Update `CHANGELOG.md`:** move the items under *Unreleased* to a new
   `## [X.Y.Z] - YYYY-MM-DD` section and add its compare link at the bottom.
4. Commit (`release: vX.Y.Z`) and push to `main`. Wait for CI.
5. **Optional dry run:** in GitHub, open *Actions → Release → Run workflow* on `main`. It runs
   every check and build step and uploads the dmg as a workflow artifact for a day, without
   tagging or publishing anything. Download it and try it on a Mac.
6. **Tag and push:**

   ```bash
   git tag -a vX.Y.Z -m "Sift vX.Y.Z"
   git push origin vX.Y.Z
   ```

## What the workflow does

| Job | Runs on | What it does |
| --- | --- | --- |
| `build` | `macos-15` (Apple Silicon), read-only, no secrets | Checks that the tag is `vMAJOR.MINOR.PATCH`, matches `pyproject.toml`, points to a commit on `main`, and has no release yet. Runs `make lint` and `make test`. Builds with `packaging/build_dmg.sh` (never with a teaching file). Verifies the frozen app: version, arm64, code signature, no teaching file, `--self-check`. Writes `Sift-X.Y.Z-macos-arm64.dmg` and its `.sha256`. |
| `publish` | `ubuntu-24.04`, `contents: write` | Tag pushes only. Re-checks the checksum and creates the release "Sift vX.Y.Z" with the dmg, the checksum, install notes and GitHub's generated change list. It never replaces an existing release or asset. |

Every action is pinned to a commit SHA, with its version in a comment. To update one, resolve the
new tag's commit (`git ls-remote https://github.com/OWNER/ACTION refs/tags/vX.Y.Z^{}`) and update
both the SHA and the comment, in `ci.yml` too.

## If something fails

- **Before `publish` ran:** fix the problem on `main`, then move the tag to the fixed commit:
  `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`, and tag again.
- **After a release was published:** do not replace it. Release `vX.Y.Z+1` with the fix.

## Signing and notarisation

Release builds are ad-hoc signed and not notarised, because notarisation needs a paid Apple
Developer ID. Users approve the first launch once (System Settings → Privacy & Security → Open
Anyway); the README and the release notes say so. Adding a Developer ID later means a signing
step with secrets in a protected environment, `codesign --options runtime` with entitlements,
and `xcrun notarytool` plus `stapler` before the dmg is packaged.
