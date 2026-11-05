Sift is a local-only macOS app that backs up an Android phone.

Run `make dev` to start it.

## Develop without a phone

- `make dev` runs Sift against a fake phone (an invented file tree, no real device) and keeps
  its data in the isolated `.dev` folder.
- `make fake-phone N=500` prints a summary of that fake phone: file count, total size and the
  edge cases it contains. Add `--json PATH` (run `uv run python scripts/fake_phone.py --json PATH`)
  to dump the tree, without file contents, for inspection.
- Fake mode needs `SIFT_HOME`, the folder where Sift keeps its data. Sift refuses to start with
  `--fake-phone` without it, so a fake phone can never touch your real data.
