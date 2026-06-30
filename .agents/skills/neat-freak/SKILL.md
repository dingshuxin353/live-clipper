# neat-freak

Use this skill when a StarWork single-project workspace needs end-of-session cleanup, project memory reconciliation, or a careful pass over docs before pausing work.

## Purpose

This bundled StarWork edition is intentionally small. It helps an agent close a work session without losing important context.

## Workflow

1. Read the project entry rules, usually `AGENTS.md`.
2. Read the current work file from `.starwork/workspace.json` and the Kit defaults.
3. Check whether product facts, process notes, and current task records are stored in the correct places.
4. Summarize what changed in this session.
5. Update the current work record if the workspace rules allow it.
6. List any unresolved risks, follow-ups, or decisions that need the user's attention.

## Rules

- Do not delete or rewrite user content during cleanup.
- Do not move files unless the user explicitly asks.
- Keep product facts separate from process notes.
- If the workspace has project-specific process folders, keep their progress and registry status consistent.
- If unsure where something belongs, explain the ambiguity instead of guessing.

## Output

Return a concise cleanup report:

- What was reconciled.
- What still needs attention.
- Which files were updated, if any.
