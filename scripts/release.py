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
    root = Path(env['VENUS_RELEASE_ROOT']) if env and env.get('VENUS_RELEASE_ROOT') else None
    if root:
        owner_record(root)
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            remember_process(root, process, cwd)
            stdout, stderr = process.communicate(input_text, timeout=timeout)
            result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            forget_process(root, process.pid)
    else:
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


def python_identity():
    entry = shutil.which("python3.11")
    require(entry, "Missing python3.11")
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    # A venv or external launcher may not live beside the base standard library.
    executable = run([entry, "-I", "-c", "import os,sys; print(os.path.realpath(sys._base_executable))"], env=env, timeout=60).strip()
    require(executable and Path(executable).is_absolute(), "Invalid base Python executable")
    executable = canonical_path(Path(executable))
    require(executable.is_file() and os.access(executable, os.X_OK), "Base Python is not executable")
    facts = json.loads(run([executable, "-I", "-c",
        "import encodings,ssl,sqlite3,venv,sys,sysconfig,json; "
        "print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),'executable':sys.executable,"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix,'stdlib':sysconfig.get_path('stdlib')}))"], env=env, timeout=60))
    require(facts['version'].startswith('3.11.') and facts['executable'] == str(executable)
            and facts.pop('prefix') == facts['base_prefix'], "Python 3.11 base interpreter required")
    for key in ('base_prefix', 'stdlib'):
        require(canonical_path(Path(facts[key])).is_dir(), f"Missing Python {key}")
    media_python = shutil.which("python3.11", path=str(executable.parent))
    require(media_python and Path(media_python).resolve() == executable, "Base Python directory lacks matching python3.11")
    facts['sha256'] = sha256(executable)
    return facts


def tools_snapshot():
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "macOS arm64 required")
    require(not any(os.environ.get(key) for key in ("DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS")),
            "Remove Apple toolchain overrides; builds use the system developer directory")
    python = python_identity()
    commands = {"node": ["node", "--version"], "npm": ["npm", "--version"],
                "git": ["git", "--version"], "gh": ["gh", "--version"]}
    values = {key: run(cmd, timeout=60).strip() for key, cmd in commands.items()}
    values["python"] = "Python " + python["version"]
    values["python_identity"] = python
    require(values["node"].startswith("v24.") and values["npm"].startswith("11."), "Python 3.11 / Node 24 / npm 11 required")
    # Match build-media-tools.py's system environment, without creating a build home.
    apple_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    apple_commands = {"developer_dir": ["/usr/bin/xcode-select", "-p"],
                      "clang": ["/usr/bin/clang", "--version"],
                      "sdk_version": ["/usr/bin/xcrun", "--show-sdk-version"],
                      "sdk_path": ["/usr/bin/xcrun", "--show-sdk-path"]}
    for key, command in apple_commands.items():
        values[key] = run(command, env=apple_env, timeout=60).strip()
    require(all(values.values()), "Required tool probe returned empty output")
    paths = {}
    for executable in ("codesign", "security", "xcrun", "ditto", "hdiutil", "spctl", "lipo", "lsof", "make", "otool"):
        paths[executable] = shutil.which(executable, path=apple_env["PATH"] if executable in ("make", "otool") else None)
        require(paths[executable], f"Missing {executable}")
    for executable in ("clang", "vtool", "notarytool", "stapler"):
        paths[executable] = run(["/usr/bin/xcrun", "--find", executable], env=apple_env, timeout=60).strip()
        require(paths[executable], f"Missing {executable}")
    run(["gh", "auth", "status"], timeout=60)
    values["tool_paths"] = paths
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


# Local P1 footprint was ~4.2 GiB; measured cold/hot Desktop + pinned MLX
# downloads used ~390 MiB. Reserve 15 GiB for builds and cap the shared cache
# at 4 GiB for frontend/build-tool downloads and three media identities.
BUILD_SPACE = 15 * 1024**3
CACHE_LIMIT = 4 * 1024**3


def directory_bytes(path):
    path = Path(path)
    if not path.exists() or path.is_symlink():
        return 0
    return sum(p.lstat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def owner_record(root):
    root = canonical_path(root)
    data = read_json(root / 'owner.json')
    require(data.get('format') == 1 and data.get('repo') == str(ROOT.resolve()) and data.get('id'), 'Directory ownership mismatch')
    return data


def owned_directory(root, name):
    owner = owner_record(root)
    target = canonical_path(root / name)
    require(target != root and target.is_relative_to(root), 'Owned directory escapes release root')
    require(not target.exists(), 'Owned directory already exists')
    target.mkdir(parents=True)
    facts = target.stat()
    owner.setdefault('directories', {})[name] = {'device': facts.st_dev, 'inode': facts.st_ino}
    atomic_json(root / 'owner.json', owner)
    return target


def remove_owned(root, name):
    """Return a retryable result; never infer ownership from a directory name."""
    target = root / name
    result = {'path': str(target), 'bytes_before': directory_bytes(target), 'removed': False}
    try:
        owner = owner_record(root)
        identity = owner.get('directories', {}).get(name)
        require(identity is not None, 'unregistered directory')
        canonical_path(target)
        require(target != root and target.is_relative_to(root), 'directory escapes release root')
        if not target.exists():
            return {**result, 'removed': True, 'already_absent': True}
        facts = target.stat()
        require(identity == {'device': facts.st_dev, 'inode': facts.st_ino}, 'directory identity changed')
        for path in (target, *target.rglob('*')):
            require(not os.path.ismount(path), 'mounted directory')
            require(path.resolve().is_relative_to(target), 'escaping symlink')
        occupied = subprocess.run(['lsof', '-t', '+D', str(target)], capture_output=True, text=True)
        require(occupied.returncode == 1 and not occupied.stdout.strip() and not occupied.stderr.strip(), 'occupied or occupancy check failed')
        shutil.rmtree(target)
        require(not target.exists(), 'directory remains')
        result['removed'] = True
    except (ReleaseError, OSError) as exc:
        result['reason'] = str(exc)
    return result


def space_snapshot(root, cache=None):
    location = root if root.exists() else root.parent
    return {'at': datetime.now(UTC).isoformat(), 'root_bytes': directory_bytes(root),
            'volume_free_bytes': shutil.disk_usage(location).free,
            'cache_bytes': directory_bytes(cache) if cache else 0,
            'cache_volume_free_bytes': shutil.disk_usage(cache if cache.exists() else cache.parent).free if cache else None}


def space_preflight(output, cache, repo):
    report = space_snapshot(output, cache)
    report.update(required_build_bytes=BUILD_SPACE, cache_limit_bytes=CACHE_LIMIT,
                  basis='Observed P1 ~4.2 GiB; 15 GiB conservative cold build/sign/finalize reserve plus bounded shared cache',
                  residuals=[])
    for path in sorted(output.parent.iterdir()):
        if not path.is_dir() or path.is_symlink() or path == cache:
            continue
        owned = False
        try:
            owner = read_json(path / 'owner.json')
            owned = owner.get('repo') == str(repo.resolve()) and owner.get('format') == 1
        except (OSError, ValueError):
            pass
        report['residuals'].append({'path': str(path), 'bytes': directory_bytes(path),
                                    'action': f'cleanup --root {path}' if owned else 'unknown/manual review only'})
    print(json.dumps(report, indent=2), flush=True)
    same_volume = output.parent.stat().st_dev == cache.parent.stat().st_dev
    require(report['volume_free_bytes'] >= BUILD_SPACE + (CACHE_LIMIT if same_volume else 0), 'Insufficient build space; inspect reported owned cleanup candidates')
    require(same_volume or report['cache_volume_free_bytes'] >= CACHE_LIMIT, 'Insufficient cache volume space')
    return report


def prepare_download_cache(parent, repo):
    cache = canonical_path(parent / 'download-cache')
    identity = {'format': 1, 'repo': str(repo.resolve()), 'kind': 'venus-download-cache'}
    if cache.exists():
        require(read_json(cache / 'owner.json') == identity, 'Unknown download cache owner')
    else:
        cache.mkdir(mode=0o700)
        atomic_json(cache / 'owner.json', identity)
    for name in ('npm', 'pip', 'electron', 'electron-builder', 'media', 'media-archives'):
        canonical_path(cache / name).mkdir(exist_ok=True)
    return cache


def limit_download_cache(cache, python=None):
    canonical_path(cache)
    require(read_json(canonical_path(cache / 'owner.json')) == {'format': 1, 'repo': str(ROOT.resolve()), 'kind': 'venus-download-cache'}, 'Cache ownership mismatch')
    before = directory_bytes(cache)
    if before > CACHE_LIMIT:
        for child in cache.iterdir():
            require(not child.is_symlink(), 'Unsafe cache child')
        occupied = subprocess.run(['lsof', '-t', '+D', str(cache)], capture_output=True, text=True)
        require(occupied.returncode == 1 and not occupied.stdout and not occupied.stderr, 'Cache occupied; purge deferred')
        # The shared cache has no reliable per-version download ownership. Purge
        # only our bounded cache, using package-manager commands where available.
        run(['npm', 'cache', 'clean', '--force', '--cache', cache / 'npm', '--userconfig=/dev/null'])
        run([python or python_identity()['executable'], '-I', '-m', 'pip', '--isolated', 'cache', '--cache-dir', cache / 'pip', 'purge'])
        for name in ('electron', 'electron-builder', 'media-archives'):
            path = canonical_path(cache / name)
            require(not any(os.path.ismount(p) or not p.resolve().is_relative_to(path) for p in (path, *path.rglob('*'))), 'Unsafe cache path')
            shutil.rmtree(path)
            path.mkdir()
    require(directory_bytes(cache) <= CACHE_LIMIT, 'Owned cache exceeds capacity; preserve active media entries and review cache')
    return {'before': before, 'after': directory_bytes(cache), 'limit': CACHE_LIMIT}


def process_identity(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def resource_state(root):
    path = root / 'resources.json'
    return read_json(path) if path.exists() else {'processes': [], 'mounts': []}


def remember_process(root, process, cwd, port=None):
    state = resource_state(root)
    state['processes'].append({'pid': process.pid, 'pgid': os.getpgid(process.pid),
                              'identity': process_identity(process.pid), 'cwd': str(cwd), 'port': port})
    atomic_json(root / 'resources.json', state)


def forget_process(root, pid):
    state = resource_state(root)
    state['processes'] = [item for item in state['processes'] if item['pid'] != pid]
    atomic_json(root / 'resources.json', state)


def recover_resources(root):
    owner_record(root)
    state = resource_state(root)
    issues = []
    for item in list(state['processes']):
        actual = process_identity(item['pid'])
        if actual is not None:
            if not item.get('identity') or actual != item['identity'] or os.getpgid(item['pid']) != item['pgid']:
                issues.append({'pid': item['pid'], 'reason': 'process identity changed; not signalled'})
                continue
            require(item['pgid'] == item['pid'], 'Recorded process is not an owned session leader')
            try:
                os.killpg(item['pgid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 15
            while process_identity(item['pid']) == actual and time.monotonic() < deadline:
                try:
                    os.waitpid(item['pid'], os.WNOHANG)
                except ChildProcessError:
                    pass
                threading.Event().wait(0.1)
            if process_identity(item['pid']) == actual:
                issues.append({'pid': item['pid'], 'reason': 'owned process did not exit gracefully'})
                continue
        if item.get('port'):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', item['port'])) == 0:
                    issues.append({'port': item['port'], 'reason': 'port occupied; owner not signalled'})
                    continue
        state['processes'].remove(item)
    for item in list(state['mounts']):
        mount = canonical_path(Path(item['path']))
        require(mount.is_relative_to(root), 'Mount escapes release root')
        images = plistlib.loads(subprocess.check_output(['hdiutil', 'info', '-plist']))['images']
        matches = [im for im in images if any(e.get('mount-point') == str(mount) for e in im.get('system-entities', []))]
        if matches:
            if len(matches) != 1 or str(Path(matches[0]['image-path']).resolve()) != item['image']:
                issues.append({'path': str(mount), 'reason': 'mount identity changed; not detached'})
                continue
            run(['hdiutil', 'detach', mount])
        elif os.path.ismount(mount):
            issues.append({'path': str(mount), 'reason': 'unknown mount; not detached'})
            continue
        state['mounts'].remove(item)
    atomic_json(root / 'resources.json', state)
    return issues


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


def isolated_env(root, cache=None, python=None):
    # No ambient application config, model/AI secrets, proxy auth, or Python import paths.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    for name in ("home", "cache", "config", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(root / "home"), XDG_CACHE_HOME=str(root / "cache"),
               XDG_CONFIG_HOME=str(root / "config"), TMPDIR=str(root / "tmp"),
               LIVE_CLIPPER_HOME=str(root / "app-home"), PYTHONNOUSERSITE="1")
    env.update(PIP_CONFIG_FILE=os.devnull, npm_config_userconfig=os.devnull)
    if python:
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", os.defpath)
    if cache:
        env.update(PIP_CACHE_DIR=str(cache / 'pip'), npm_config_cache=str(cache / 'npm'),
                   ELECTRON_CACHE=str(cache / 'electron'), electron_config_cache=str(cache / 'electron'), ELECTRON_BUILDER_CACHE=str(cache / 'electron-builder'),
                   VENUS_RELEASE_MEDIA_CACHE=str(cache / 'media'), VENUS_RELEASE_ARCHIVE_CACHE=str(cache / 'media-archives'))
    for parent in (root, *root.parents):
        if (parent / 'owner.json').is_file():
            env['VENUS_RELEASE_ROOT'] = str(parent)
            break
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
    for parent in target.parents:
        if (parent / 'owner.json').is_file():
            owned_directory(parent, target.relative_to(parent).as_posix())
            break
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
    require(plist.get('LSMinimumSystemVersion') == '14.0', 'Minimum macOS declaration must be 14.0')
    resources = app / "Contents/Resources"
    binaries = [app / "Contents/MacOS/Venus", resources / "backend/live-clipper-backend", resources / "bin/ffmpeg", resources / "bin/ffprobe"]
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
    root = evidence.parent
    owner_record(root)
    owned_directory(root, mount.relative_to(root).as_posix())
    state = resource_state(root)
    state['mounts'].append({'path': str(mount), 'image': str(canonical_path(dmg))})
    atomic_json(root / 'resources.json', state)
    try:
        run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, dmg], log=evidence / f"{mount.name}-attach.log")
        yield mount
    finally:
        # Recovery matches the exact image and mountpoint, including partial attach.
        require(not recover_resources(root), 'Resource recovery incomplete; inspect resources.json')
        require(remove_owned(root, mount.relative_to(root).as_posix())['removed'], 'Mount directory cleanup failed')


def check_packages(root, directory, info, source, label):
    evidence = root / "evidence"
    app = directory / "mac-arm64/Venus.app"
    facts = app_facts(app, info, evidence, label)
    check_packaged_privacy(app)
    require((app / "Contents/Resources/app-update.yml").read_bytes() == (source / "desktop/build/app-update.yml").read_bytes(), "Packaged updater configuration differs")
    run([source / ".venv/bin/python", source / "scripts/ci/assert_backend_bundle.py", "--bundle", app / "Contents/Resources/backend"], log=evidence / f"{label}-backend-bundle.log")
    files = release_assets(directory, info["version"])
    check_media_source(directory, app, info)
    run(['node', source / 'desktop/media-runtime.js', app / 'Contents/Resources'], log=evidence / f'{label}-media.log')
    check_dir = root / f"{label}-unpacked"
    extract_zip(directory / files["zip"], check_dir)
    require(inventory(check_dir / "Venus.app") == inventory(app), "ZIP/App contents differ")
    with mounted_dmg(directory / files["dmg"], root / f"{label}-mount", evidence) as mount:
        # Finder metadata outside Venus.app is deliberately not part of app identity.
        require(inventory(mount / "Venus.app") == inventory(app), "DMG/App contents differ")
    require(remove_owned(root, check_dir.name)['removed'], 'Verified ZIP extraction cleanup failed')
    return facts


def asset_names(version):
    return {"dmg": f"Venus-{version}-arm64.dmg", "zip": f"Venus-{version}-arm64-mac.zip", "blockmap": f"Venus-{version}-arm64-mac.zip.blockmap", "latest": "latest-mac.yml", "media_source": f"Venus-{version}-media-sources.tar.gz"}


def release_assets(directory, version):
    expected = asset_names(version)
    require(all((directory / name).is_file() and not (directory / name).is_symlink() for name in expected.values()), "Five release assets required")
    return expected


def check_media_source(directory, app, info):
    source = directory / release_assets(directory, info['version'])['media_source']
    notice = (app / 'Contents/Resources/licenses/ffmpeg/CORRESPONDING-SOURCE.md').read_text()
    url = f"https://github.com/{info['github']}/releases/download/v{info['version']}/{source.name}"
    require(url in notice and sha256(source) in notice, 'Media source notice/name/version/hash mismatch')
    require(not (app / 'Contents/Resources/media-sources.tar.gz').exists(), 'Source archive must not be bundled in App')
    return {'name': source.name, 'sha256': sha256(source), 'url': url}


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
    owned_directory(root, "previous")
    archive = previous / files[0]["name"]
    download(files[0]["browser_download_url"], archive)
    require(archive.stat().st_size == files[0]["size"], "Previous ZIP truncated")
    extract_zip(archive, previous / "unpacked")
    archive.unlink()
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
    if (root / 'owner.json').exists():
        owned_directory(root, scope.name)
    else:
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
    if (root / 'owner.json').is_file():
        require(remove_owned(root, scope.name)['removed'], 'Provider temporary data cleanup failed')
    return report


def backend_smoke(root, app):
    scope = root / "backend-smoke"
    if (root / 'owner.json').exists():
        owned_directory(root, scope.name)
    else:
        scope.mkdir()
    env = isolated_env(scope)
    token = uuid.uuid4().hex
    env["LIVE_CLIPPER_WEB_TOKEN"] = token
    ffmpeg = app / "Contents/Resources/bin/ffmpeg"
    source = scope / "recordings"
    source.mkdir()
    raw = scope / 'synthetic.rgb'
    raw.write_bytes(bytes((0, 0, 255)) * (320 * 180 * 25))
    env['PATH'] = str(ffmpeg.parent) + ':/usr/bin:/bin:/usr/sbin:/sbin'
    run([ffmpeg, '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '320x180',
         '-framerate', '25', '-i', raw, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', source / 'synthetic.mp4'],
        env=env, log=root / 'evidence/synthetic-media.log')
    raw.unlink()
    env['PATH'] = str(ffmpeg.parent) + ':/usr/bin:/bin:/usr/sbin:/sbin'
    probe = app / 'Contents/Resources/bin/ffprobe'
    media = json.loads(run([probe, '-v', 'error', '-show_streams', '-of', 'json', source / 'synthetic.mp4'], env=env))
    require(any(x.get('codec_name') == 'h264' for x in media['streams']), 'Bundled ffprobe synthetic media check failed')
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    binary = app / "Contents/Resources/backend/live-clipper-backend"
    evidence = root / "evidence/backend.log"
    env["PATH"] = str(ffmpeg.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"

    def request(route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + route, data=data, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    with evidence.open("w") as stream:
        process = subprocess.Popen([binary, "app", "--host", "127.0.0.1", "--port", str(port)], cwd=scope, env=env, stdout=stream, stderr=stream, start_new_session=True)
        tracked = (root / 'owner.json').is_file()
        if tracked:
            remember_process(root, process, scope, port)
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
            if tracked:
                forget_process(root, process.pid)


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
    space = space_preflight(output, output.parent / "download-cache", repo)
    profile = os.environ.get("VENUS_NOTARY_PROFILE", "")
    require(profile.strip(), "Set VENUS_NOTARY_PROFILE to the approved keychain profile")
    run(["xcrun", "notarytool", "history", "--keychain-profile", profile, "--output-format", "json"], timeout=60)
    owner = create_root(output, repo, args.source)
    owner['version'] = info['version']
    atomic_json(output / 'owner.json', owner)
    source = output / "source"
    evidence = output / "evidence"
    atomic_json(evidence / 'space-preflight.json', space)
    cache = prepare_download_cache(output.parent, repo)
    owned_directory(output, 'build-environment')
    python = tools["python_identity"]["executable"]
    env = isolated_env(output / "build-environment", cache=cache, python=python)
    prune_media_cache(cache, cache_keep_versions(cleanup_plan(output)))
    limit_download_cache(cache, python=python)

    def step(name, command, **kwargs):
        print(name, flush=True)
        result = run(command, cwd=source, env=env, log=evidence / f"{name}.log", **kwargs)
        atomic_json(evidence / f'{name}-space.json', space_snapshot(output, cache))
        limit_download_cache(cache, python=python)
        return result

    owned_directory(output, "source")
    run(["git", "clone", "--shared", "--no-checkout", repo, source], log=evidence / "source-clone.log")
    git(source, "checkout", "--detach", args.source)
    step("venv", [python, "-I", "-m", "venv", "--copies", ".venv"])
    step("python-install", [".venv/bin/pip", "install", ".[mlx,dev]", "-r", "desktop/build/mlx-requirements.txt", "ruff==0.16.3", "pyinstaller>=6.10"])
    step("python-test", [".venv/bin/python", "-m", "pytest", "-q"])
    step("ruff-version", [".venv/bin/ruff", "--version"])
    step("ruff", [".venv/bin/ruff", "check", "src", "tests", "scripts/release.py"])
    step("python-build", [".venv/bin/python", "-m", "build"])
    step("pip-check", [".venv/bin/pip", "check"])
    step("pip-freeze", [".venv/bin/pip", "freeze"])
    step("desktop-install", ["npm", "--prefix", "desktop", "ci"])
    step("desktop-test", ["npm", "--prefix", "desktop", "test"])
    audit = json.loads(step("desktop-audit", ["npm", "--prefix", "desktop", "audit", "--json"]))
    require(audit["metadata"]["vulnerabilities"]["total"] == 0, "Desktop audit must include dev dependencies and report zero vulnerabilities")
    step("backend-build", ["bash", "desktop/scripts/build-backend.sh"])
    step("frontend-audit", ["npm", "--prefix", "frontend", "audit", "--omit=dev", "--audit-level=high"])
    step("bundle-check", [".venv/bin/python", "scripts/ci/assert_backend_bundle.py"])
    # Use the login keychain for signing, without passing any password or enabling notarization.
    sign_env = {**env, "HOME": str(Path.home()), "CSC_NAME": matches[0][1], "CSC_IDENTITY_AUTO_DISCOVERY": "true"}
    directory = owned_directory(output, "candidate")
    run([source / "desktop/node_modules/.bin/electron-builder", "--mac", "--arm64", "--publish", "never",
         "-c.mac.notarize=false", "-c.forceCodeSigning=true", f"-c.directories.output={directory}"], cwd=source / "desktop", env=sign_env, log=evidence / "electron-builder.log", timeout=14400)
    media = source / 'desktop/vendor/media-tools/darwin-arm64'
    media_identity = read_json(media / 'build-manifest.json')
    shutil.copy2(media / 'media-sources.tar.gz', directory / f"Venus-{info['version']}-media-sources.tar.gz")
    update_metadata(directory, info["version"], source, evidence)
    signature = check_packages(output, directory, info, source, "candidate")
    app = directory / "mac-arm64/Venus.app"
    backend = backend_smoke(output, app)
    previous = previous_app(output, info["github"], args.previous_tag, info)
    provider = provider_smoke(output, source, previous, directory, info)
    require(not git(source, "diff", "--name-only"), "Build changed tracked source")
    check_source(repo, args.source, info["github"])
    machine = {"backend": backend, "provider": provider}
    atomic_json(evidence / 'space-built-verified.json', space_snapshot(output, cache))
    atomic_json(evidence / "machine-checks.json", machine)
    manifest = {"format": 1, **info, "owner_id": owner["id"], "source": args.source,
                "tree": git(repo, "rev-parse", args.source + "^{tree}"), "previous_tag": args.previous_tag,
                "tools": tools, "signature": signature, "notary_profile": profile, "media_build": media_identity, "cache": str(cache),
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
    require(data['assets'] == asset_records(root / 'candidate', data['version']), 'Candidate five-asset manifest mismatch')
    check_media_source(root / 'candidate', root / 'candidate/mac-arm64/Venus.app', data)
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
        require(final['assets'] == asset_records(directory, info['version']), 'Final five-asset manifest mismatch')
        check_media_source(directory, directory / 'mac-arm64/Venus.app', info)
        name = asset_names(info['version'])['media_source']
        require(final['assets'][name] == manifest['assets'][name], 'Finalize changed corresponding source')
        return final
    require(not directory.exists(), "Unfinished finalize; preserve it for diagnosis, do not overwrite")
    owned_directory(root, "finalize")
    shutil.copytree(root / "candidate", directory, symlinks=True, dirs_exist_ok=True)
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
    require(remove_owned(root, 'final-unpacked')['removed'], 'Final extraction cleanup failed')
    check_media_source(directory, app, info)
    media_name = files['media_source']
    require(sha256(directory / media_name) == manifest['assets'][media_name]['sha256'], 'Finalize changed corresponding source')
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
    require(set(expected) == set(asset_names(info["version"]).values()), "Five upload assets required")
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


def published_record(root):
    owner = owner_record(root)
    done = read_json(root / 'published-manifest.json')
    candidate = read_json(root / 'candidate-manifest.json')
    require(candidate['owner_id'] == owner['id'] and candidate['source'] == owner['source'] == done['source'], 'Published ownership mismatch')
    require(done['candidate_sha256'] == sha256(root / 'candidate-manifest.json'), 'Published candidate hash mismatch')
    require(re.fullmatch(r'v\d+\.\d+\.\d+', done['tag']), 'Invalid published version')
    expected = asset_names(done['tag'][1:])
    require(set(done['assets']) == set(expected.values()), 'Five published assets required')
    return done


def cleanup_plan(root):
    owner_record(root)
    completed = []
    unknown = []
    for path in sorted(root.parent.iterdir()):
        if not path.is_dir() or path.is_symlink() or path.name == 'download-cache':
            continue
        try:
            owner_record(path)
        except (ReleaseError, OSError, ValueError):
            unknown.append({'path': str(path), 'bytes': directory_bytes(path), 'reason': 'unknown/manual directory; not owned'})
            continue
        if (path / 'published-manifest.json').exists():
            completed.append((path, published_record(path)))
    completed.sort(key=lambda pair: version_tuple(pair[1]['tag'][1:]), reverse=True)
    retained = {str(path) for path, _ in completed[:2]}
    releases = []
    for path, _done in completed:
        releases.append({'root': str(path), 'published': True, 'retain_assets': str(path) in retained,
                         'directories': list(owner_record(path).get('directories', {})), 'bytes': directory_bytes(path)})
    # Failed runs require an explicit incident resolution linking a verified,
    # completed successor. A similar name or newer directory is never enough.
    for path in sorted(root.parent.iterdir()):
        if not path.is_dir() or path.is_symlink() or any(str(path) == item['root'] for item in releases):
            continue
        try:
            owner = owner_record(path)
            resolution = read_json(path / 'resolution.json')
            successor = canonical_path(Path(resolution['successor']))
            done = published_record(successor)
            require(successor.parent == root.parent and resolution['owner_id'] == owner['id'], 'Resolution owner mismatch')
            require(resolution['reason'].strip() and resolution['successor_candidate_sha256'] == done['candidate_sha256'], 'Resolution evidence missing')
            git(ROOT, 'merge-base', '--is-ancestor', owner['source'], done['source'])
        except (ReleaseError, OSError, ValueError, KeyError):
            continue
        releases.append({'root': str(path), 'published': False, 'retain_assets': False,
                         'directories': list(owner.get('directories', {})), 'bytes': directory_bytes(path)})
    unresolved = [str(p) for p in root.parent.iterdir() if p.is_dir() and (p / 'owner.json').is_file()
                  and p.name != 'download-cache' and not any(str(p) == item['root'] for item in releases)]
    return {'releases': releases, 'manual_only': unknown, 'preserved_unresolved': unresolved, 'retained_releases': sorted(retained)}


def retain_formal_assets(root, done):
    target = root / 'release-assets'
    if not target.exists():
        owned_directory(root, 'release-assets')
    else:
        owner = owner_record(root)
        facts = target.stat()
        require(owner.get('directories', {}).get('release-assets') == {'device': facts.st_dev, 'inode': facts.st_ino}, 'Formal asset directory identity changed')
    for name, record in done['assets'].items():
        require(Path(name).name == name, 'Unsafe formal asset name')
        file = canonical_path(target / name)
        if not file.exists():
            original = canonical_path(root / 'finalize' / name)
            require(sha256(original) == record['sha256'], 'Final asset changed')
            shutil.copy2(original, file)
        require(file.stat().st_size == record['size'] and sha256(file) == record['sha256'], 'Preserved formal asset mismatch')


def prune_media_cache(cache, keep_versions):
    require(read_json(canonical_path(cache / 'owner.json')) == {'format': 1, 'repo': str(ROOT.resolve()), 'kind': 'venus-download-cache'}, 'Cache ownership mismatch')
    index = canonical_path(cache / 'media/entries.json')
    if not index.exists():
        return
    entries = read_json(index)
    for key, facts in list(entries.items()):
        require(re.fullmatch(r'[0-9a-f]{64}', key), 'Unsafe media cache key')
        if facts['version'] in keep_versions:
            continue
        path = canonical_path(cache / 'media' / key)
        if path.exists():
            current = path.stat()
            require(current.st_dev == facts['device'] and current.st_ino == facts['inode'], 'Media cache identity changed')
            require(not any(os.path.ismount(p) or not p.resolve().is_relative_to(path) for p in (path, *path.rglob('*'))), 'Unsafe media cache tree')
            occupied = subprocess.run(['lsof', '-t', '+D', str(path)], capture_output=True, text=True)
            require(occupied.returncode == 1 and not occupied.stdout and not occupied.stderr, 'Media cache occupied')
            shutil.rmtree(path)
        del entries[key]
    atomic_json(index, entries)


def cache_keep_versions(plan):
    keep = {published_record(Path(p))['tag'][1:] for p in plan['retained_releases']}
    for value in plan['preserved_unresolved']:
        try:
            owner = owner_record(Path(value))
            if owner.get('version'):
                keep.add(owner['version'])
        except ReleaseError:
            pass
    return keep


def cleanup(root, apply=False):
    root = validate_output(root, ROOT)
    plan = cleanup_plan(root)
    if not apply:
        print(json.dumps(plan, indent=2))
        return plan
    before = space_snapshot(root)
    results = []
    recovery = recover_resources(root)
    for item in plan['releases']:
        path = Path(item['root'])
        issues = recover_resources(path)
        if issues:
            results.append({'root': str(path), 'issues': issues, 'complete': False})
            continue
        try:
            done = published_record(path) if item['published'] else None
            if done:
                if item['retain_assets'] or (path / 'release-assets').exists() or not (path / 'public-recovery.json').exists():
                    retain_formal_assets(path, done)
                if not item['retain_assets'] and (path / 'release-assets').exists():
                    remote = github_release(done['github'], done['tag'])
                    require(remote and remote['id'] == done['release_id'] and not remote['draft'], 'Public recovery source not confirmed')
                    for name, facts in done['assets'].items():
                        matches = [a for a in remote['assets'] if a['name'] == name]
                        require(len(matches) == 1, 'Public recovery asset missing')
                        verify_remote_asset(done, done['tag'], name, facts, path, matches[0]['browser_download_url'])
                    atomic_json(path / 'public-recovery.json', {'release_id': done['release_id'], 'assets': done['assets'], 'verified_at': datetime.now(UTC).isoformat()})
            # Archive the compiler logs before deleting a registered source tree.
            source = path / 'source'
            if source.exists() and 'source' in owner_record(path).get('directories', {}):
                facts = source.stat()
                require(owner_record(path)['directories']['source'] == {'device': facts.st_dev, 'inode': facts.st_ino}, 'Source directory identity changed')
                archive = path / 'evidence/media-build-logs'
                for logs in source.glob('desktop/vendor/media-tools/.build-*/logs'):
                    require(logs.resolve().is_relative_to(source), 'Unsafe build log path')
                    inventory(logs)
                    destination = archive / logs.parent.name
                    if not destination.exists():
                        shutil.copytree(logs, destination)
            directories = owner_record(path).get('directories', {})
            removals = [remove_owned(path, name) for name in sorted(directories, key=lambda n: n.count('/'), reverse=True)
                        if name != 'release-assets' or not item['retain_assets']]
            complete = all(record['removed'] for record in removals)
            results.append({'root': str(path), 'complete': complete, 'removals': removals})
            if done:
                done['cleanup_complete'] = complete
                atomic_json(path / 'published-manifest.json', done)
        except (ReleaseError, OSError, ValueError, KeyError) as exc:
            results.append({'root': str(path), 'complete': False, 'reason': str(exc)})
    cache = root.parent / 'download-cache'
    if cache.exists():
        try:
            prune_media_cache(cache, cache_keep_versions(plan))
            cache_report = limit_download_cache(cache)
        except (ReleaseError, OSError, ValueError) as exc:
            cache_report = {'reason': str(exc)}
    else:
        cache_report = {}
    report = {**plan, 'before': before, 'after': space_snapshot(root), 'results': results,
              'resource_issues': recovery, 'cache': cache_report,
              'cleanup_complete': str(root) not in plan['preserved_unresolved'] and not recovery and all(r['complete'] for r in results) and 'reason' not in cache_report,
              'accounting': 'Logical file sizes and observed volume free space; APFS snapshots/clones mean these are not guaranteed physically reclaimed bytes',
              'retry': f'python3.11 scripts/release.py cleanup --root {root} --apply'}
    if (root / 'published-manifest.json').is_file():
        done = published_record(root)
        done['cleanup_complete'] = report['cleanup_complete']
        atomic_json(root / 'published-manifest.json', done)
    atomic_json(root / 'cleanup-report.json', report)
    print(json.dumps(report, indent=2))
    return report


def cleanup_owned(root, manifest):
    require(owner_record(root)['id'] == manifest['owner_id'], 'Cleanup ownership mismatch')
    report = cleanup(root, apply=True)
    require(report['cleanup_complete'], 'Published, cleanup incomplete; see cleanup-report.json and its retry command')


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
    retain_formal_assets(root, done)
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
    clean = commands.add_parser('cleanup', help='List owned cleanup targets; --apply only resumes cleanup')
    clean.add_argument('--root', type=Path, required=True)
    clean.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        common = Path(git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        with exclusive_lock(common):
            if args.command == 'cleanup':
                report = cleanup(args.root, args.apply)
                if args.apply and not report['cleanup_complete']:
                    return 1
            else:
                (candidate if args.command == "candidate" else publish)(args)
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        # No raw command output: it may contain signed URLs or credentials.
        message = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print(f"Release stopped; preserve the working directory. {message}", file=sys.stderr)
        if args.command == "publish":
            state = args.candidate.parent / "progress.json"
            if state.is_file() and read_json(state).get("publish_intent"):
                print("Release may already be public; verification/cleanup is incomplete. Do not rebuild, delete or re-publish it.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
