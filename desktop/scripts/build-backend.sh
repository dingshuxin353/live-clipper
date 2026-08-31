#!/usr/bin/env bash
# Freeze the Python backend with PyInstaller (onedir).
set -euo pipefail

cd "$(dirname "$0")/../.."  # repo root

if ! .venv/bin/python -c "import mlx, mlx_whisper" >/dev/null 2>&1; then
  echo '[build-backend] MLX desktop runtime is required; refusing to build an incomplete backend.' >&2
  echo '[build-backend] Install it with: .venv/bin/pip install ".[mlx]"' >&2
  exit 1
fi

npm --prefix frontend ci
npm --prefix frontend run check
git diff --exit-code -- src/live_clipper/web_static/react

if ! .venv/bin/python -c "import PyInstaller" >/dev/null 2>&1; then
  .venv/bin/pip install "pyinstaller>=6.10"
fi

echo "[build-backend] bundling mlx and mlx_whisper"

rm -rf desktop/backend-dist desktop/backend-build

.venv/bin/pyinstaller --noconfirm --clean \
  --name live-clipper-backend \
  --distpath desktop/backend-dist \
  --workpath desktop/backend-build \
  --specpath desktop/backend-build \
  --add-data "$PWD/src/live_clipper/prompts:live_clipper/prompts" \
  --add-data "$PWD/src/live_clipper/web_static:live_clipper/web_static" \
  --collect-all huggingface_hub \
  --collect-all modelscope_hub \
  --exclude-module torch \
  --exclude-module torchvision \
  --exclude-module torchaudio \
  --collect-all mlx \
  --collect-all mlx_whisper \
  desktop/backend_entry.py

echo "[build-backend] done -> desktop/backend-dist/live-clipper-backend/"
