# Safety rules, preference defaults, and model identity

Sift separates three kinds of behaviour. Safety rules are not settings: they hold whatever the
owner configures. Preference defaults are starting points the owner can change, by teaching,
by choosing, or in Settings. Model identity says which analysis results belong together.

## Safety rules (always on)

- **A phone file is deleted only when every required backup folder holds a good copy.** The
  copy must be present, the right size, and its content (SHA-256 read from the disk at that
  moment) must equal the phone file's hash. This is checked during the delete checks and again
  just before each delete. A stored "verified" date alone is never enough.
- **The phone file must be the one that was checked.** The phone re-hashes each batch just
  before its deletes, and each file is re-stat'ed (size and mtime) right before its own delete.
  The phone has no compare-and-delete, so an edit in the few seconds between that hash and the
  delete is not caught.
- **Restores are proven, and only ever touch their own upload.** The backup copy is hashed
  before it is pushed. The upload goes to a hidden temporary name in the target folder that only
  Sift makes (`.sift-upload-<random>.part`), and the phone hashes it there. A wrong or failed
  upload is removed by that name, so no other file can be removed. A good upload is moved to its
  name, or a free `_restored` name, with a no-replace rename (`mv -n`, checked afterwards): a
  file that appeared at that name meanwhile stays. A restore never writes over a file on the
  phone or in a backup folder. If the phone disconnects mid-upload, a hidden partial file under
  Sift's temporary name can remain.
- **Nothing in a backup folder is overwritten.** The only deletes there are quarantine purge
  and old WhatsApp chat backups the owner chooses to remove. Quarantine moves use an atomic
  no-replace rename, and an interrupted restore is repaired at start from its recorded
  destination.
- **The never-delete list** (Android/data and obb, the WhatsApp Databases and Backups folders)
  always applies, together with the phone identity check, the typed count for large or unusual
  deletes, and protected apps (WhatsApp and authenticators).
- **When in doubt, keep.** A picture whose words could not be read, whose reading was made for
  other languages than the ones set now, or that is not analysed yet,
  is never suggested as junk, and Clean slate keeps it. Unreadable files, document words, IDs,
  camera originals on the suggestion tabs, and protected folders are Protected.
- **Pre-ticking needs proof and consent.** Model suggestions are pre-ticked only when the owner
  turns on "Tick junk suggestions for me" (off by default during the beta) and a calibration
  with at least 50 checked items and zero wrong passed on exactly the current pipeline (see
  Model identity). Exact duplicate copies are pre-ticked as before; they hold identical bytes.
  Look-alike videos are never pre-ticked: matching sampled frames does not show the sound, or
  the frames in between, are the same.

## Preference defaults (the owner can change them)

- **What counts as junk.** The built-in suggestions start from short descriptions of greeting
  cards, quotes, memes and religious forwards (`cleanup/suggest.py`), plus word lists for
  greetings and documents (editable in the app's lexicons folder). These are guesses: each
  group's suggestion moves with the owner's teaching. Nothing is deleted without the owner
  ticking and confirming it.
- **Which copy to keep.** Among duplicates, the default keeper is the camera original, then
  Pictures and DCIM, then WhatsApp received, then sent, then other folders, then the earliest
  date (`cleanup/candidates.py::keeper_rank`). The owner can tick any copy instead.
- **Near-duplicate pre-ticks.** An image copy is pre-ticked when its dHash is within 4 of the
  keeper's (`cleanup/near_tabs.py`). It is a convenience; the delete checks still apply.
- **Provisional beta policies.** The calibration minimum (50 checked items, zero wrong) and the
  group weight (one label given to a group counts as at most 5 decisions) are provisional
  choices for the beta, to be revisited with more owners' data. The group weight applies
  everywhere labels are learned from: training weights, class balance, the choice of
  regularisation, probability calibration, the label minimums and counts, the neighbour vote and
  support (a group takes at most 5 neighbour places), and the Teach page suggestions.
- **Thresholds in Settings** (scoring, clustering, health) are preferences within validated
  limits. Limits that are safety-related are enforced. For example, the near-duplicate hash
  distances cannot be set looser than 6 (dHash) and 8 (pHash).

## Model identity

Analysis results are used together only when they come from the same pipeline:

- **Embeddings:** the CLIP model `clip-vit-b32-onnx`, version `CLIP.version`, with pinned file
  hashes (`analysis/models.py`). Each stored embedding records `embed_version`, and features
  and training use only embeddings of the current version.
- **Text readings:** each records the reader version and the language list it was asked for
  (`ocr_lang`). A reading for another list counts as not checked and is read again.
- **The approval fingerprint** (`cleanup/junk.py::model_fingerprint`) covers the labels, the
  threshold, the embedding model and its files, the OCR version and languages, `FEATURES_VERSION`,
  `CLASSIFIER_VERSION`, the word lists as they read now, and the scoring settings. A
  calibration approval and any pre-ticks end when any of these change.
- **The trained model cache** is keyed by this phone's analysis inputs, so it never crosses
  phones or survives a change.
- **Changing the model, features or classifier means bumping its version.** Embeddings are then
  recomputed, and calibration must be done again.

## Teaching files

A teaching file (`Teach, Save teaching`) is the owner's labels by file hash, with where each
came from (a picture's own label, or one label given to a group). It is not a trained model,
and it helps only where the same pictures exist again. Builds bundle one only when
`SIFT_TEACHING` names it. A public build carries no one's labels.
