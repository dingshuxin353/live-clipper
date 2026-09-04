from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/assert_backend_bundle.py"
spec = importlib.util.spec_from_file_location("bundle_contract", SCRIPT)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def _valid_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "live-clipper-backend"
    internal = bundle / "_internal"
    required_files = (
        "mlx/lib/libmlx.dylib",
        "mlx/lib/libjaccl.dylib",
        "mlx/core.cpython-311-darwin.so",
        "mlx/lib/mlx.metallib",
        "mlx_whisper/assets/mel_filters.npz",
    )
    for relative in required_files:
        path = internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")
    return bundle


@pytest.fixture(autouse=True)
def native_tools(monkeypatch):
    def run(command, **kwargs):
        assert kwargs["check"] and kwargs["timeout"] == 10
        output = "arm64\n" if command[0].endswith("lipo") else (
            "Load command 8\n cmd LC_BUILD_VERSION\n platform MACOS\n minos 14.0\n sdk 26.5\n"
        )
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(contract.subprocess, "run", run)


def test_assert_backend_bundle_accepts_mlx_runtime_without_weights(tmp_path: Path, capsys):
    assert contract.main(["--bundle", str(_valid_bundle(tmp_path))]) == 0
    result = capsys.readouterr()
    assert "MLX runtime present and model weights absent" in result.out
    assert result.err == ""


def test_assert_backend_bundle_rejects_missing_runtime_assets(tmp_path: Path):
    bundle = _valid_bundle(tmp_path)
    (bundle / "_internal/mlx/lib/mlx.metallib").unlink()

    issues = "\n".join(contract.validate_bundle(bundle))
    assert "required MLX runtime asset missing" in issues
    assert "mlx.metallib" in issues


def test_assert_backend_bundle_rejects_packaged_model_weights(tmp_path: Path):
    bundle = _valid_bundle(tmp_path)
    weights = bundle / "_internal/mlx_whisper/weights.npz"
    weights.write_bytes(b"model weights")

    issues = "\n".join(contract.validate_bundle(bundle))
    assert "model weights must not be packaged" in issues
    assert "weights.npz" in issues


@pytest.mark.parametrize("arch,platform,minimum,error", [
    ("arm64", "MACOS", "26.2", "exceeds macOS 14.0"),
    ("arm64", "MACOS", "14.0.1", "exceeds macOS 14.0"),
    ("x86_64", "MACOS", "14.0", "must be arm64"),
    ("arm64 x86_64", "MACOS", "14.0", "must be arm64"),
    ("arm64", "IOS", "14.0", "no unique macOS"),
    ("arm64", "MACOS", "broken", "invalid minos"),
    ("arm64", "MACOS", "", "no unique macOS"),
])
def test_rejects_wrong_macho_contract(monkeypatch, arch, platform, minimum, error):
    monkeypatch.setattr(contract.subprocess, "run", lambda command, **_: SimpleNamespace(
        stdout=arch if command[0].endswith("lipo") else (
            f" platform {platform}\n minos {minimum}\n sdk 26.5\n"
        )
    ))
    assert error in "\n".join(contract.validate_macho(Path("runtime.dylib")))


@pytest.mark.parametrize("failure", [OSError("unreadable"),
    subprocess.CalledProcessError(1, "lipo"), subprocess.TimeoutExpired("vtool", 10)])
def test_native_inspection_failure_is_not_success(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(contract.subprocess, "run", fail)
    assert "cannot inspect" in "\n".join(contract.validate_macho(Path("invalid")))


@pytest.mark.parametrize("relative", ["mlx/core.cpython-311-darwin.so", "mlx/lib/libjaccl.dylib"])
def test_rejects_missing_binary(tmp_path, relative):
    bundle = _valid_bundle(tmp_path)
    (bundle / "_internal" / relative).unlink()
    assert contract.validate_bundle(bundle)


@pytest.mark.parametrize("wrong", ["tag", "version", "hash", "url", "missing", "malformed", None])
def test_installed_identity_rejects_wrong_wheel_at_same_version(tmp_path, monkeypatch, wrong):
    internal = _valid_bundle(tmp_path) / "_internal"
    monkeypatch.setattr(contract.sys, "version_info", (3, 11))
    monkeypatch.setattr(contract.sys, "platform", "darwin")
    monkeypatch.setattr(contract.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(contract.metadata, "version", lambda _: "0.4.3")
    entries = {}
    for line in contract.REQUIREMENTS.read_text().splitlines():
        if " @ " not in line:
            continue
        name, reference = line.split(" @ ")
        url, digest = reference.split("#sha256=")
        tag = "-".join(Path(url).stem.split("-")[-3:])
        entries[name] = {"WHEEL": f"Tag: {tag}\n", "direct_url.json": json.dumps({
            "url": url, "archive_info": {"hashes": {"sha256": digest}},
        })}
    data = entries["mlx"]
    if wrong == "tag":
        data["WHEEL"] = data["WHEEL"].replace("macosx_14_0", "macosx_26_0")
    if wrong in ("hash", "url"):
        direct = json.loads(data["direct_url.json"])
        if wrong == "hash":
            direct["archive_info"]["hashes"]["sha256"] = "0" * 64
        else:
            direct["url"] = "https://example.invalid/wrong.whl"
        data["direct_url.json"] = json.dumps(direct)
    if wrong == "missing":
        data["direct_url.json"] = None
    if wrong == "malformed":
        data["direct_url.json"] = "[]"
    monkeypatch.setattr(contract.metadata, "distribution", lambda name: SimpleNamespace(
        version="0.32.1" if wrong == "version" else "0.32.2",
        read_text=lambda filename: entries[name][filename], locate_file=lambda _: internal,
    ))
    if wrong is None:
        native_run = contract.subprocess.run
        checks = []
        def run(command, **kwargs):
            if command[0] in ("/usr/bin/lipo", "/usr/bin/vtool"):
                return native_run(command, **kwargs)
            checks.append(command)
            assert kwargs["check"] and kwargs["timeout"] == 60
            return SimpleNamespace(stdout="")
        monkeypatch.setattr(contract.subprocess, "run", run)
        assert contract.validate_installed() == []
        assert checks[0][1:] == ["-m", "pip", "check"]
        assert "mx.eval(value)" in checks[1][-1]
        assert "import mlx_whisper" in checks[1][-1]
    else:
        assert contract.validate_installed()


def test_build_stops_at_failed_preflight_before_any_build(tmp_path, monkeypatch):
    # Only the subprocess under test gets a deliberately failing Python entrypoint.
    monkeypatch.undo()
    script = tmp_path / "desktop/scripts/build-backend.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes((ROOT / "desktop/scripts/build-backend.sh").read_bytes())
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "$*" >> calls\nexit 1\n')
    python.chmod(0o755)
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert (tmp_path / "calls").read_text().splitlines() == [
        "scripts/ci/assert_backend_bundle.py --installed"
    ]
    assert "mlx-requirements.txt" in result.stderr
    assert not (tmp_path / "desktop/backend-dist").exists()


def test_build_does_not_report_success_when_frozen_bundle_fails(tmp_path, monkeypatch):
    monkeypatch.undo()
    script = tmp_path / "desktop/scripts/build-backend.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes((ROOT / "desktop/scripts/build-backend.sh").read_bytes())
    binaries = tmp_path / ".venv/bin"
    binaries.mkdir(parents=True)
    for name in ("python", "pyinstaller", "npm", "git"):
        executable = binaries / name
        executable.write_text(
            '#!/bin/sh\necho "$0 $*" >> calls\n'
            'case "$*" in *--bundle*) exit 1;; esac\nexit 0\n'
        )
        executable.chmod(0o755)
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False,
                            env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin"})
    assert result.returncode == 1
    calls = (tmp_path / "calls").read_text().splitlines()
    assert "--installed" in calls[0]
    assert "--bundle desktop/backend-dist/live-clipper-backend" in calls[-1]
    assert any("pyinstaller --noconfirm" in call for call in calls)
    assert "[build-backend] done" not in result.stdout
