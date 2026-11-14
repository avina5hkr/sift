# Contributing to Sift

Thank you for helping. Sift deletes files from people's phones, so the bar for correctness is
high. The rules below keep it safe for everyone who uses it.

## Before you start

- For anything bigger than a small fix, open an issue first to agree on the approach.
- Read [docs/defaults-and-safety.md](docs/defaults-and-safety.md) (what must never break) and
  [docs/CODEMAP.md](docs/CODEMAP.md) (where things live).
- AI coding assistants: [CLAUDE.md](CLAUDE.md) holds the working rules used to build Sift; they
  apply to human contributors too.

## Development setup

Requirements: macOS 14+ on Apple Silicon, the Xcode Command Line Tools,
[uv](https://docs.astral.sh/uv/) and Node.js 24 (`web/.nvmrc`).

```bash
uv sync --locked
npm --prefix web ci
npm --prefix web exec playwright install webkit   # for make e2e
make dev                                          # Sift against a fake phone, data in .dev/
```

## Checks

Run these before opening a pull request. CI runs the same on every push.

```bash
make lint        # ruff (lint and format) and mypy
make test        # Python tests
make web-test    # oxlint, TypeScript and Vitest
make e2e         # Playwright, against the fake phone
```

While working, run only the tests you touched (`uv run pytest -q tests/<area>`), and the full set
once at the end. Run `make codemap` after adding or renaming a module.

## Safety rules for code and tests

These are not negotiable, and reviewers check them:

- **Tests never touch a real phone or real data.** Use the fake phone (`sift.device.fake_*`), a
  temporary `SIFT_HOME`, temporary backup folders and `SIFT_TRACKER=off`. Tests that need a real
  device are marked `realphone` and are not part of the normal run.
- **Never kill or restart the user's adb server**, and never write to a phone outside the
  delete, restore, uninstall and push paths that already exist.
- **Never overwrite or delete a file in a backup destination** (invariant I1). Backups are
  written with an exclusive create and verified by hash.
- **Deletion stays behind its checks:** every required backup copy is re-hashed, the phone file
  is re-hashed, the phone's identity is checked, and the user confirms. Do not weaken a check to
  make a test pass.
- **When in doubt, keep.** A new analysis result that may be missing or stale must count as "not
  checked", never as "safe to delete".
- Code that deletes files or writes to the phone needs a regression test that fails without the
  change, and gets a focused review.

## Style

- Python 3.14, fully typed (mypy), formatted with ruff (line length 100).
- TypeScript and React for the UI, linted with oxlint.
- Every message the user can see is plain English: no jargon, no stack traces. New exception
  types get a sentence in `src/sift/services/plainwords.py`.
- Keep modules small with one job, and add a one-line docstring at the top (the code map uses it).

## Commits and pull requests

- One logical change per commit, with a message in the style of the history:
  `fix(restore): ...`, `feat(cleanup): ...`, `docs: ...`, `build: ...`.
- Never commit personal data: phone serials or models, Android IDs, home paths, real file names
  from a phone, or logs that contain them.
- The pull request template asks what you changed, how you tested it, and whether it touches
  deletion or phone writes.

## Releases

Maintainers release by tag; see [docs/releasing.md](docs/releasing.md).

By contributing, you agree that your contributions are licensed under the [MIT licence](LICENSE).
