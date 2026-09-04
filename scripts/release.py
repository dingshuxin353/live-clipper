#!/usr/bin/env python3.11
"""Build one isolated candidate; publish only that candidate after human acceptance."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUMAN_CHECKS = ("fresh_first_frame", "previous_data_upgrade", "release_risk_paths")


class ReleaseError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha512(path):
    with Path(path).open("rb") as stream:
        return base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    require(not path.is_symlink(), "Refusing a symlink state file")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def version_tuple(value):
    require(isinstance(value, str) and re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value), "Expected stable X.Y.Z version")
    return tuple(map(int, value.split(".")))


def inventory(root):
    require(not Path(root).is_symlink(), "Inventory root must not be a symlink")
    root = Path(root).resolve(strict=True)
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        require(path.resolve().is_relative_to(root), f"Escaping symlink: {name}")
        if path.is_symlink():
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"size": path.stat().st_size, "sha256": sha256(path), "mode": stat.S_IMODE(path.stat().st_mode)}
        elif path.is_dir():
            result[name] = {"directory": True}
        else:
            raise ReleaseError(f"Unsupported file: {name}")
    return result


def verify_inventory(root, expected):
    require(inventory(root) == expected, f"Frozen content changed: {root}")


def canonical_path(path):
    path = Path(path)
    require(path.is_absolute() and path == Path(os.path.abspath(path)), "Use a canonical absolute path")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink paths are not allowed")
    return path


def validate_output(output, repo):
    output = canonical_path(output)
    repo = Path(repo).resolve()
    home = Path.home().resolve()
    require(output not in (Path("/"), home, repo.parent) and not repo.is_relative_to(output), "Release root is too broad")
    require(not output.is_relative_to(repo), "Release root must be outside the source repository")
    protected = [home / "Library", home / ".config", home / ".cache", repo.parent / "input"]
    if os.environ.get("LIVE_CLIPPER_HOME"):
        protected.append(Path(os.environ["LIVE_CLIPPER_HOME"]).expanduser().resolve())
    require(not any(output.is_relative_to(p) or p.is_relative_to(output) for p in protected), "Release root overlaps user data")
    require(output.parent.is_dir(), "Create the release parent directory explicitly first")
    return output


def create_root(output, repo, source):
    output = validate_output(output, repo)
    require(not output.exists(), "Unknown or unfinished directory; preserve it and inspect instead of overwriting")
    output.mkdir(mode=0o700)
    owner = {"format": 1, "repo": str(Path(repo).resolve()), "source": source, "id": str(uuid.uuid4())}
    atomic_json(output / "owner.json", owner)
    (output / "evidence").mkdir()
    return owner


@contextlib.contextmanager
def exclusive_lock(common_git_dir):
    lock = Path(common_git_dir) / "venus-release.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("Another release operation holds the repository lock") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run(args, *, cwd=ROOT, env=None, log=None, input_text=None, timeout=3600):
    """No shell, no command/environment echo; credentials never belong in arguments."""
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, input=input_text,
                            text=True, capture_output=True, timeout=timeout)
    if log:
        # Tool output can contain authenticated URLs. Redact known environment secrets too.
        text = result.stdout + result.stderr
        for key, value in (env or os.environ).items():
            if value and re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key, re.I):
                text = text.replace(value, "[REDACTED]")
        text = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", text)
        Path(log).write_text(text)
    require(result.returncode == 0, f"{Path(str(args[0])).name} failed (exit {result.returncode}); inspect private evidence")
    return result.stdout or result.stderr


def git(repo, *args):
    return run(["git", "-C", repo, *args], timeout=120).strip()


def metadata(repo):
    version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    version_tuple(version)
    versions = [version]
    for folder in ("frontend", "desktop"):
        versions.append(read_json(repo / folder / "package.json")["version"])
        lock = read_json(repo / folder / "package-lock.json")
        versions.extend([lock["version"], lock["packages"][""]["version"]])
    require(set(versions) == {version}, "Seven root versions must agree")
    changelog = (repo / "CHANGELOG.md").read_text()
    section = re.search(rf"^## {re.escape(version)}(?: - [^\n]+)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    require(section and section[1].strip(), "Release CHANGELOG entry missing")
    config = (repo / "desktop/electron-builder.yml").read_text()
    update = dict(re.findall(r"^(\w+):\s*(\S+)\s*$", (repo / "desktop/build/app-update.yml").read_text(), re.M))
    require(update.get("provider") == "github", "Only the configured GitHub provider is supported")
    for key in ("owner", "repo"):
        require(re.fullmatch(r"[\w.-]+", update[key]), "Invalid configured repository")
        require(re.search(rf"^  {key}: {re.escape(update[key])}$", config, re.M), "Publish/provider mismatch")
    bundle = re.search(r"^appId: ([\w.-]+)$", config, re.M)
    require(bundle, "Bundle ID missing")
    return {"version": version, "github": f"{update['owner']}/{update['repo']}", "bundle_id": bundle[1], "notes": section[1].strip()}


def check_source(repo, source, github):
    require(re.fullmatch(r"[0-9a-f]{40}", source), "Use an exact source SHA")
    require(git(repo, "rev-parse", "HEAD") == source == git(repo, "rev-parse", "master"), "HEAD/master/source differ")
    require(not git(repo, "status", "--porcelain", "--untracked-files=all"), "Source worktree must be clean")
    origin = git(repo, "remote", "get-url", "origin")
    require(origin in (f"https://github.com/{github}.git", f"https://github.com/{github}", f"git@github.com:{github}.git"), "Origin differs from configured GitHub repository")
    remote = git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/master")
    require(remote.split()[0] == source, "Remote master moved")


def github_release(github, tag):
    # Listing distinguishes a missing tag from a failed request without parsing error prose.
    releases = json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{github}/releases?per_page=100"]))
    matches = [r for page in releases for r in page if r["tag_name"] == tag]
    require(len(matches) <= 1, "Ambiguous release identity")
    return matches[0] if matches else None


def tools_snapshot():
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "macOS arm64 required")
    commands = {"python": ["python3.11", "--version"], "node": ["node", "--version"], "npm": ["npm", "--version"],
                "git": ["git", "--version"], "gh": ["gh", "--version"], "xcode": ["xcodebuild", "-version"]}
    values = {key: run(cmd, timeout=60).strip() for key, cmd in commands.items()}
    require(values["python"].startswith("Python 3.11.") and values["node"].startswith("v24.") and values["npm"].startswith("11."), "Python 3.11 / Node 24 / npm 11 required")
    for executable in ("codesign", "security", "xcrun", "ditto", "hdiutil", "spctl", "lipo", "lsof"):
        require(shutil.which(executable), f"Missing {executable}")
    run(["gh", "auth", "status"], timeout=60)
    values["os"] = platform.mac_ver()[0]
    return values


def signature_facts(details, bundle_id):
    def field(name):
        match = re.search(rf"^{name}=(.+)$", details, re.M)
        require(match, f"Missing codesign {name}")
        return match[1]

    flags = re.search(r"\bflags=0x([0-9a-fA-F]+)", details)
    require(flags and int(flags[1], 16) & 0x10000, "Hardened Runtime is absent")
    require(field("Identifier") == bundle_id, "Signed Bundle ID mismatch")
    require("Authority=Developer ID Application:" in details and field("Timestamp"), "Developer ID / trusted timestamp absent")
    cdhash = field("CDHash")
    require(re.fullmatch(r"[0-9a-f]{40}", cdhash), "Invalid CDHash")
    return {"hardened_runtime": True, "cdhash": cdhash, "timestamp": field("Timestamp")}


def acceptance_template(manifest, source):
    return {"candidate_sha256": sha256(manifest), "source": source, "reviewer": "", "reviewed_at": "",
            "checks": {name: {"status": "pending", "evidence": []} for name in HUMAN_CHECKS}}


def verify_acceptance(path, manifest, source):
    path = canonical_path(path)
    data = read_json(path)
    require(data.get("candidate_sha256") == sha256(manifest) and data.get("source") == source, "Acceptance belongs to another candidate")
    require(data.get("reviewer", "").strip() and data.get("reviewed_at"), "Human reviewer/time required")
    require(datetime.fromisoformat(data["reviewed_at"].replace("Z", "+00:00")).tzinfo, "Acceptance time needs timezone")
    require(set(data.get("checks", {})) == set(HUMAN_CHECKS), "Human checklist incomplete")
    for name, item in data["checks"].items():
        require(item.get("status") in ("passed", "waived"), f"Human check unfinished: {name}")
        require(item.get("evidence"), f"Missing evidence: {name}")
        if item["status"] == "waived":
            require(item.get("user_decision", "").strip(), "Waiver requires this release's explicit user decision")
        for evidence in item["evidence"]:
            target = canonical_path(path.parent / evidence["path"])
            require(target.is_file() and sha256(target) == evidence["sha256"], "Human evidence missing or changed")
    return data


def notarize(asset, profile, progress, state_path, command=run):
    records = progress.setdefault("notary", {})
    name = asset.name
    record = records.get(name)
    digest = sha256(asset)
    if record:
        require(record["sha256"] == digest, "Notary asset changed")
        require(record.get("id"), "Submission outcome uncertain; recover its ID manually, do not resubmit")
    else:
        record = records[name] = {"state": "submitting", "sha256": digest}
        atomic_json(state_path, progress)
        response = json.loads(command(["xcrun", "notarytool", "submit", asset, "--keychain-profile", profile, "--output-format", "json"]))
        require(response.get("id"), "Apple submission returned no ID; stop and reconcile")
        record.update(id=response["id"], state="submitted", submission=response)
        atomic_json(state_path, progress)
    if record.get("state") != "accepted":
        response = json.loads(command(["xcrun", "notarytool", "wait", record["id"], "--keychain-profile", profile, "--output-format", "json"], timeout=14400))
        record["response"] = response
        atomic_json(state_path, progress)
        require(response.get("id") == record["id"] and response.get("status") == "Accepted", "Apple did not accept; preserve submission and evidence")
        record["state"] = "accepted"
        atomic_json(state_path, progress)


def isolated_env(root):
    # No ambient application config, model/AI secrets, proxy auth, or Python import paths.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    for name in ("home", "cache", "config", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(root / "home"), XDG_CACHE_HOME=str(root / "cache"),
               XDG_CONFIG_HOME=str(root / "config"), TMPDIR=str(root / "tmp"),
               LIVE_CLIPPER_HOME=str(root / "app-home"), PYTHONNOUSERSITE="1")
    return env


def extract_zip(archive, target, command=run):
    require(not target.exists(), "Extraction target already exists")
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        names = [Path(entry.filename) for entry in members]
        require(len(set(names)) == len(names), "Duplicate archive member")
        links = {Path(entry.filename) for entry in members if stat.S_ISLNK(entry.external_attr >> 16)}
        for entry in members:
            name = Path(entry.filename)
            require(not name.is_absolute() and ".." not in name.parts, "Unsafe archive member")
            require(not links.intersection(name.parents), "Archive writes through a symlink")
            if stat.S_ISLNK(entry.external_attr >> 16):
                link = Path(zipped.read(entry).decode())
                resolved = (target / name.parent / link).resolve()
                require(not link.is_absolute() and resolved.is_relative_to(target), "Unsafe archive symlink")
    command(["ditto", "-x", "-k", archive, target])
    inventory(target)


def app_facts(app, info, evidence, label, command=run):
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    require(plist["CFBundleIdentifier"] == info["bundle_id"] and
            plist["CFBundleShortVersionString"] == info["version"] and
            plist["CFBundleVersion"] == info["version"], "App version / Bundle ID mismatch")
    command(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app], log=evidence / f"{label}-signature.log")
    details = command(["codesign", "-d", "--verbose=4", app], log=evidence / f"{label}-codesign.log")
    facts = signature_facts(details, info["bundle_id"])
    resources = app / "Contents/Resources"
    binaries = [app / "Contents/MacOS/Venus", resources / "backend/live-clipper-backend", resources / "bin/ffmpeg"]
    for index, binary in enumerate(binaries):
        arch = command(["lipo", "-archs", binary], log=evidence / f"{label}-arch-{index}.log").strip()
        require(arch == "arm64", "Expected arm64-only executables")
    return facts


def check_packaged_privacy(app):
    resources = app / "Contents/Resources"
    forbidden_names = {".env", "live-clipper.toml", "projects.sqlite3", "service.json", "scheduler.json", "events.jsonl"}
    for path in resources.rglob("*"):
        if path.is_file():
            require(path.name not in forbidden_names and path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".wav", ".p12"}, f"Private/user asset in app: {path.name}")
            if path.suffix.lower() in {".pem", ".key"}:
                require(b"PRIVATE KEY-----" not in path.read_bytes(), "Private key in packaged resources")


@contextlib.contextmanager
def mounted_dmg(dmg, mount, evidence):
    require(not mount.exists(), "Mount path already exists")
    mount.mkdir()
    try:
        run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, dmg], log=evidence / f"{mount.name}-attach.log")
        yield mount
    finally:
        # Even attach can fail after mounting. Never unmount by volume name or all devices.
        if os.path.ismount(mount):
            run(["hdiutil", "detach", mount], log=evidence / f"{mount.name}-detach.log")


def check_packages(root, directory, info, source, label):
    evidence = root / "evidence"
    app = directory / "mac-arm64/Venus.app"
    facts = app_facts(app, info, evidence, label)
    check_packaged_privacy(app)
    require((app / "Contents/Resources/app-update.yml").read_bytes() == (source / "desktop/build/app-update.yml").read_bytes(), "Packaged updater configuration differs")
    run([source / ".venv/bin/python", source / "scripts/ci/assert_backend_bundle.py", "--bundle", app / "Contents/Resources/backend"], log=evidence / f"{label}-backend-bundle.log")
    files = release_assets(directory, info["version"])
    check_dir = root / f"{label}-unpacked"
    extract_zip(directory / files["zip"], check_dir)
    require(inventory(check_dir / "Venus.app") == inventory(app), "ZIP/App contents differ")
    with mounted_dmg(directory / files["dmg"], root / f"{label}-mount", evidence) as mount:
        # Finder metadata outside Venus.app is deliberately not part of app identity.
        require(inventory(mount / "Venus.app") == inventory(app), "DMG/App contents differ")
    return facts


def release_assets(directory, version):
    expected = {"dmg": f"Venus-{version}-arm64.dmg", "zip": f"Venus-{version}-arm64-mac.zip", "blockmap": f"Venus-{version}-arm64-mac.zip.blockmap", "latest": "latest-mac.yml"}
    require(all((directory / name).is_file() and not (directory / name).is_symlink() for name in expected.values()), "Four release assets required")
    return expected


def asset_records(directory, version):
    return {name: {"size": (directory / name).stat().st_size, "sha256": sha256(directory / name), "sha512": sha512(directory / name)} for name in release_assets(directory, version).values()}


def update_metadata(directory, version, source, evidence):
    archive = directory / f"Venus-{version}-arm64-mac.zip"
    # app-builder is already pinned by the desktop lockfile.
    run(["node", source / "scripts/release-smoke.cjs", "blockmap", archive, source], log=evidence / f"{directory.name}-blockmap.log")
    digest = sha512(archive)
    (directory / "latest-mac.yml").write_text(
        f"version: {version}\nfiles:\n  - url: {archive.name}\n    sha512: {digest}\n    size: {archive.stat().st_size}\npath: {archive.name}\nsha512: {digest}\nreleaseDate: '{datetime.now(UTC).isoformat()}'\n")


def download(url, destination):
    require(url.startswith("https://"), "Public assets require HTTPS")
    require(not destination.exists(), "Download target already exists")
    # No gh auth headers or ambient proxies: verify what an anonymous user downloads.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=120) as response, destination.open("xb") as stream:
        require(response.status == 200, "Asset request did not return 200")
        shutil.copyfileobj(response, stream, 1024 * 1024)


def previous_app(root, github, previous_tag, info):
    release = github_release(github, previous_tag)
    require(release and not release["draft"] and not release["prerelease"], "Previous stable release not found")
    files = [a for a in release["assets"] if a["name"].endswith("-arm64-mac.zip")]
    require(len(files) == 1, "Previous ZIP is ambiguous")
    previous = root / "previous"
    previous.mkdir()
    archive = previous / files[0]["name"]
    download(files[0]["browser_download_url"], archive)
    require(archive.stat().st_size == files[0]["size"], "Previous ZIP truncated")
    extract_zip(archive, previous / "unpacked")
    app = previous / "unpacked/Venus.app"
    app_facts(app, {**info, "version": previous_tag[1:]}, root / "evidence", "previous")
    return app


@contextlib.contextmanager
def asset_server(directory):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def provider_smoke(root, source, app, directory, info, public=False):
    scope = root / ("public-updater" if public else "candidate-updater")
    scope.mkdir()
    env = isolated_env(scope)
    args = ["node", source / "scripts/release-smoke.cjs", "provider", app, source, scope, info["version"]]
    if public:
        result = run([*args, "github"], env=env, log=root / "evidence/public-provider.log")
    else:
        with asset_server(directory) as url:
            result = run([*args, url], env=env, log=root / "evidence/candidate-provider.log")
    report = json.loads(result)
    expected = directory / f"Venus-{info['version']}-arm64-mac.zip"
    require(report["version"] == info["version"] and report["sha256"] == sha256(expected), "Previous updater downloaded different ZIP")
    return report


def backend_smoke(root, app):
    scope = root / "backend-smoke"
    scope.mkdir()
    env = isolated_env(scope)
    token = uuid.uuid4().hex
    env["LIVE_CLIPPER_WEB_TOKEN"] = token
    ffmpeg = app / "Contents/Resources/bin/ffmpeg"
    source = scope / "recordings"
    source.mkdir()
    run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1", "-c:v", "libx264", source / "synthetic.mp4"], env=env, log=root / "evidence/synthetic-media.log")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    binary = app / "Contents/Resources/backend/live-clipper-backend"
    evidence = root / "evidence/backend.log"
    env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"

    def request(route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + route, data=data, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    with evidence.open("w") as stream:
        process = subprocess.Popen([binary, "app", "--host", "127.0.0.1", "--port", str(port)], cwd=scope, env=env, stdout=stream, stderr=stream, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while True:
                require(process.poll() is None, "Packaged backend exited before readiness")
                try:
                    health = request("/api/onboarding")
                    break
                except (OSError, ValueError):
                    require(time.monotonic() < deadline, "Packaged backend readiness timed out")
                    threading.Event().wait(0.1)
            projects = request("/api/projects")
            preview = request("/api/projects/scan-preview", {"source_directory": str(source), "first_scan_mode": "choose_existing"})
            require(preview.get("processable_files") == 1, "Synthetic recording scan failed")
            request("/api/service/stop", {})
            return {"health": health, "projects": projects, "scan_preview": preview}
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            with socket.socket() as probe:
                require(probe.connect_ex(("127.0.0.1", port)) != 0, "Backend port still occupied after exit")


def candidate(args):
    repo = ROOT
    info = metadata(repo)
    check_source(repo, args.source, info["github"])
    require(re.fullmatch(r"v\d+\.\d+\.\d+", args.previous_tag), "Previous tag must be exact")
    require(version_tuple(info["version"]) > version_tuple(args.previous_tag[1:]), "Version must be newer than previous tag")
    previous_commit = git(repo, "rev-parse", f"{args.previous_tag}^{{commit}}")
    git(repo, "merge-base", "--is-ancestor", previous_commit, args.source)
    output = validate_output(args.output, repo)
    if (output / "candidate-manifest.json").is_file():
        manifest = verify_candidate(output / "candidate-manifest.json")
        require(manifest["source"] == args.source and manifest["previous_tag"] == args.previous_tag, "Existing candidate belongs to different input")
        require(manifest["tools"] == tools_snapshot(), "Tool environment changed")
        print("Candidate already frozen and verified; no rebuild: " + str(output / "candidate-manifest.json"))
        return
    require(not output.exists(), "Unfinished/unknown release root; preserve and inspect")
    tag = "v" + info["version"]
    require(not git(repo, "tag", "--list", tag), "Target tag already exists locally")
    require(not git(repo, "ls-remote", "origin", f"refs/tags/{tag}"), "Target tag exists remotely")
    require(github_release(info["github"], tag) is None, "Target release already exists")
    tools = tools_snapshot()
    identities = run(["security", "find-identity", "-v", "-p", "codesigning"])
    matches = re.findall(r'\b([0-9A-F]{40}) "(Developer ID Application:[^"\n]+)"', identities)
    require(len(matches) == 1, "Exactly one valid Developer ID identity required")
    require(shutil.disk_usage(output.parent).free >= 15 * 1024**3, "At least 15 GiB free space required")
    profile = os.environ.get("VENUS_NOTARY_PROFILE", "")
    require(profile.strip(), "Set VENUS_NOTARY_PROFILE to the approved keychain profile")
    run(["xcrun", "notarytool", "history", "--keychain-profile", profile, "--output-format", "json"], timeout=60)
    owner = create_root(output, repo, args.source)
    source = output / "source"
    evidence = output / "evidence"
    env = isolated_env(output / "build-environment")

    def step(name, command, **kwargs):
        print(name, flush=True)
        return run(command, cwd=source, env=env, log=evidence / f"{name}.log", **kwargs)

    run(["git", "clone", "--shared", "--no-checkout", repo, source], log=evidence / "source-clone.log")
    git(source, "checkout", "--detach", args.source)
    step("venv", ["python3.11", "-m", "venv", ".venv"])
    step("python-install", [".venv/bin/pip", "install", ".[mlx,dev]", "ruff==0.16.3", "pyinstaller>=6.10"])
    step("python-test", [".venv/bin/python", "-m", "pytest", "-q"])
    step("ruff-version", [".venv/bin/ruff", "--version"])
    step("ruff", [".venv/bin/ruff", "check", "src", "tests", "scripts/release.py"])
    step("python-build", [".venv/bin/python", "-m", "build"])
    step("pip-check", [".venv/bin/pip", "check"])
    step("pip-freeze", [".venv/bin/pip", "freeze"])
    step("desktop-install", ["npm", "--prefix", "desktop", "ci"])
    step("desktop-test", ["npm", "--prefix", "desktop", "test"])
    step("desktop-audit", ["npm", "--prefix", "desktop", "audit", "--omit=dev", "--audit-level=high"])
    step("backend-build", ["bash", "desktop/scripts/build-backend.sh"])
    step("frontend-audit", ["npm", "--prefix", "frontend", "audit", "--omit=dev", "--audit-level=high"])
    step("bundle-check", [".venv/bin/python", "scripts/ci/assert_backend_bundle.py"])
    # Use the login keychain for signing, without passing any password or enabling notarization.
    sign_env = {**env, "HOME": str(Path.home()), "CSC_NAME": matches[0][1], "CSC_IDENTITY_AUTO_DISCOVERY": "true"}
    directory = output / "candidate"
    run([source / "desktop/node_modules/.bin/electron-builder", "--mac", "--arm64", "--publish", "never",
         "-c.mac.notarize=false", "-c.forceCodeSigning=true", f"-c.directories.output={directory}"], cwd=source / "desktop", env=sign_env, log=evidence / "electron-builder.log", timeout=14400)
    update_metadata(directory, info["version"], source, evidence)
    signature = check_packages(output, directory, info, source, "candidate")
    app = directory / "mac-arm64/Venus.app"
    backend = backend_smoke(output, app)
    previous = previous_app(output, info["github"], args.previous_tag, info)
    provider = provider_smoke(output, source, previous, directory, info)
    require(not git(source, "diff", "--name-only"), "Build changed tracked source")
    check_source(repo, args.source, info["github"])
    machine = {"backend": backend, "provider": provider}
    atomic_json(evidence / "machine-checks.json", machine)
    manifest = {"format": 1, **info, "owner_id": owner["id"], "source": args.source,
                "tree": git(repo, "rev-parse", args.source + "^{tree}"), "previous_tag": args.previous_tag,
                "tools": tools, "signature": signature, "notary_profile": profile,
                "candidate_inventory": inventory(directory), "assets": asset_records(directory, info["version"]),
                "evidence": inventory(evidence), "previous_inventory": inventory(output / "previous"),
                "node_dependencies": inventory(source / "desktop/node_modules")}
    atomic_json(output / "candidate-manifest.json", manifest)
    atomic_json(output / "acceptance.json", acceptance_template(output / "candidate-manifest.json", args.source))
    atomic_json(output / "progress.json", {"candidate_sha256": sha256(output / "candidate-manifest.json")})
    print(f"Candidate ready, NOT accepted: {output / 'candidate-manifest.json'}\nHuman checklist: {output / 'acceptance.json'}\nLaunch only: {app / 'Contents/MacOS/Venus'}\nUse a new isolated LIVE_CLIPPER_HOME beneath {output}; verify PID executable and visible window.")


def verify_candidate(path):
    path = canonical_path(path)
    root = validate_output(path.parent, ROOT)
    require(path.name == "candidate-manifest.json", "Expected candidate-manifest.json")
    owner = read_json(root / "owner.json")
    data = read_json(path)
    require(data.get("format") == 1 and owner["repo"] == str(ROOT) and owner["source"] == data["source"] and owner["id"] == data["owner_id"], "Candidate ownership mismatch")
    verify_inventory(root / "candidate", data["candidate_inventory"])
    # Publish adds evidence, but frozen candidate evidence must still exist unchanged.
    current = inventory(root / "evidence")
    require(all(current.get(k) == v for k, v in data["evidence"].items()), "Candidate machine evidence changed")
    verify_inventory(root / "previous", data["previous_inventory"])
    source = root / "source"
    require(git(source, "rev-parse", "HEAD") == data["source"] and not git(source, "diff", "HEAD", "--name-only"), "Frozen source changed")
    verify_inventory(source / "desktop/node_modules", data["node_dependencies"])
    return data


def ensure_tag(repo, info, manifest, progress, state_path):
    tag = "v" + info["version"]
    annotation = f"Release {tag}\nCandidate {manifest['owner_id']}\nSource {manifest['source']}"
    if git(repo, "tag", "--list", tag):
        require(git(repo, "cat-file", "-t", f"refs/tags/{tag}") == "tag", "Existing tag is not annotated")
        require(git(repo, "rev-parse", f"{tag}^{{commit}}") == manifest["source"], "Existing tag points elsewhere")
        body = git(repo, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
        require(progress.get("tag_intent") == annotation and body == annotation, "Existing tag has no matching ownership record")
    else:
        require(not progress.get("tag_object"), "Recorded tag disappeared")
        progress["tag_intent"] = annotation
        atomic_json(state_path, progress)
        git(repo, "tag", "-a", tag, manifest["source"], "-m", annotation)
    tag_object = git(repo, "rev-parse", f"refs/tags/{tag}")
    require(progress.get("tag_object", tag_object) == tag_object, "Tag object changed")
    progress["tag_object"] = tag_object
    atomic_json(state_path, progress)
    return tag


def finalize(root, info, manifest):
    directory = root / "finalize"
    final_path = root / "final-manifest.json"
    if final_path.exists():
        final = read_json(final_path)
        require(final["candidate_sha256"] == sha256(root / "candidate-manifest.json"), "Final belongs to another candidate")
        verify_inventory(directory, final["inventory"])
        return final
    require(not directory.exists(), "Unfinished finalize; preserve it for diagnosis, do not overwrite")
    shutil.copytree(root / "candidate", directory, symlinks=True)
    source = root / "source"
    evidence = root / "evidence"
    app = directory / "mac-arm64/Venus.app"
    files = release_assets(directory, info["version"])
    for target in (app, directory / files["dmg"]):
        run(["xcrun", "stapler", "staple", target], log=evidence / f"staple-{target.suffix[1:]}.log")
        run(["xcrun", "stapler", "validate", target], log=evidence / f"ticket-{target.suffix[1:]}.log")
    facts = app_facts(app, info, evidence, "final")
    require(facts == manifest["signature"], "Staple changed App signature identity")
    run(["spctl", "--assess", "--type", "execute", "--verbose=4", app], log=evidence / "gatekeeper-app.log")
    run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", directory / files["dmg"]], log=evidence / "gatekeeper-dmg.log")
    # Only this owned copy is replaced, never the frozen candidate ZIP.
    (directory / files["zip"]).unlink()
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, directory / files["zip"]], log=evidence / "final-zip.log")
    update_metadata(directory, info["version"], source, evidence)
    extract_zip(directory / files["zip"], root / "final-unpacked")
    require(inventory(root / "final-unpacked/Venus.app") == inventory(app), "Final ZIP differs from stapled app")
    with mounted_dmg(directory / files["dmg"], root / "final-mount", evidence) as mount:
        require(app_facts(mount / "Venus.app", info, evidence, "final-dmg")["cdhash"] == facts["cdhash"], "Final DMG app differs")
    verify_inventory(root / "candidate", manifest["candidate_inventory"])
    final = {"candidate_sha256": sha256(root / "candidate-manifest.json"), "source": manifest["source"],
             "signature": facts, "inventory": inventory(directory), "assets": asset_records(directory, info["version"])}
    atomic_json(final_path, final)
    return final


def ensure_release(info, manifest, progress, state_path):
    github = info["github"]
    tag = "v" + info["version"]
    marker = f"<!-- release-candidate:{manifest['owner_id']} source:{manifest['source']} -->"
    existing = github_release(github, tag)
    if existing is None:
        require(not progress.get("release_intent"), "Release creation result uncertain; reconcile instead of repeating")
        progress["release_intent"] = marker
        atomic_json(state_path, progress)
        existing = json.loads(run(["gh", "api", "--method", "POST", f"repos/{github}/releases", "--input", "-"], input_text=json.dumps({
            "tag_name": tag, "target_commitish": manifest["source"], "name": tag, "body": info["notes"] + "\n\n" + marker, "draft": True, "prerelease": False})))
    require(progress.get("release_intent") == marker and marker in existing.get("body", ""), "Cannot adopt a release without matching ownership")
    require(existing["tag_name"] == tag and existing["target_commitish"] == manifest["source"] and not existing["prerelease"], "Release identity changed")
    require(progress.get("release_id", existing["id"]) == existing["id"], "Release ID changed")
    progress["release_id"] = existing["id"]
    atomic_json(state_path, progress)
    return existing


def verify_remote_asset(info, tag, name, expected, root, public_url=None):
    with tempfile.TemporaryDirectory(prefix="asset-check-", dir=root) as scratch:
        target = Path(scratch) / name
        if public_url:
            download(public_url, target)
        else:
            run(["gh", "release", "download", tag, "--repo", info["github"], "--pattern", name, "--dir", scratch])
        require(target.stat().st_size == expected["size"] and sha256(target) == expected["sha256"], f"Remote asset differs: {name}")


def upload_assets(root, info, final, release, progress, state_path):
    tag = "v" + info["version"]
    expected = final["assets"]
    assets = {a["name"]: a for a in release["assets"]}
    require(len(assets) == len(release["assets"]) and set(assets).issubset(expected), "Unexpected release assets")
    for name, facts in expected.items():
        local = root / "finalize" / name
        require(not local.is_symlink() and local.stat().st_size == facts["size"] and sha256(local) == facts["sha256"], "Final asset changed before upload")
        if name not in assets:
            require(release["draft"], "Published release is incomplete; stop, do not repair it in place")
            progress["upload_intent"] = name
            atomic_json(state_path, progress)
            # A lost response is reconciled by the next invocation's remote asset inventory.
            run(["gh", "release", "upload", tag, root / "finalize" / name, "--repo", info["github"]])
        verify_remote_asset(info, tag, name, facts, root)


def cleanup_owned(root, manifest):
    """Only successful, fully verified releases reach this bounded cleanup."""
    owner = read_json(root / "owner.json")
    require(owner["id"] == manifest["owner_id"] and owner["repo"] == str(ROOT), "Cleanup ownership mismatch")
    # Preserve a compact four-asset set before removing rebuildable contents.
    final = read_json(root / "final-manifest.json")
    assets = root / "release-assets"
    assets.mkdir(exist_ok=True)
    for name, record in final["assets"].items():
        target = assets / name
        require(not target.is_symlink(), "Cleanup asset symlink")
        if not target.exists():
            shutil.copy2(root / "finalize" / name, target)
        require(sha256(target) == record["sha256"], "Preserved formal asset mismatch")
    names = ("source", "candidate", "finalize", "candidate-unpacked", "final-unpacked", "previous",
             "build-environment", "backend-smoke", "candidate-updater", "public-updater", "candidate-mount", "final-mount")
    for name in names:
        target = canonical_path(root / name)
        if not target.exists():
            continue
        require(target.is_dir() and not os.path.ismount(target), "Cleanup target is not an owned directory")
        # lsof returns 1 for no open files, 0 for occupied, and other failures must stop.
        result = subprocess.run(["lsof", "-t", "+D", str(target)], capture_output=True, text=True)
        require(result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(), "Cleanup target occupied or process check failed")
        shutil.rmtree(target)


def publish(args):
    manifest_path = canonical_path(args.candidate)
    root = validate_output(manifest_path.parent, ROOT)
    if (root / "published-manifest.json").exists():
        # A completed invocation can verify the small retained assets after cleanup.
        done = read_json(root / "published-manifest.json")
        require(done["candidate_sha256"] == sha256(manifest_path) and args.confirm_version == done["tag"], "Published release identity differs")
        info = metadata(ROOT)
        require(info["version"] == done["tag"][1:] and info["github"] == done["github"], "Current repository changed")
        check_source(ROOT, done["source"], info["github"])
        verify_acceptance(args.acceptance, manifest_path, done["source"])
        remote = github_release(info["github"], done["tag"])
        require(remote and remote["id"] == done["release_id"] and not remote["draft"], "Published release missing or changed")
        for name, facts in done["assets"].items():
            require(sha256(root / "release-assets" / name) == facts["sha256"], "Retained asset changed")
            matches = [a for a in remote["assets"] if a["name"] == name]
            require(len(matches) == 1, "Public asset missing")
            verify_remote_asset(info, done["tag"], name, facts, root, matches[0]["browser_download_url"])
        if not done.get("cleanup_complete"):
            cleanup_owned(root, read_json(manifest_path))
            done["cleanup_complete"] = True
            atomic_json(root / "published-manifest.json", done)
        print("Published release verified; no repeated external writes.")
        return
    manifest = verify_candidate(manifest_path)
    info = metadata(ROOT)
    require(all(manifest[k] == info[k] for k in info), "Source release metadata changed")
    require(args.confirm_version == "v" + info["version"], "Explicit version confirmation mismatch")
    check_source(ROOT, manifest["source"], info["github"])
    require(tools_snapshot() == manifest["tools"], "Tool environment changed since candidate")
    verify_acceptance(args.acceptance, manifest_path, manifest["source"])
    state_path = root / "progress.json"
    progress = read_json(state_path)
    require(progress["candidate_sha256"] == sha256(manifest_path), "Progress belongs to another candidate")
    tag = ensure_tag(ROOT, info, manifest, progress, state_path)
    assets = release_assets(root / "candidate", info["version"])
    for kind in ("zip", "dmg"):
        notarize(root / "candidate" / assets[kind], manifest["notary_profile"], progress, state_path)
    final = finalize(root, info, manifest)
    check_source(ROOT, manifest["source"], info["github"])
    remote_tag = git(ROOT, "ls-remote", "origin", f"refs/tags/{tag}")
    if remote_tag:
        require(remote_tag.split()[0] == progress["tag_object"], "Remote tag object differs")
    else:
        git(ROOT, "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    require(git(ROOT, "ls-remote", "origin", f"refs/tags/{tag}").split()[0] == progress["tag_object"], "Tag push verification failed")
    release = ensure_release(info, manifest, progress, state_path)
    upload_assets(root, info, final, release, progress, state_path)
    if release["draft"]:
        check_source(ROOT, manifest["source"], info["github"])
        progress["publish_intent"] = True
        atomic_json(state_path, progress)
        run(["gh", "api", "--method", "PATCH", f"repos/{info['github']}/releases/{release['id']}", "--input", "-"], input_text='{"draft":false,"make_latest":"true"}')
    release = github_release(info["github"], tag)
    require(release and not release["draft"] and release["id"] == progress["release_id"], "Public release verification failed")
    progress["public"] = True
    atomic_json(state_path, progress)
    for name, facts in final["assets"].items():
        matches = [a for a in release["assets"] if a["name"] == name]
        require(len(matches) == 1, "Public asset set differs")
        verify_remote_asset(info, tag, name, facts, root, matches[0]["browser_download_url"])
    provider = provider_smoke(root, root / "source", root / "previous/unpacked/Venus.app", root / "finalize", info, public=True)
    done = {"source": manifest["source"], "tag": tag, "tag_object": progress["tag_object"], "github": info["github"],
            "release_id": release["id"], "url": release["html_url"], "assets": final["assets"], "provider": provider,
            "candidate_sha256": sha256(manifest_path), "acceptance_sha256": sha256(args.acceptance)}
    # Retain formal assets before writing the published record; cleanup failures remain recoverable.
    (root / "release-assets").mkdir(exist_ok=True)
    for name in final["assets"]:
        target = root / "release-assets" / name
        require(not target.is_symlink(), "Formal asset symlink")
        if target.exists():
            require(sha256(target) == final["assets"][name]["sha256"], "Existing formal asset differs")
        else:
            shutil.copy2(root / "finalize" / name, target)
    atomic_json(root / "published-manifest.json", done)
    cleanup_owned(root, manifest)
    done["cleanup_complete"] = True
    atomic_json(root / "published-manifest.json", done)
    print("Published and publicly verified: " + release["html_url"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("candidate", help="Build once; never tag, notarize or publish")
    build.add_argument("--source", required=True)
    build.add_argument("--previous-tag", required=True)
    build.add_argument("--output", type=Path, required=True)
    ship = commands.add_parser("publish", help="Requires human acceptance and explicit release authorization")
    ship.add_argument("--candidate", type=Path, required=True)
    ship.add_argument("--acceptance", type=Path, required=True)
    ship.add_argument("--confirm-version", required=True)
    args = parser.parse_args(argv)
    try:
        common = Path(git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        with exclusive_lock(common):
            (candidate if args.command == "candidate" else publish)(args)
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # No raw command output: it may contain signed URLs or credentials.
        message = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print(f"Release stopped; preserve the working directory. {message}", file=sys.stderr)
        if args.command == "publish":
            state = args.candidate.parent / "progress.json"
            if state.is_file() and read_json(state).get("publish_intent"):
                print("Release may already be public; verification/cleanup is incomplete. Do not rebuild, delete or re-publish it.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
