#!/usr/bin/env python3
"""Fail unless the frozen desktop backend contains MLX but no model weights."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = ROOT / "desktop/backend-dist/live-clipper-backend"

REQUIRED_DIRECTORIES = (
    "mlx",
    "mlx_whisper",
)
REQUIRED_FILES = (
    "mlx/lib/libmlx.dylib",
    "mlx/lib/mlx.metallib",
    "mlx_whisper/assets/mel_filters.npz",
)
FORBIDDEN_WEIGHT_PATTERNS = (
    "**/weights.npz",
    "**/*.safetensors",
    "**/*.gguf",
    "**/pytorch_model*.bin",
    "**/model*.safetensors",
)


def validate_bundle(bundle: Path) -> list[str]:
    """Return human-readable contract violations for a PyInstaller onedir bundle."""
    issues: list[str] = []
    if not bundle.is_dir():
        return [f"backend bundle not found: {bundle}"]

    internal = bundle / "_internal"
    if not internal.is_dir():
        return [f"PyInstaller _internal directory not found: {internal}"]

    for relative in REQUIRED_DIRECTORIES:
        path = internal / relative
        if not path.is_dir():
            issues.append(f"required MLX directory missing: {path}")

    for relative in REQUIRED_FILES:
        path = internal / relative
        if not path.is_file():
            issues.append(f"required MLX runtime asset missing: {path}")

    weights = sorted(
        {
            path.relative_to(bundle).as_posix()
            for pattern in FORBIDDEN_WEIGHT_PATTERNS
            for path in bundle.glob(pattern)
            if path.is_file()
        }
    )
    if weights:
        issues.append("model weights must not be packaged: " + ", ".join(weights))

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE,
        help=f"PyInstaller onedir bundle (default: {DEFAULT_BUNDLE})",
    )
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    issues = validate_bundle(bundle)
    if issues:
        for issue in issues:
            print(f"[assert-backend-bundle] {issue}", file=sys.stderr)
        return 1

    print(f"[assert-backend-bundle] MLX runtime present and model weights absent: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
