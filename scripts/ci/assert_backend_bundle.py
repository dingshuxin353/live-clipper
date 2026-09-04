#!/usr/bin/env python3
"""Check desktop MLX wheel identity and macOS 14 binaries, then frozen resources."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from email.parser import Parser
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = ROOT / "desktop/backend-dist/live-clipper-backend"
REQUIREMENTS = ROOT / "desktop/build/mlx-requirements.txt"

REQUIRED_DIRECTORIES = (
    "mlx",
    "mlx_whisper",
)
REQUIRED_FILES = (
    "mlx/lib/libmlx.dylib",
    "mlx/lib/libjaccl.dylib",
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


def validate_macho(path: Path) -> list[str]:
    """Read the actual load commands; the build SDK is not the deployment target."""
    try:
        arches = subprocess.run(
            ["/usr/bin/lipo", "-archs", str(path)],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if arches != "arm64":
            return [f"MLX binary must be arm64: {path} ({arches})"]
        output = subprocess.run(
            ["/usr/bin/vtool", "-show-build", str(path)],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
        targets = re.findall(r"^\s*platform\s+(\S+)\s*$", output, re.MULTILINE)
        minimums = re.findall(r"^\s*minos\s+(\S+)\s*$", output, re.MULTILINE)
        if targets != ["MACOS"] or len(minimums) != 1:
            return [f"MLX binary has no unique macOS build target: {path}"]
        minimum = minimums[0]
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", minimum):
            return [f"MLX binary has invalid minos {minimum}: {path}"]
        version = tuple(int(part) for part in minimum.split("."))
        if version + (0,) * (3 - len(version)) > (14, 0, 0):
            return [f"MLX binary minos {minimum} exceeds macOS 14.0: {path}"]
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        return [f"cannot inspect MLX Mach-O {path}: {exc}"]
    return []


def validate_runtime(internal: Path) -> list[str]:
    issues: list[str] = []
    for relative in REQUIRED_DIRECTORIES:
        path = internal / relative
        if not path.is_dir():
            issues.append(f"required MLX directory missing: {path}")

    for relative in REQUIRED_FILES:
        path = internal / relative
        if not path.is_file():
            issues.append(f"required MLX runtime asset missing: {path}")

    cores = list((internal / "mlx").glob("core*.so"))
    if len(cores) != 1:
        issues.append(f"exactly one MLX core extension required: {internal / 'mlx'}")
    for path in [*cores, internal / "mlx/lib/libmlx.dylib", internal / "mlx/lib/libjaccl.dylib"]:
        if not path.is_file() or not path.resolve().is_relative_to(internal.resolve()):
            issues.append(f"MLX binary missing or outside runtime: {path}")
        else:
            issues.extend(validate_macho(path))
    return issues


def validate_installed() -> list[str]:
    if (sys.version_info[:2], sys.platform, platform.machine()) != ((3, 11), "darwin", "arm64"):
        return ["desktop MLX build requires Python 3.11 on macOS arm64"]
    issues: list[str] = []
    try:
        for line in REQUIREMENTS.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            if " @ " not in line:
                name, version = line.split("==")
                if metadata.version(name) != version:
                    issues.append(f"installed {name} must be {version}")
                continue
            name, reference = line.split(" @ ")
            url, digest = reference.split("#sha256=")
            _, version, python, abi, target = Path(urlsplit(url).path).stem.split("-")
            distribution = metadata.distribution(name)
            wheel = Parser().parsestr(distribution.read_text("WHEEL") or "")
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
            if (
                distribution.version != version
                or wheel.get_all("Tag") != [f"{python}-{abi}-{target}"]
                or direct.get("url") != url
                or direct.get("archive_info", {}).get("hashes", {}).get("sha256") != digest
            ):
                issues.append(f"installed {name} does not match the fixed macOS 14 wheel")
        issues.extend(validate_runtime(Path(metadata.distribution("mlx").locate_file(""))))
    except (OSError, ValueError, TypeError, AttributeError, metadata.PackageNotFoundError) as exc:
        issues.append(f"cannot verify installed MLX identity: {exc}")
    if not issues:
        for command in (
            [sys.executable, "-m", "pip", "check"],
            [sys.executable, "-c", "import mlx.core as mx; import mlx_whisper; "
             "assert mx.metal.is_available(); mx.set_default_device(mx.gpu); "
             "value = mx.sum(mx.array([1, 2, 3])); mx.eval(value); assert value.item() == 6"],
        ):
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                issues.append(f"MLX runtime check failed: {exc}")
                break
    return issues


def validate_bundle(bundle: Path) -> list[str]:
    """Return human-readable contract violations for a PyInstaller onedir bundle."""
    if not bundle.is_dir():
        return [f"backend bundle not found: {bundle}"]
    internal = bundle / "_internal"
    if not internal.is_dir():
        return [f"PyInstaller _internal directory not found: {internal}"]
    issues = validate_runtime(internal)

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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installed", action="store_true", help="check the build Python environment")
    mode.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE,
        help=f"PyInstaller onedir bundle (default: {DEFAULT_BUNDLE})",
    )
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    issues = validate_installed() if args.installed else validate_bundle(bundle)
    if issues:
        for issue in issues:
            print(f"[assert-backend-bundle] {issue}", file=sys.stderr)
        return 1

    if args.installed:
        print("[assert-backend-bundle] fixed MLX wheels, macOS 14 binaries, pip check and Metal passed")
    else:
        print(f"[assert-backend-bundle] MLX runtime present and model weights absent: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
