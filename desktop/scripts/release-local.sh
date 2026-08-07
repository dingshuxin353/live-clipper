#!/usr/bin/env bash
# Local fallback for the CI release: same signing/notarization, publishes to GitHub.
# Requires env: CSC_LINK CSC_KEY_PASSWORD APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID GH_TOKEN
set -euo pipefail
cd "$(dirname "$0")/.."
for var in CSC_LINK CSC_KEY_PASSWORD APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID GH_TOKEN; do
  if [ -z "${!var:-}" ]; then
    echo "missing env: $var" >&2
    exit 1
  fi
done
npm ci
npm run build:backend
../.venv/bin/python ../scripts/ci/assert_backend_bundle.py
npx electron-builder --mac --publish always
