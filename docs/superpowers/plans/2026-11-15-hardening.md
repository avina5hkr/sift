# Hardening pass: external review of backup, cleanup, duplicates and learning

Working method: `CLAUDE.md`. Every finding was reproduced with a failing test on the fake phone
and temporary backup folders before it was fixed (`tests/hardening/`). Rules and defaults are
summarised in `docs/defaults-and-safety.md`.

## Confirmed and fixed

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Same-size backup damage authorised a delete | Every required copy is hashed fresh in the checks and again just before each delete (`cleanup/verify.py`); a damaged copy reports `backup_corrupt` |
| 2 | Restore accepted damaged content and recorded the original hash | `archive.push_verified`: hashes the source, then the phone hashes the result; a wrong upload is removed; used by archive and quarantine restore |
| 3 | A phone edit keeping size and mtime was deleted | The phone re-hashes each batch right before its deletes |
| 4 | An interrupted quarantine restore lost the tracked location | `quarantine.restore_to` is recorded before the move; start-up finishes or undoes it (migration 0014) |
| 5 | An OCR failure counted as "no text" | A failure leaves `ocr_version` unchanged and records `ocr_error`; it is retried; earlier failures are reopened (migration 0015) |
| 6 | Calibration passed with one item; it survived word-list changes | At least 50 items; a failure only raises the threshold; the fingerprint covers the model files, OCR, feature and classifier versions and the word lists |
| 7 | A group label counted as hundreds of decisions; groups leaked across validation folds | A group counts at most 5 decisions (weights, the 30-each minimum, counts); folds keep groups together; scaling is fitted inside the folds |
| 8 | Look-alike videos could be pre-ticked | Near-duplicate videos are never pre-ticked |
| 9 | Settings that did nothing or were silently capped | Languages and workers honoured; hash limits explicit (≤6, ≤8); six dead settings retired |
| 12 | Teaching file lost provenance; the build bundled it by default | Format 2 keeps the source and group (migration 0016); bundling only with `SIFT_TEACHING` |
| 13 | Near-duplicate grouping stored every pair; no Stop | Union-find without a pair list (proven to give identical groups); Stop between buckets |
| Sec | Quarantine move: exists-check then rename | Atomic no-replace rename; token checks pinned on the newer sensitive routes |

## Deliberate policies, not defects

- 10: built-in junk cues (greetings, memes, devotional forwards) and the keeper order are
  preference defaults: documented; teaching moves them, and nothing deletes without review.
- 11: one fixed embedding model with pinned files; its identity is now in the fingerprint and
  `docs/defaults-and-safety.md` says what a model change requires.

## Deferred

- Clustering (`cluster.py`) is quadratic in time (5 s at 30,000 pictures, 23 s at 60,000) and
  links each picture to its 16 nearest neighbours (an approximation used only for Teach
  groups, never for deletion). It can be stopped and its memory stays linear.
- Near-duplicate buckets are still compared pair by pair: 12 s for 30,000 random pictures,
  68 s for 10,000 near-identical ones. Bigger band keys would cut the time but use about four
  times the memory.
- Stronger video equivalence (audio fingerprints, dense frames) before any video is pre-ticked.

## Migration for existing installs

- 0014–0016 run on start. Calibration approvals end, because the fingerprint changed: run one
  calibration of at least 50 items. Images whose text reading had failed are read again on the
  next Analyze. Teaching saved before this change loads as format 1 (every label as a picture's
  own); save it again to keep group provenance.

## Follow-up (second review)

| Finding | Verdict | Fix |
| --- | --- | --- |
| Upload cleanup could delete an unrelated phone file | Confirmed: a file appearing at the chosen name made `push` fail with "already exists", and the cleanup removed that file | Uploads go to Sift's own random hidden name, are hashed there, and move into place with `mv -n` (checked afterwards; `rename_new` on the transports); cleanup only removes names with Sift's prefix. Archive restore, quarantine restore and the contacts upload (`push_one`) |
| Calibration identity ignored OCR languages | Confirmed; also stored readings were never redone after a language change | Each reading records the configured list; a reading for another list is not checked (protected, kept by Clean slate) and is read again; the list is in the fingerprint |
| Probability calibration discarded group weights | Confirmed: Platt and isotonic were unweighted, the isotonic switch and class balance counted pictures | Weighted Platt and isotonic, weighted class balance, isotonic from 200 decisions |
| Neighbour vote and support ignored groups | Confirmed, and the Teach page suggestions too | A group takes at most 5 neighbour places (its closest members); Teach suggestions use weighted minimums and say |
| Model pre-selection | Policy | Opt-in `cleanup.precheck_suggestions`, off; `never_precheck` retired, so existing installs are off too |

Assumptions: F1. `mv -n` (toybox) is the no-replace rename; its result is verified by stat,
so a phone without it refuses restores rather than risking a file. The owner's phone (Android
16, toybox 0.8.12) lists `-n`; a real restore is part of Gate C. F2. In the neighbour vote a
group's cap is applied to places rather than to per-picture weights, so a big group cannot crowd
out other labels. F3. Readings made before this change carry the languages Vision used, which
match the default list, so default installs read nothing again.

Migration: none for the schema. Pre-ticking of model suggestions is off for everyone until the
owner turns it on in Settings, General. A changed language list reopens every reading.
