#!/usr/bin/env bash
set -euo pipefail

# Compare to HEAD so staged changes and untracked (even ignored) outputs also fail.
paths=(frontend/src/theme/venus-stone-overrides.css src/live_clipper/web_static/react)
changes=$(git diff --name-status HEAD -- "${paths[@]}")
untracked=$(git ls-files --others -- "${paths[@]}")
if [[ -n "$changes" || -n "$untracked" ]]; then
  printf '%s\n' 'Generated resources differ from the commit. Run npm --prefix frontend run build and commit the generated files:'
  printf '%s\n' "$changes" "$untracked"
  exit 1
fi
