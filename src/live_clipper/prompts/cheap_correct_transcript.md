# Cheap Model: Correct Transcript

You will receive ASR transcript sentences with timestamps and a glossary of preferred terms.

Correct likely ASR mistakes while preserving:

- sentence order
- `start` and `end` timestamps
- speaker labels when present
- the original meaning and tone

Use glossary `canonical` terms when the ASR text clearly matches one of the listed `common_mistakes`.

Do not rewrite for style. Do not invent missing content. If unsure, keep the original wording.

Return exactly one JSON object. Do not return a top-level JSON array. Do not wrap the JSON in Markdown fences. Do not include explanations before or after the JSON.

The response must use this exact top-level shape:

```json
{
  "sentences": [
    {
      "start": 0.0,
      "end": 1.0,
      "text": "corrected sentence text",
      "speaker": null
    }
  ],
  "corrections": [
    {
      "start": 0.0,
      "end": 1.0,
      "original_text": "original ASR text",
      "corrected_text": "corrected text",
      "reason": "short reason",
      "confidence": 0.9
    }
  ]
}
```

`sentences` must contain exactly the same number of items as the input `sentences`, in the same order.
Use an empty `corrections` array when no changes were made.
