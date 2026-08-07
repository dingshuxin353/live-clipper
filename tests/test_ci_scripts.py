from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/assert_backend_bundle.py"


def _valid_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "live-clipper-backend"
    internal = bundle / "_internal"
    required_files = (
        "mlx/lib/libmlx.dylib",
        "mlx/lib/mlx.metallib",
        "mlx_whisper/assets/mel_filters.npz",
    )
    for relative in required_files:
        path = internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")
    return bundle


def _run(bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(bundle)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_assert_backend_bundle_accepts_mlx_runtime_without_weights(tmp_path: Path):
    result = _run(_valid_bundle(tmp_path))

    assert result.returncode == 0
    assert "MLX runtime present and model weights absent" in result.stdout
    assert result.stderr == ""


def test_assert_backend_bundle_rejects_missing_runtime_assets(tmp_path: Path):
    bundle = _valid_bundle(tmp_path)
    (bundle / "_internal/mlx/lib/mlx.metallib").unlink()

    result = _run(bundle)

    assert result.returncode == 1
    assert "required MLX runtime asset missing" in result.stderr
    assert "mlx.metallib" in result.stderr


def test_assert_backend_bundle_rejects_packaged_model_weights(tmp_path: Path):
    bundle = _valid_bundle(tmp_path)
    weights = bundle / "_internal/mlx_whisper/weights.npz"
    weights.write_bytes(b"model weights")

    result = _run(bundle)

    assert result.returncode == 1
    assert "model weights must not be packaged" in result.stderr
    assert "weights.npz" in result.stderr
