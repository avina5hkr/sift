# M6: OCR with Apple Vision (English and Hindi)

Vision (`VNRecognizeTextRequest`, accurate level) on this Mac lists `hi-IN` among its supported
languages, so no Tesseract is bundled (plan assumption D1).

| Fixture | Languages asked | Read as |
|---|---|---|
| "Good Morning" card | hi-IN, en-US | `good morning` |
| document page | hi-IN, en-US | the text lines (`date invoice ...`) |
| "सुप्रभात" card | en-US, hi-IN | nothing |
| "सुप्रभात" card | hi-IN, en-US | `सुपूरभात` |
| "शुभ प्रभात" card | hi-IN, en-US | `शुभ पूरभात` |

- The language order matters: with en-US first Vision returns no Devanagari at all; with hi-IN
  first it reads Hindi and still reads English. Sift asks for hi-IN first.
- The conjunct `प्र` comes back as `पूर` because the fixture is drawn by Pillow without text
  shaping (no libraqm): Vision reads what was drawn. Real phone images are shaped correctly.
