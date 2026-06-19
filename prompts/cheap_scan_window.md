# Cheap Model: Scan Window

You will receive one transcript window with timestamps. Find 0-3 candidate clips that may be worth publishing.

Return strict JSON with:

- `window_id`
- `candidates`
- candidate `start`, `end`, `score`, `clip_type`, `hook`, `core_value`, `reason`, `risk`, `suggested_context_before`, `suggested_context_after`

