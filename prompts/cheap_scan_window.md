# Cheap Model: Scan Window

You will receive one transcript window with timestamps. Find 0-3 candidate clips that may be worth publishing.

Return strict JSON with:

- `window_id`
- `candidates`
- candidate `start`, `end`, `score`, `clip_type`, `hook`, `core_value`, `reason`, `risk`, `suggested_context_before`, `suggested_context_after`


Use a numeric `score` from 0 to 10 inclusive (not a percentage). All times are seconds on the supplied transcript timeline, with `window.start <= start < end <= window.end`. Context values are nonnegative seconds. Each candidate must have a unique ID if an ID is supplied. Return `candidates: []` only when the content has no suitable clips.
