# Agnes: Refine Livestream Clip Candidate

You are reviewing one livestream clip candidate for a sponsored workflow that must showcase Agnes as a practical AI model for real work.

Decide whether this candidate should advance to final human/Codex review. Prefer clips that:

- show a concrete work problem, workflow, AI tool use, or useful judgment
- make Agnes / AI feel practical for ordinary creators, operators, or knowledge workers
- have a strong opening hook or clear tension in the first few seconds
- can stand alone without heavy livestream context
- are not just setup chatter, stream debugging, repeated explanation, or overly narrow technical noise

Return strict JSON only:

{
  "candidate_id": "same id as input",
  "keep": true,
  "refined_score": 0-10,
  "commercial_fit": 0-10,
  "hook_strength": 0-10,
  "standalone_value": 0-10,
  "clarity": 0-10,
  "suggested_title": "short Chinese title",
  "selection_reason": "short Chinese reason",
  "weaknesses": ["short weakness"],
  "recommended_adjustments": ["optional trim or review note"]
}

Use `keep: false` for candidates that are weak, repetitive, too dependent on context, or not useful for promoting Agnes.
