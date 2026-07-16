#!/usr/bin/env bash
# Freeze the Python backend with PyInstaller (onedir).
set -euo pipefail

cd "$(dirname "$0")/../.."  # repo root

if ! .venv/bin/python -c "import PyInstaller" >/dev/null 2>&1; then
  .venv/bin/pip install "pyinstaller>=6.10"
fi

MLX_FLAGS=()
if .venv/bin/python -c "import mlx_whisper" >/dev/null 2>&1; then
  MLX_FLAGS=(--collect-all mlx --collect-all mlx_whisper)
  echo "[build-backend] bundling mlx_whisper"
else
  echo "[build-backend] mlx_whisper not installed; packaged app will need ASR backend 'openai'"
fi

rm -rf desktop/backend-dist desktop/backend-build

.venv/bin/pyinstaller --noconfirm --clean \
  --name live-clipper-backend \
  --distpath desktop/backend-dist \
  --workpath desktop/backend-build \
  --specpath desktop/backend-build \
  --add-data "$PWD/src/live_clipper/prompts:live_clipper/prompts" \
  --add-data "$PWD/src/live_clipper/web_static:live_clipper/web_static" \
  --exclude-module torch \
  --exclude-module torchvision \
  --exclude-module torchaudio \
  "${MLX_FLAGS[@]}" \
  desktop/backend_entry.py

echo "[build-backend] done -> desktop/backend-dist/live-clipper-backend/"
