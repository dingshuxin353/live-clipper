import importlib.util
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[1] / "desktop/scripts/build-media-tools.py"


def load_recipe():
    spec = importlib.util.spec_from_file_location("media_build", RECIPE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_recipe_and_locked_inputs_replace_prebuilt_supply():
    module = load_recipe()
    desktop = RECIPE.parent.parent
    lock = module.read_lock(desktop)
    assert set(lock["sources"]) == {"ffmpeg", "x264", "dav1d", "pkgconf"}
    assert lock["sources"]["x264"]["commit"] == "b35605ace3ddf7c1a5d67a2eb553f034aef41d55"
    hook = (desktop / "scripts/prepare-media-tools.js").read_text()
    assert "martin-riedl" not in hook
    assert "build-media-tools.py" in hook


@pytest.mark.parametrize("kind,name,link", [
    (tarfile.REGTYPE, "../escape", ""),
    (tarfile.REGTYPE, "/absolute", ""),
    (tarfile.SYMTYPE, "source/link", "../../escape"),
    (tarfile.LNKTYPE, "source/link", "../escape"),
    (tarfile.FIFOTYPE, "source/pipe", ""),
])
def test_archive_rejects_paths_links_and_special_files(tmp_path, kind, name, link):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, link
        output.addfile(member)
    with pytest.raises(ValueError, match="Unsafe"):
        load_recipe().unpack(archive, tmp_path / "extract")
    assert not (tmp_path / "escape").exists()


def test_regular_archive_and_source_hash_validation(tmp_path):
    module = load_recipe()
    file = tmp_path / "source.tar"
    with tarfile.open(file, "w") as output:
        member = tarfile.TarInfo("source/README")
        member.size = 2
        output.addfile(member, io.BytesIO(b"ok"))
    identity = module.digest(file)
    module.verify_source(file, identity)
    assert (module.unpack(file, tmp_path / "out") / "README").read_text() == "ok"
    with file.open("ab") as output:
        output.write(b"bad")
    with pytest.raises(ValueError, match="identity mismatch"):
        module.verify_source(file, identity)


@pytest.mark.parametrize("body,status", [(b"short", 200), (b"toolong", 200), (b"", 302), (b"", 500)])
def test_download_errors_and_truncation_are_not_accepted(tmp_path, monkeypatch, body, status):
    module = load_recipe()

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            self.sock = self

        def settimeout(self, timeout):
            assert 0 < timeout <= 120

        def connect(self):
            pass

        def request(self, method, path):
            assert method == "GET" and path == "/fixed.tar"

        def getresponse(self):
            response = io.BytesIO(body)
            response.status = status
            return response

        def close(self):
            pass

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    entry = {"url": "https://example.invalid/fixed.tar", "size": 6, "sha256": "0" * 64}
    with pytest.raises(ValueError):
        module.download(entry, tmp_path / "input")


def test_git_requires_exact_commit_and_disables_interactive_configuration(tmp_path, monkeypatch):
    module = load_recipe()
    lock = {"sources": {"x264": module.read_lock(RECIPE.parent.parent)["sources"]["x264"]}}
    calls = []
    monkeypatch.setattr(module, "run", lambda command, *a, **kw: calls.append(command))
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "wrong-head\n")
    with pytest.raises(ValueError, match="commit mismatch"):
        module.obtain_sources(lock, tmp_path, {})
    assert calls[0][-2] == "--template="
    assert all("core.hooksPath=/dev/null" in call for call in calls)
    assert lock["sources"]["x264"]["commit"] in calls[1]


def test_wrong_host_and_private_paths_are_rejected(monkeypatch):
    module = load_recipe()
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    with pytest.raises(ValueError, match="macOS arm64"):
        module.identity(RECIPE.parent.parent)
    for data in (b"/Users/example/work/file", b"/home/person/project", b"/Volumes/private/work"):
        with pytest.raises(ValueError, match="Private build path"):
            module.public_bytes(data)
    module.public_bytes(RECIPE.read_bytes())


def test_missing_material_and_wrong_toolchain_are_rejected(tmp_path, monkeypatch):
    module = load_recipe()
    desktop = RECIPE.parent.parent
    shutil.copytree(desktop / "build/ffmpeg", tmp_path / "build/ffmpeg")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(RECIPE, tmp_path / "scripts/build-media-tools.py")
    shutil.copyfile(desktop / "package.json", tmp_path / "package.json")
    lock = module.read_lock(desktop)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.subprocess, "check_output", lambda args, **kw:
                        lock["compiler"] if args[0].endswith("clang") else lock["sdk"])
    assert module.identity(tmp_path)["target"] == "darwin-arm64"
    (tmp_path / "build/ffmpeg/COMPONENTS.md").unlink()
    with pytest.raises(FileNotFoundError):
        module.identity(tmp_path)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kw: "changed toolchain")
    with pytest.raises(ValueError, match="toolchain"):
        module.identity(tmp_path)


def test_network_timeout_leaves_no_accepted_source(tmp_path, monkeypatch):
    module = load_recipe()

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise TimeoutError("network unavailable")

        def close(self):
            pass

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    with pytest.raises(TimeoutError):
        module.download({"url": "https://example.invalid/source.tar"}, tmp_path / "source")
    assert not (tmp_path / "source").exists()


def test_subprocess_failure_timeout_and_build_failure_preserve_evidence(tmp_path, monkeypatch):
    module = load_recipe()
    with pytest.raises(RuntimeError, match="Build failed"):
        module.run([sys.executable, "-c", "raise SystemExit(4)"], tmp_path, {}, tmp_path / "failure.log")
    with pytest.raises(TimeoutError):
        module.run([sys.executable, "-c", "while True: pass"], tmp_path, {}, tmp_path / "timeout.log", .05)
    monkeypatch.setattr(module, "identity", lambda _: {"version": "1.0.1"})
    monkeypatch.setattr(module, "read_lock", lambda _: {})

    def failed_source(*args):
        raise ValueError("source failure")

    monkeypatch.setattr(module, "obtain_sources", failed_source)
    with pytest.raises(ValueError, match="source failure"):
        module.build(tmp_path)
    parent = tmp_path / "vendor/media-tools"
    assert not (parent / "darwin-arm64").exists()
    assert len(list(parent.glob(".build-*"))) == 1


def test_source_package_carries_its_own_recipe_materials_and_version(tmp_path):
    module = load_recipe()
    desktop = RECIPE.parent.parent
    (tmp_path / "archives").mkdir()
    (tmp_path / "archives/source.tar").write_bytes(b"archive fixture")
    output = tmp_path / "bundle.tar.gz"
    module.source_bundle(desktop, tmp_path, output, "1.0.2")
    with tarfile.open(output) as package:
        names = package.getnames()
        assert "desktop/scripts/build-media-tools.py" in names
        assert "desktop/build/ffmpeg/sources.lock.json" in names
        assert "archives/source.tar" in names
        assert json.load(package.extractfile("desktop/package.json")) == {"version": "1.0.2"}
        assert not any("vendor" in name or "build-manifest" in name for name in names)
