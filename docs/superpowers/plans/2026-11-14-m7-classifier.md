# M7: Junk classifier and calibration (Gate B)

Working method: `CLAUDE.md`. Roadmap: section 5, M7. Spec: 9 (junk classification), 10 (tabs).
Scoring reads the database only; nothing touches the phone or a backup folder.

## Features, in build order

1. **Lexicons** (`resources/lexicons/greeting.yaml`, `document.yaml`; `cleanup/lexicons.py`):
   phrases and regexes from spec 9, copied into the app home on first run (editable there).
   `greeting_hit(text)`, `doc_hit(text)` on NFKC/casefolded OCR text; regexes run on the raw
   upper-cased text.
2. **Features** (`cleanup/features.py`): per content of this phone: embedding plus log size,
   log pixels, aspect, text_density, OCR line count, greeting_hit, doc_hit, origin flags
   (received, sent, private), has_camera_exif, is_screenshot, log(near copies), phone path
   count, log age in days. Standardised with the training mean and std.
3. **Model** (`cleanup/classifier.py`): kNN vote (k 15, cosine ≥ 0.80, weight cos⁴, support at
   0.85, protect_sim, keep_sim); logistic regression in numpy (L2, C from 5-fold CV on log loss,
   balanced classes), Platt sigmoid calibration (isotonic with 200+ labels);
   junk_score = min(p_lr, junk_vote). Cold start below 30 junk or 30 keep labels: no model.
4. **Tiers and reasons** (`cleanup/junk.py`): Protected, Keep, Suggested, Maybe, none, first
   match wins (spec 9 table); videos need 0.985 and a copy or a junk cluster; up to 3 reason
   chips from the features that fired. `rebuild_junk(db, device_id, cfg)` writes `junk` and
   `maybe` candidates (an exact-duplicate row wins its phone file), pre-checked only when
   precheck_allowed and not never_precheck. Files of the Recordings set are never scored.
   Cold start: lexicon greeting hits with 2+ copies go to Maybe.
5. **Retrain on label change**: every label write bumps `labels_rev`; the Junk tab, its counts
   and calibration rebuild when the stored rev differs (lazy, so J/K stays instant). A retrain
   increments `scoring_model_version` and clears precheck_allowed.
6. **Calibration** (`cleanup/calibration.py`, migration `0007_calibration.sql`): sample 300 of
   Suggested at random (all if fewer) into `calibration_item`; Correct / Wrong per item; when
   all are marked: 0 wrong → calibration row passed, precheck_allowed on; any wrong → those
   become keep (source calibration), T_suggest = highest wrong score + 0.01, a fresh sample is
   needed.
7. **API**: `GET /api/cleanup/candidates?tab=junk|maybe`, `GET /api/cleanup/junk/status`
   (labels per class, cold start, T_suggest, precheck_allowed, last calibration),
   `POST /api/labels` gains `source: manual|feedback` (Not junk), calibration
   `POST /start`, `GET`, `POST /mark`, under `/api/cleanup/calibration`.
8. **Web**: Junk tab (reason chips, Not junk button, "Teach the app first" in cold start),
   Show lower confidence toggle, Calibration screen (J = Correct, K = Wrong, progress, result).

## Exit

Tier tests and P5 (no Protected file in candidates, property test) pass;
`test_recordings_never_junk`; calibration stores its row. Full suite, lint, web, e2e green.
One whole-branch review. → Gate B (the owner labels real clusters and runs one calibration).

## Assumptions

- E1. pyyaml is added for the lexicons (spec says YAML); a broken lexicon file falls back to
  the built-in one with a log line.
- E2. Logistic regression and isotonic regression are written in numpy (no scikit-learn,
  ~100 MB less in the app); C is picked from (0.01, 0.1, 1, 10).
- E3. "Path inside a user-protected folder": a new `scoring.protected_folders` list (empty).
- E4. One candidate row per phone file: exact duplicates keep their row; junk does not
  replace it. Near-duplicate groups are computed live and may show the same file.
- E5. The Junk tab shows each file as its own one-item group, so the review UI is reused.
- E6. Calibration samples phone files of the current phone; a mark applies to the content.
- E7. Videos need a near-duplicate copy or a junk cluster in addition to the image
  corroboration; "Seen N times" and the cold-start "2+ copies" count extra phone paths too.
- E8. A file whose protection cannot be judged yet (image not read by OCR, video of unknown
  length, no embedding once a model exists) is not listed. Training uses every labelled
  content with an embedding, on any phone, present or not.
- E9. Candidates have one row per phone file and tab (migration 0007), so an exact duplicate
  can also be junk. The junk tabs rebuild whenever any input changes (labels, settings, word
  lists, phone files, analysis results); one rebuild at a time. Pre-checking is allowed only
  for the model fingerprint a calibration passed on; T_suggest is not capped at 1.

## Outcome

Built: lexicons, features, kNN vote and calibrated logistic regression (numpy), tiers with
Protected first, reasons, Junk and Lower confidence tabs, calibration, web Junk tab and
Calibration screen. Review: 2 Critical (stale or lost rebuild key) and 7 Important (threshold
cap, isotonic lookup, training only on present files, unjudgeable files listed, stale ticks,
calibration not tied to the model, exact rows hiding junk) fixed in one pass, with the minor
lexicon, screenshot-origin and calibration-race fixes. Carried: re-check Protected/Keep in the
M8 preflight (M8); Junk tab keyboard navigation, double-J on the last calibration item, a
protected label in the UI, and caching the trained model (M9 polish).
Final run: 1582 Python, 202 web, 13 e2e passed; lint clean. Gate B is the owner's: label 30+
junk and 30+ keep groups on the real phone, then one calibration.
