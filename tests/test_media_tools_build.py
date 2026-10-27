import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
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


def test_installs_generated_files_with_spaces_and_required_modes(tmp_path):
    module = load_recipe()
    source = tmp_path / "fresh build"
    source.mkdir()
    (source / "header.h").write_bytes(b"int media_header;\n")
    (source / "command").write_text('#!/bin/sh\nprintf "%s" "$1"\n')
    (source / "command").chmod(0o600)
    install = tmp_path / "staged files"
    install.mkdir()

    module.install_built_files(source, install, [
        ("header.h", "include/header.h", 0o644),
        ("command", "bin/command", 0o755),
    ])

    assert (install / "include/header.h").read_bytes() == b"int media_header;\n"
    assert stat.S_IMODE((install / "include/header.h").stat().st_mode) == 0o644
    assert stat.S_IMODE((install / "bin/command").stat().st_mode) == 0o755
    assert subprocess.check_output([str(install / "bin/command"), "preserved argument"], text=True) == "preserved argument"
    assert stat.S_IMODE((source / "command").stat().st_mode) == 0o600


@pytest.mark.parametrize("fault", ["missing", "directory", "symlink", "fifo"])
def test_install_rejects_unsafe_compiled_input(tmp_path, fault):
    source = tmp_path / "build output"
    source.mkdir()
    file = source / "header.h"
    if fault == "directory":
        file.mkdir()
    elif fault == "symlink":
        original = tmp_path / "original.h"
        original.write_text("preserve original")
        file.symlink_to(original)
    elif fault == "fifo":
        os.mkfifo(file)
    install = tmp_path / "install"
    install.mkdir()

    with pytest.raises(ValueError, match="Not a regular file"):
        load_recipe().install_built_files(source, install, [("header.h", "include/header.h", 0o644)])
    assert not list(install.iterdir())


@pytest.mark.parametrize("fault", ["root_link", "parent_link", "target_link", "escape"])
def test_install_never_writes_outside_its_staging_tree(tmp_path, fault):
    source = tmp_path / "source"
    source.mkdir()
    (source / "header.h").write_text("new header")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "header.h"
    sentinel.write_text("preserve original")
    install = tmp_path / "install"
    relative = "include/header.h"
    if fault == "root_link":
        install.symlink_to(outside, target_is_directory=True)
    else:
        install.mkdir()
        if fault == "parent_link":
            (install / "include").symlink_to(outside, target_is_directory=True)
        elif fault == "target_link":
            (install / "include").mkdir()
            (install / relative).symlink_to(sentinel)
        else:
            relative = "../outside/header.h"

    with pytest.raises(ValueError, match="Unsafe installation"):
        load_recipe().install_built_files(source, install, [("header.h", relative, 0o644)])
    assert sentinel.read_text() == "preserve original"


def test_install_detects_changed_bytes_in_the_copy(tmp_path, monkeypatch):
    module = load_recipe()
    source = tmp_path / "source"
    source.mkdir()
    (source / "header.h").write_bytes(b"complete header")
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(module.shutil, "copyfileobj", lambda origin, destination: destination.write(b"truncated"))

    with pytest.raises(ValueError, match="Installed content mismatch"):
        module.install_built_files(source, install, [("header.h", "include/header.h", 0o644)])
    assert (source / "header.h").read_bytes() == b"complete header"


@pytest.mark.parametrize("nested", [False, True])
def test_ffmpeg_references_resolve_to_owned_directories_from_actual_cwd(tmp_path, nested):
    module = load_recipe()
    work = tmp_path / "physical build with spaces"
    (work / "tmp").mkdir(parents=True)
    (work / "install").mkdir()
    cwd = work / "sources" / "ffmpeg checkout" if nested else work / "ffmpeg checkout"
    cwd.mkdir(parents=True)
    env = {"TMPDIR": str(work / "tmp"), "PKG_CONFIG_SYSROOT_DIR": str(work / "install"),
           "PKG_CONFIG_LIBDIR": str(work / "install/lib/pkgconfig"), "CFLAGS": "-mmacosx-version-min=14.0"}

    actual = module.ffmpeg_environment(work, cwd, env)

    assert not Path(actual["TMPDIR"]).is_absolute()
    assert not Path(actual["PKG_CONFIG_SYSROOT_DIR"]).is_absolute()
    assert env["TMPDIR"] == str(work / "tmp")
    assert actual["PKG_CONFIG_LIBDIR"] == env["PKG_CONFIG_LIBDIR"]
    assert actual["CFLAGS"] == env["CFLAGS"]
    observed = json.loads(subprocess.check_output([sys.executable, "-I", "-B", "-c",
        "import json,os; from pathlib import Path; print(json.dumps([str(Path(os.environ[k]).resolve()) "
        "for k in ('TMPDIR','PKG_CONFIG_SYSROOT_DIR')]))"], cwd=cwd, env=actual, text=True))
    assert observed == [str(work / "tmp"), str(work / "install")]


@pytest.mark.parametrize("version,dependency,accepted", [
    ("2.5.1", "/usr/lib/libSystem.B.dylib", True),
    ("wrong-version", "/usr/lib/libSystem.B.dylib", False),
    ("2.5.1", "/opt/local/lib/libpkgconf.dylib", False),
])
def test_pkgconf_requires_locked_version_and_only_system_dependencies(tmp_path, monkeypatch, version, dependency, accepted):
    module = load_recipe()
    file = tmp_path / "pkgconf"

    def command_output(command, **kwargs):
        if command[0] == "/usr/bin/otool":
            assert command == ["/usr/bin/otool", "-L", str(file)]
            return f"{file}:\n\t{dependency} (compatibility version 1.0.0)\n"
        assert command == [str(file), "--version"]
        return version + "\n"

    monkeypatch.setattr(module.subprocess, "check_output", command_output)
    if accepted:
        assert module.verify_pkgconf(file, "2.5.1", {})["version"] == "2.5.1"
    else:
        with pytest.raises(ValueError, match="Unexpected pkgconf"):
            module.verify_pkgconf(file, "2.5.1", {})


@pytest.fixture(autouse=True)
def archive_cache(tmp_path, monkeypatch):
    cache = (tmp_path / "archive-cache").resolve()
    cache.mkdir()
    monkeypatch.setenv("VENUS_RELEASE_ARCHIVE_CACHE", str(cache))
    return cache


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


def test_regular_archive_and_source_hash_validation(tmp_path, monkeypatch, archive_cache):
    module = load_recipe()
    file = tmp_path / "source.tar"
    with tarfile.open(file, "w") as output:
        member = tarfile.TarInfo("source/README")
        member.size = 2
        output.addfile(member, io.BytesIO(b"ok"))
    identity = module.digest(file)
    cached = archive_cache / (identity["sha256"] + "-" + file.name)
    shutil.copyfile(file, cached)
    lock = {"sources": {"source": {"file": file.name, **identity}}}

    def unexpected_fetch(*args, **kwargs):
        pytest.fail("Cached archives must not invoke download or Git")

    monkeypatch.setattr(module, "download", unexpected_fetch)
    monkeypatch.setattr(module, "run", unexpected_fetch)
    work = tmp_path / "valid"
    work.mkdir()
    sources = module.obtain_sources(lock, work, {})
    assert (sources["source"] / "README").read_text() == "ok"
    assert cached.read_bytes() == file.read_bytes()
    with cached.open("ab") as output:
        output.write(b"bad")
    damaged = cached.read_bytes()
    work = tmp_path / "damaged"
    work.mkdir()
    with pytest.raises(ValueError, match="identity mismatch"):
        module.obtain_sources(lock, work, {})
    assert cached.read_bytes() == damaged
    assert not (work / "archives" / file.name).exists()


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
