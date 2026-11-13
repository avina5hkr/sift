# Sift: working rules for Claude

Read this first, then `docs/CODEMAP.md` (module map) instead of opening files to find things.
Run `make codemap` after adding or renaming modules.

## How a milestone is built (from M3 on)

1. **Plan small.** A milestone plan lists features, interfaces and the invariants each one
   touches. It does not contain code or test bodies. About 1 page per milestone.
2. **Build natively.** Claude implements features directly in the main session. A subagent only when
   it clearly saves tokens (parallel independent work, or a large read whose conclusion is enough). No per-task reviewer, no fix-round loops.
3. **Test per feature, not per step.** Write the feature, then its tests, then run only the
   affected test files (`uv run pytest -q tests/<area>`). Run the full suite, `make lint`,
   `make web-test` and `make e2e` once at the end of the milestone (and before any merge).
4. **Decide, don't ask.** Make reasonable assumptions, record each one as a line in the plan's
   "Assumptions" list, and keep going. Ask the owner only for something irreversible, a push or
   merge, or a step that needs the phone or the owner's hands.
5. **One review per milestone.** A single whole-branch review on the most capable model at the end,
   one fix pass, done. Add a focused review mid-milestone only for code that deletes files or
   writes to the phone (M8, M11, M12).
6. **Owner checks happen once, at the end of the milestone,** batched into one session.

## Token rules

- Read the code map, then `grep -n`, then read only the line ranges you need. Never read whole
  large files or diffs into context; write long output to a file and read slices.
- Pipe test output through `tail -n 5` (and `grep -E "FAILED|Error"` on failure).
- Status messages: one or two lines. No recaps of earlier work.
- Subagents only for genuinely parallel, independent work, briefed with file paths, not pasted text.

## Never

- Push, or add a Co-Authored-By trailer. Commit as the repo-local identity.
- Commit personal data (model names, serials, UUIDs, home paths) or the private spec.
- Write to or delete on the phone; kill or restart the adb server; touch the owner's real backup
  folders or the real `~/Library` in tests (`SIFT_TRACKER=off`, temp `SIFT_HOME`).
- Overwrite or delete a file in a backup destination (I1); skip the I2 copy sequence.
