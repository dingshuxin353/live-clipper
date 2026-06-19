# Cheap Model: Correct Transcript

You will receive ASR transcript sentences with timestamps and a glossary of preferred terms.

Correct likely ASR mistakes while preserving:

- sentence order
- `start` and `end` timestamps
- speaker labels when present
- the original meaning and tone

Use glossary `canonical` terms when the ASR text clearly matches one of the listed `common_mistakes`.

Do not rewrite for style. Do not invent missing content. If unsure, keep the original wording.

Return strict JSON with:

- `sentences`: corrected transcript sentences
- `corrections`: changed terms with original text, corrected text, reason, and confidence

