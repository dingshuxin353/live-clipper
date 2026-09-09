"""Build the macOS arm64 media pair from locked, unmodified source archives."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

MATERIALS = ("SOURCE.md", "COMPONENTS.md", "GPL-2.0.txt")
INPUTS = ("scripts/build-media-tools.py", "build/ffmpeg/sources.lock.json",
          *(f"build/ffmpeg/{name}" for name in MATERIALS))
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def digest(file):
    if file.is_symlink() or not file.is_file():
        raise ValueError(f"Not a regular file: {file.name}")
    with file.open("rb") as source:
        return {"size": file.stat().st_size,
                "sha256": hashlib.file_digest(source, "sha256").hexdigest()}


def read_lock(desktop):
    return json.loads((desktop / "build/ffmpeg/sources.lock.json").read_text())


def public_bytes(data):
    if re.search(rb"/(?:Users|home|Volumes)/[A-Za-z0-9][^\s\"']+", data):
        raise ValueError("Private build path in public material")


def identity(desktop):
    lock = read_lock(desktop)
    if sys.version_info[:2] != (3, 11) or platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Requires Python 3.11 on macOS arm64")
    if lock["target"] != "darwin-arm64" or lock["minimum_macos"] != "14.0":
        raise ValueError("Unsupported media target")
    env = {"PATH": SYSTEM_PATH}
    compiler = subprocess.check_output(["/usr/bin/clang", "--version"], env=env, text=True, timeout=10).splitlines()[0]
    sdk = subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-version"], env=env, text=True, timeout=10).strip()
    if compiler != lock["compiler"] or sdk != lock["sdk"]:
        raise ValueError("Unvalidated Apple toolchain; revalidation required")
    version = json.loads((desktop / "package.json").read_text())["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid application version")
    inputs = {}
    for name in INPUTS:
        file = desktop / name
        public_bytes(file.read_bytes())
        inputs[name] = digest(file)
    return {"version": version, "target": lock["target"], "minimum_macos": lock["minimum_macos"],
            "compiler": compiler, "sdk": sdk, "inputs": inputs}


def verify_source(file, entry):
    if digest(file) != {key: entry[key] for key in ("size", "sha256")}:
        raise ValueError(f"Source identity mismatch: {file.name}")


def download(entry, destination):
    url = urlsplit(entry["url"])
    if url.scheme != "https" or url.username or url.password:
        raise ValueError("Source requires HTTPS")
    deadline = time.monotonic() + 120
    connection = http.client.HTTPSConnection(url.hostname, timeout=120)
    try:
        connection.connect()
        connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
        connection.request("GET", url.path)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Source download requires HTTP 200; no redirects or retries")
        size = 0
        with destination.open("xb") as output:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Source download deadline exceeded")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > entry["size"]:
                    raise ValueError("Source download exceeds size limit")
                output.write(chunk)
        verify_source(destination, entry)
    finally:
        connection.close()


def unpack(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 50000 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("Source archive exceeds extraction limits")
        # Reject links entirely: none is needed by these four source distributions.
        for member in members:
            target = destination / member.name
            if (not target.resolve().is_relative_to(destination.resolve())
                    or Path(member.name).is_absolute()
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe source archive member")
        source.extractall(destination, members=members, filter="data")
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError("Expected a single source directory")
    return children[0]


def run(command, cwd, env, log, timeout=900):
    print(f"Building: {log.name}", flush=True)
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if log.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("Build log exceeds size limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Build step timed out")
                try:
                    code = process.wait(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if log.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Build log exceeds size limit")
            if code:
                raise RuntimeError(f"Build failed ({code}): {log}")
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise


def obtain_sources(lock, work, env, archives=None):
    downloads = work / "archives"
    downloads.mkdir()
    sources = {}
    for name, entry in lock["sources"].items():
        file = downloads / entry["file"]
        if archives is not None:
            original = archives / entry["file"]
            verify_source(original, entry)
            shutil.copyfile(original, file)
        elif "git" in entry:
            repo = work / "x264-git"
            commands = [
                ["init", "--template=", str(repo)],
                ["-C", str(repo), "fetch", "--depth=1", entry["git"], entry["commit"]],
                ["-C", str(repo), "checkout", "--detach", "FETCH_HEAD"],
            ]
            for index, args in enumerate(commands):
                run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args], work, env,
                    work / "logs" / f"git-{index}.log", timeout=120)
            actual = subprocess.check_output(["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
                                             env=env, text=True, timeout=10).strip()
            if actual != entry["commit"]:
                raise ValueError("x264 commit mismatch")
            run(["/usr/bin/git", "-C", str(repo), "archive", "--format=tar", "--prefix=x264/",
                 f"--output={file}", actual], work, env, work / "logs/git-archive.log", timeout=30)
        else:
            download(entry, file)
        verify_source(file, entry)
        sources[name] = unpack(file, work / f"source-{name}")
    return sources


def source_bundle(desktop, work, output, version):
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        files = [(desktop / name, f"desktop/{name}") for name in INPUTS]
        files += [(file, f"archives/{file.name}") for file in sorted((work / "archives").iterdir())]
        for file, name in files:
            info = tarfile.TarInfo(name)
            data = file.read_bytes()
            info.size, info.mode = len(data), 0o644
            bundle.addfile(info, io.BytesIO(data))
        data = (json.dumps({"version": version}) + "\n").encode()
        info = tarfile.TarInfo("desktop/package.json")
        info.size, info.mode = len(data), 0o644
        bundle.addfile(info, io.BytesIO(data))


def build(desktop, archives=None):
    expected = identity(desktop)
    lock = read_lock(desktop)
    parent = desktop
    for part in ("vendor", "media-tools"):
        parent /= part
        parent.mkdir(exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("Unsafe media build directory")
    destination = parent / "darwin-arm64"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Cache already exists; refusing to overwrite")
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=parent))
    print(f"Build evidence: {work}", flush=True)
    for part in ("logs", "home", "tmp", "install", "result"):
        (work / part).mkdir()
    env = {"PATH": SYSTEM_PATH, "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"),
           "LANG": "C", "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false",
           "CC": "clang", "CXX": "clang++", "MACOSX_DEPLOYMENT_TARGET": "14.0",
           "CFLAGS": "-mmacosx-version-min=14.0", "LDFLAGS": "-mmacosx-version-min=14.0"}
    sources = obtain_sources(lock, work, env, archives)
    tools = work / "tools"
    run([sys.executable, "-m", "venv", str(tools)], work, env, work / "logs/venv.log", 120)
    run([str(tools / "bin/python"), "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
         "--no-cache-dir", "--retries=0", "--timeout=30", "--index-url=https://pypi.org/simple",
         "--dest", "wheels", *(f"{n}=={v}" for n, v in lock["build_tools"].items())],
        work, env, work / "logs/tools-download.log", 120)
    wheels = sorted((work / "wheels").iterdir())
    run([str(tools / "bin/python"), "-m", "pip", "install", "--no-index", "--no-deps",
         *(str(file) for file in wheels)], work, env, work / "logs/tools-install.log", 120)
    env.update(PATH=f"{work}/install/venus-tools/bin:{tools}/bin:{SYSTEM_PATH}",
               PKG_CONFIG_LIBDIR=str(work / "install/venus-media/lib/pkgconfig"),
               PKG_CONFIG_SYSROOT_DIR=str(work / "install"))
    tool_versions = {name: subprocess.check_output([str(tools / "bin" / name), "--version"],
                                                  env=env, text=True, timeout=10).strip()
                     for name in lock["build_tools"]}
    package_versions = json.loads(subprocess.check_output(
        [str(tools / "bin/python"), "-I", "-c",
         "import importlib.metadata as m, json; print(json.dumps({n: m.version(n) for n in ('meson', 'ninja')}))"],
        env=env, text=True, timeout=10))
    if package_versions != lock["build_tools"]:
        raise ValueError("Unexpected installed build tool version")
    prefix = "/venus-media"
    plans = {
        "pkgconf": (sources["pkgconf"], [
            ["./configure", "--prefix=/venus-tools", "--disable-shared", "--enable-static",
             "--with-pkg-config-dir=/venus-media/lib/pkgconfig"],
            ["make", "-j6"], ["make", "install", f"DESTDIR={work}/install"]]),
        "x264": (sources["x264"], [
            ["./configure", f"--prefix={prefix}", "--enable-static", "--enable-pic", "--disable-cli",
             "--disable-opencl", "--extra-cflags=-mmacosx-version-min=14.0",
             "--extra-ldflags=-mmacosx-version-min=14.0"],
            ["make", "-j6"], ["make", "install", f"DESTDIR={work}/install"]]),
        "dav1d": (work, [
            ["meson", "setup", "build-dav1d", str(sources["dav1d"]), f"--prefix={prefix}",
             "--libdir=lib", "--default-library=static", "--buildtype=release", "--wrap-mode=nodownload",
             "-Denable_tools=false", "-Denable_tests=false"],
            ["meson", "compile", "-C", "build-dav1d", "-j6"],
            ["meson", "install", "-C", "build-dav1d", "--destdir", str(work / "install")]]),
        "ffmpeg": (sources["ffmpeg"], [
            ["./configure", f"--prefix={prefix}", "--arch=aarch64", "--target-os=darwin", "--cc=clang",
             "--cxx=clang++", "--pkg-config=pkgconf", "--pkg-config-flags=--static",
             "--disable-autodetect", "--enable-gpl", "--enable-libx264", "--enable-libdav1d",
             "--enable-static", "--disable-shared", "--disable-doc", "--disable-debug",
             "--disable-ffplay", "--disable-avdevice", "--disable-network", "--enable-pthreads",
             "--enable-zlib", "--enable-iconv", "--extra-libs=-liconv",
             "--extra-cflags=-mmacosx-version-min=14.0", "--extra-ldflags=-mmacosx-version-min=14.0"],
            ["make", "-j6", "ffmpeg", "ffprobe"], ["make", "install-progs", f"DESTDIR={work}/install"]]),
    }
    for component, (cwd, commands) in plans.items():
        for index, command in enumerate(commands):
            run(command, cwd, env, work / "logs" / f"{component}-{index}.log")
    result = work / "result"
    reports = {}
    for name in ("ffmpeg", "ffprobe"):
        file = result / name
        shutil.copy2(work / "install/venus-media/bin" / name, file)
        public_bytes(file.read_bytes())
        version = subprocess.check_output([str(file), "-version"], env=env, text=True, timeout=5)
        configuration = subprocess.check_output([str(file), "-buildconf"], env=env, text=True,
                                                stderr=subprocess.STDOUT, timeout=5)
        license_text = subprocess.check_output([str(file), "-L"], env=env, text=True,
                                               stderr=subprocess.STDOUT, timeout=5)
        if (not version.startswith(f"{name} version 9.0.1 ") or "version 2" not in license_text
                or "nonfree" in configuration or "--enable-version3" in configuration):
            raise ValueError("Unexpected media version/license")
        public_bytes((version + configuration + license_text).encode())
        arch = subprocess.check_output(["/usr/bin/lipo", "-archs", str(file)], text=True, timeout=10).strip()
        dylibs = subprocess.check_output(["/usr/bin/otool", "-L", str(file)], text=True, timeout=10).splitlines()[1:]
        minos = subprocess.check_output(["/usr/bin/xcrun", "vtool", "-show-build", str(file)], text=True, timeout=10)
        if (arch != "arm64" or not re.search(r"minos\s+14\.0", minos)
                or not all(line.strip().startswith(("/usr/lib/", "/System/Library/")) for line in dylibs)):
            raise ValueError("Unexpected binary architecture, deployment target or dependency")
        reports[name] = {**digest(file), "version": version, "configuration": configuration,
                         "license": license_text, "dylibs": dylibs, "minimum_macos": "14.0"}
    source_bundle(desktop, work, result / "media-sources.tar.gz", expected["version"])
    licenses = result / "licenses"
    licenses.mkdir()
    for name in MATERIALS:
        shutil.copyfile(desktop / "build/ffmpeg" / name, licenses / name)
    source_name = f"Venus-{expected['version']}-media-sources.tar.gz"
    source_hash = digest(result / "media-sources.tar.gz")["sha256"]
    (licenses / "CORRESPONDING-SOURCE.md").write_text(
        f"# Corresponding source\n\nArchive: {source_name}\n\nSHA-256: {source_hash}\n\n"
        f"https://github.com/dingshuxin353/live-clipper/releases/download/v{expected['version']}/{source_name}\n\n"
        "Includes the unmodified source archives, build recipe, input lock and license notices.\n"
        "Rebuild instructions: desktop/build/ffmpeg/SOURCE.md in the archive.\n")
    outputs = {str(file.relative_to(result)): digest(file) for file in result.rglob("*") if file.is_file()}
    manifest = {"identity": expected, "outputs": outputs, "tools": reports,
                "build_tools": {"python": platform.python_version(),
                                "wheels": {file.name: digest(file) for file in wheels},
                                "versions": package_versions, "command_versions": tool_versions}}
    (result / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if identity(desktop) != expected:
        raise ValueError("Build inputs changed during compilation")
    result.rename(destination)
    print(f"Built media tools: {destination}", flush=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, help="Directory of locked source archives for offline source preparation")
    parser.add_argument("--identity", action="store_true")
    args = parser.parse_args()
    desktop_root = Path(__file__).resolve().parent.parent
    if args.identity:
        print(json.dumps(identity(desktop_root)))
    else:
        build(desktop_root, args.archives)
