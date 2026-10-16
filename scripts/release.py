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
    def __init__(self, message, *, category="input-or-content", exit_code=None):
        super().__init__(message)
        self.category = category
        self.exit_code = exit_code


def safe_diagnostic(text, env=None):
    for key, value in (env or os.environ).items():
        if value and re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL", key, re.I):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?im)(authorization|proxy-authorization|cookie|set-cookie)\s*:[^\r\n]*", r"\1: [REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(https?://[^\s?\#]+)[?\#][^\s]*", r"\1?[REDACTED]", text)



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
        failure = None
        try:
            remember_process(root, process, cwd)
            stdout, stderr = process.communicate(input_text, timeout=timeout)
            result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            failure = exc
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            forget_process(root, process.pid)
            if failure is not None:
                failure.exit_code = process.returncode
                failure.category = "timeout" if isinstance(failure, subprocess.TimeoutExpired) else "interrupted"
    else:
        result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, input=input_text,
                                text=True, capture_output=True, timeout=timeout)
    diagnostic = safe_diagnostic(result.stdout + result.stderr, env)
    if log:
        target = Path(log)
        if target.exists():
            target = target.with_name(target.stem + "-" + uuid.uuid4().hex + target.suffix)
        target.write_text(diagnostic)
    if result.returncode != 0:
        raise ReleaseError(f"{Path(str(args[0])).name} failed (exit {result.returncode}): {diagnostic[-2000:]}",
                           category="external-command", exit_code=result.returncode)
    return result.stdout or result.stderr


def git(repo, *args):
    return run(["git", "-C", repo, *args], timeout=120).strip()


def read_updater_config(path):
    # This project's updater config is a flat mapping of unquoted identifier values.
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):[ \t]+([\w.-]+)[ \t]*", line)
        require(match and match[1] not in result, "Unsupported or duplicate updater configuration field")
        result[match[1]] = match[2]
    require(set(result) == {"provider", "owner", "repo", "updaterCacheDirName"},
            "Missing or unexpected updater configuration fields")
    return result


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
    update = read_updater_config(repo / "desktop/build/app-update.yml")
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


def signature_facts(details, bundle_id=None):
    def field(name):
        match = re.search(rf"^{name}=(.+)$", details, re.M)
        require(match, f"Missing codesign {name}")
        return match[1]

    require("Authority=Developer ID Application:" in details and field("Timestamp"), "Developer ID / trusted timestamp absent")
    cdhash = field("CDHash")
    require(re.fullmatch(r"[0-9a-f]{40}", cdhash), "Invalid CDHash")
    facts = {"cdhash": cdhash, "timestamp": field("Timestamp")}
    if bundle_id is not None:
        flags = re.search(r"\bflags=0x([0-9a-fA-F]+)", details)
        require(flags and int(flags[1], 16) & 0x10000, "Hardened Runtime is absent")
        require(field("Identifier") == bundle_id, "Signed Bundle ID mismatch")
        facts["hardened_runtime"] = True
    return facts


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


def owner_record(root, *, repo=None):
    root = canonical_path(root)
    data = read_json(root / 'owner.json')
    require(data.get('format') == 1 and data.get('repo') == str((ROOT if repo is None else repo).resolve()) and data.get('id'), 'Directory ownership mismatch')
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


def check_packages(root, directory, info, source, label, signing_identity):
    evidence = root / "evidence"
    app = directory / "mac-arm64/Venus.app"
    facts = app_facts(app, info, evidence, label)
    check_packaged_privacy(app)
    require(read_updater_config(app / "Contents/Resources/app-update.yml") == read_updater_config(source / "desktop/build/app-update.yml"), "Packaged updater configuration differs")
    run([source / ".venv/bin/python", source / "scripts/ci/assert_backend_bundle.py", "--bundle", app / "Contents/Resources/backend"], log=evidence / f"{label}-backend-bundle.log")
    files = release_assets(directory, info["version"])
    dmg = directory / files["dmg"]
    run(["codesign", "--verify", "--strict", "--verbose=2", "-R",
         f'=certificate leaf = H"{signing_identity}"', app, dmg], log=evidence / f"{label}-container-signature.log")
    signature_facts(run(["codesign", "-d", "--verbose=4", dmg], log=evidence / f"{label}-dmg-codesign.log"))
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


def input_file(path):
    path = canonical_path(path)
    before = path.stat()
    require(stat.S_ISREG(before.st_mode), "Previous input must be a regular file")
    digest = sha256(path)
    after = path.stat()
    def identity(st):
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    require(identity(before) == identity(after), "Previous input changed while reading")
    return {"device": after.st_dev, "inode": after.st_ino, "size": after.st_size,
            "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns, "sha256": digest}


def previous_input(github, previous_tag, published_manifest=None):
    release = github_release(github, previous_tag)
    require(release and not release["draft"] and not release["prerelease"] and
            release["tag_name"] == previous_tag, "Previous stable release not found")
    name = asset_names(previous_tag[1:])["zip"]
    files = [a for a in release["assets"] if a["name"].endswith("-arm64-mac.zip")]
    require(len(files) == 1 and files[0]["name"] == name, "Previous ZIP is ambiguous or misnamed")
    asset = files[0]
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""), "Previous ZIP requires a SHA-256 digest")
    source = git(ROOT, "rev-parse", f"{previous_tag}^{{commit}}")
    tag_object = git(ROOT, "rev-parse", f"refs/tags/{previous_tag}")
    refs = dict(line.split()[::-1] for line in git(ROOT, "ls-remote", "origin",
                f"refs/tags/{previous_tag}", f"refs/tags/{previous_tag}^{{}}").splitlines())
    require(refs.get(f"refs/tags/{previous_tag}") == tag_object and
            refs.get(f"refs/tags/{previous_tag}^{{}}", tag_object) == source, "Previous remote tag changed")
    origin = {"mode": "published" if published_manifest is not None else "network", "github": github,
              "tag": previous_tag, "tag_object": tag_object, "source": source, "release_id": release["id"],
              "asset_id": asset["id"], "name": name, "size": asset["size"], "sha256": asset["digest"][7:]}
    archive = None
    if published_manifest is not None:
        path = canonical_path(published_manifest)
        require(path.name == "published-manifest.json", "Expected published-manifest.json")
        root = path.parent
        records = {name: input_file(root / name) for name in
                   ("owner.json", "candidate-manifest.json", "final-manifest.json", "published-manifest.json")}
        owner = read_json(root / "owner.json")
        repo = canonical_path(Path(owner["repo"]))
        # Worktrees share provenance, but deletion still requires the exact owner and directory identity.
        require(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir") ==
                git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"), "Previous repository mismatch")
        done = published_record(root, repo=repo)
        candidate = read_json(root / "candidate-manifest.json")
        final = read_json(root / "final-manifest.json")
        require(done["github"] == candidate["github"] == github and done["tag"] == previous_tag and
                candidate["version"] == previous_tag[1:] and done["tag_object"] == tag_object and
                done["source"] == final["source"] == source and done["release_id"] == release["id"],
                "Previous published identity mismatch")
        require(final["candidate_sha256"] == records["candidate-manifest.json"]["sha256"] and
                final["assets"] == done["assets"], "Previous final manifest mismatch")
        expected = done["assets"][name]
        require(expected["size"] == origin["size"] and expected["sha256"] == origin["sha256"],
                "Previous published ZIP differs from remote")
        archive = root / "release-assets" / name
        records[str(archive.relative_to(root))] = input_file(archive)
        require(records[str(archive.relative_to(root))]["size"] == origin["size"] and
                records[str(archive.relative_to(root))]["sha256"] == origin["sha256"], "Previous local ZIP differs from published bytes")
        require(all(input_file(root / name) == facts for name, facts in records.items()), "Previous input changed during validation")
        origin.update(published_manifest=str(path), manifest_sha256=records[path.name]["sha256"],
                      files=records, final_signature=final["signature"])
    return origin, asset, archive


def previous_app(root, github, previous_tag, info, published_manifest=None):
    origin, asset, original = previous_input(github, previous_tag, published_manifest)
    previous = owned_directory(root, "previous")
    archive = previous / origin["name"]
    if original is None:
        download(asset["browser_download_url"], archive)
    else:
        with original.open("rb") as source, archive.open("xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        old_root = Path(origin["published_manifest"]).parent
        require(all(input_file(old_root / name) == facts for name, facts in origin["files"].items()),
                "Previous input changed during copy")
    copied = input_file(archive)
    require(copied["size"] == origin["size"] and copied["sha256"] == origin["sha256"], "Previous ZIP bytes mismatch")
    extract_zip(archive, previous / "unpacked")
    app = previous / "unpacked/Venus.app"
    signature = app_facts(app, {**info, "version": previous_tag[1:]}, root / "evidence", "previous")
    if original is not None:
        require(signature == origin["final_signature"], "Previous final signature mismatch")
    provider = read_updater_config(app / "Contents/Resources/app-update.yml")
    require(provider["provider"] == "github" and f"{provider['owner']}/{provider['repo']}" == github,
            "Previous Provider differs from repository")
    atomic_json(root / "evidence/previous-input.json", {"input": origin, "signature": signature, "provider": provider})
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
    name = "public-provider" if public else "candidate-provider"
    state_path = root / "progress.json" if public else root / "evidence/candidate-provider.json"
    progress = read_json(state_path) if state_path.exists() else {}
    expected = directory / asset_names(info["version"])["zip"]
    inputs = {"owner_id": owner_record(root)["id"], "version": info["version"], "github": info["github"],
              "previous": inventory(root / "previous"), "app": inventory(app),
              "config_sha256": sha256(app / "Contents/Resources/app-update.yml"),
              "asar_sha256": sha256(app / "Contents/Resources/app.asar"),
              "probe_sha256": sha256(source / "scripts/release-smoke.cjs"),
              "expected": {"name": expected.name, "size": expected.stat().st_size, "sha256": sha256(expected), "sha512": sha512(expected)},
              "metadata_sha256": sha256(directory / "latest-mac.yml")}
    if public:
        inputs["context"] = progress["inputs"]
        inputs["remote"] = {key: {k: v for k, v in record["inputs"].items() if k != "digest"} for key, record in progress["checks"].items() if key.startswith("anonymous:")}
        stage(root, progress, name)
    record = progress.get("provider")
    if record:
        require(record["inputs"] == inputs, "Provider input identity changed")
    else:
        identifier = uuid.uuid4().hex
        record = {"id": identifier, "scope": name + "-" + identifier, "inputs": inputs}
        progress["provider"] = record
        atomic_json(state_path, progress)
    result = saved_result(root, record, inputs)
    if not result:
        scope = root / record["scope"]
        if scope.exists():
            checked_scope(root, record["scope"])
        else:
            require(not record.get("result"), "Provider completion evidence missing")
            owned_directory(root, record["scope"])
        input_path = canonical_path(root / "evidence" / (record["id"] + ".input.json"))
        if input_path.exists():
            require(read_json(input_path) == inputs, "Provider invocation input changed")
        else:
            atomic_json(input_path, inputs)
        args = ["node", source / "scripts/release-smoke.cjs", "provider", app, source, scope, info["version"]]
        env = isolated_env(scope)
        log = root / "evidence" / (record["id"] + "-" + uuid.uuid4().hex + ".log")
        if public:
            run([*args, "github", input_path, result_path(root, record)], env=env, log=log)
        else:
            with asset_server(directory) as url:
                run([*args, url, input_path, result_path(root, record)], env=env, log=log)
        result = saved_result(root, record, inputs)
        require(result is not None, "Provider completion evidence missing")
    validate_provider_result(root, record, result)
    record["result"] = {"path": str(result_path(root, record).relative_to(root)), "sha256": sha256(result_path(root, record))}
    atomic_json(state_path, progress)
    print(f"Provider complete: {result['completed_at']}; process exit: {result['exit_code']}", flush=True)
    if (root / record["scope"]).exists():
        record["cleanup"] = remove_owned(root, record["scope"])
        atomic_json(state_path, progress)
    return result["result"]


def validate_provider_result(root, record, saved):
    inputs = record["inputs"]
    result = saved["result"]
    expected = inputs["expected"]
    require(result["version"] == inputs["version"] and all(result[k] == expected[k] for k in ("size", "sha256", "sha512"))
            and result["transport"] == "node-http" and result["native_installer_executed"] is False,
            "Provider completion content differs")
    path = result_path(root, record)
    for suffix, key in ((".discovery.json", "discovery_sha256"), (".extracted.json", "extraction_sha256")):
        evidence = canonical_path(Path(str(path) + suffix))
        require(evidence.is_file() and sha256(evidence) == result[key], "Provider supporting evidence changed")
    discovery = read_json(Path(str(path) + ".discovery.json"))
    require(discovery["inputs"] == inputs and discovery["version"] == inputs["version"]
            and discovery["updater_version"] == result["updater_version"]
            and discovery["previous_version"] == result["previous_version"]
            and discovery["provider"] == result["provider"]
            and discovery["file"]["size"] == expected["size"] and discovery["file"]["sha512"] == expected["sha512"]
            and discovery["file"]["name"] == expected["name"], "Provider discovery differs")


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
        origin, _, _ = previous_input(info["github"], args.previous_tag, args.previous_published_manifest)
        require(read_json(output / "evidence/previous-input.json")["input"] == origin, "Frozen previous input changed")
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
    previous = previous_app(output, info["github"], args.previous_tag, info, args.previous_published_manifest)
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
    sign_env = {**env, "HOME": str(Path.home()), "CSC_NAME": matches[0][0], "CSC_IDENTITY_AUTO_DISCOVERY": "true"}
    directory = owned_directory(output, "candidate")
    run([source / "desktop/node_modules/.bin/electron-builder", "--mac", "--arm64", "--publish", "never",
         "-c.mac.notarize=false", "-c.forceCodeSigning=true", f"-c.directories.output={directory}"], cwd=source / "desktop", env=sign_env, log=evidence / "electron-builder.log", timeout=14400)
    media = source / 'desktop/vendor/media-tools/darwin-arm64'
    media_identity = read_json(media / 'build-manifest.json')
    shutil.copy2(media / 'media-sources.tar.gz', directory / f"Venus-{info['version']}-media-sources.tar.gz")
    update_metadata(directory, info["version"], source, evidence)
    signature = check_packages(output, directory, info, source, "candidate", matches[0][0])
    app = directory / "mac-arm64/Venus.app"
    backend = backend_smoke(output, app)
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
    atomic_json(output / "progress.json", {"format": 2, "execution": execution_identity(), "candidate_sha256": sha256(output / "candidate-manifest.json")})
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



def execution_identity():
    return {"release_sha256": sha256(ROOT / "scripts/release.py"),
            "probe_sha256": sha256(ROOT / "scripts/release-smoke.cjs"),
            "python": sys.version, "executable": str(Path(sys.executable).resolve())}


def stage(root, progress, name):
    progress["active"] = {"stage": name, "started_at": datetime.now(UTC).isoformat(), "exit_code": None}
    atomic_json(root / "progress.json", progress)
    print(f"Stage: {name}; evidence: {root / 'evidence'}", flush=True)


def checked_scope(root, name):
    target = canonical_path(root / name)
    require(target != root and target.is_relative_to(root), "Scope escapes release root")
    owner = owner_record(root)
    identity = owner.get("directories", {}).get(name)
    require(identity is not None and target.is_dir(), "Missing owned transfer directory")
    facts = target.stat()
    require(identity == {"device": facts.st_dev, "inode": facts.st_ino}, "Transfer directory identity changed")
    inventory(target)
    require(not any(os.path.ismount(p) for p in (target, *target.rglob('*'))), "Mounted transfer directory")
    occupied = subprocess.run(["lsof", "-t", "+D", str(target)], capture_output=True, text=True)
    require(occupied.returncode == 1 and not occupied.stdout.strip() and not occupied.stderr.strip(), "Transfer directory occupied or check failed")
    return target


def result_path(root, attempt):
    require(re.fullmatch(r"[0-9a-f]{32}", attempt["id"]), "Invalid evidence identity")
    return canonical_path(root / "evidence" / (attempt["id"] + ".complete.json"))


def saved_result(root, attempt, inputs):
    path = result_path(root, attempt)
    reference = attempt.get("result")
    if reference:
        require(reference["path"] == str(path.relative_to(root)), "Completion evidence path changed")
        require(not path.exists() or sha256(path) == reference["sha256"], "Completion evidence changed")
    if not path.exists():
        return None
    result = read_json(path)
    require(result["inputs"] == inputs and result.get("completed_at"), "Completion input identity changed")
    return result


def save_result(root, progress, attempt, inputs, result, *, recovered=False):
    path = result_path(root, attempt)
    if not path.exists():
        atomic_json(path, {"inputs": inputs, "completed_at": datetime.now(UTC).isoformat(),
                           "result": result, "exit_code": None if recovered else 0,
                           "recovery": "local-content-check" if recovered else "download"})
    attempt["result"] = {"path": str(path.relative_to(root)), "sha256": sha256(path)}
    atomic_json(root / "progress.json", progress)
    return read_json(path)


def remote_asset_inputs(info, remote, name, expected, progress):
    marker = f"<!-- release-candidate:{progress['inputs']['owner_id']} source:{progress['inputs']['source']} -->"
    require(remote["id"] == progress["release_id"] and remote["tag_name"] == "v" + info["version"]
            and remote["target_commitish"] == progress["inputs"]["source"] and not remote["prerelease"]
            and marker in remote.get("body", ""), "Remote release identity changed")
    matches = [a for a in remote["assets"] if a["name"] == name]
    require(len(matches) == 1, "Remote asset missing or duplicated: " + name)
    asset = matches[0]
    require(asset.get("id") and asset.get("updated_at") and asset.get("size") == expected["size"], "Remote asset metadata incomplete or changed")
    digest = asset.get("digest")
    require(digest is None or digest == "sha256:" + expected["sha256"], "Remote asset digest changed: " + name)
    return {"release_id": remote["id"], "tag_object": progress["tag_object"], "github": info["github"],
            "tag": "v" + info["version"], "asset_id": asset["id"], "name": name, "size": asset["size"],
            "digest": digest, "updated_at": asset["updated_at"], "expected": expected}


def check_remote_asset(root, info, remote, name, expected, progress, *, public=False):
    key = ("anonymous:" if public else "authenticated:") + name
    stage(root, progress, key)
    inputs = {"context": {**progress["inputs"], "final_sha256": progress["final_sha256"]}, "access": "anonymous" if public else "authenticated",
              **remote_asset_inputs(info, remote, name, expected, progress)}
    record = progress.setdefault("checks", {}).get(key)
    if record:
        before = record["inputs"]
        require({k: v for k, v in before.items() if k != "digest"} ==
                {k: v for k, v in inputs.items() if k != "digest"}, "Asset check identity changed: " + name)
        require(not before.get("digest") or not inputs["digest"] or before["digest"] == inputs["digest"], "Remote digest changed")
        attempt = record["attempts"][-1]
        saved = saved_result(root, attempt, before)
        if saved:
            require(saved["result"] == {"size": expected["size"], "sha256": expected["sha256"]}, "Asset result differs from final")
            if inputs["digest"] and before.get("digest"):
                save_result(root, progress, attempt, before, saved["result"])
                print(f"Reused {key}: {saved['completed_at']}", flush=True)
                return attempt["result"]
        elif (root / attempt["scope"]).exists():
            scope = checked_scope(root, attempt["scope"])
            origin = canonical_path(scope / "origin.json")
            if origin.exists():
                require(read_json(origin) == before, "Download origin changed")
                file = canonical_path(scope / name)
                if file.is_file() and file.stat().st_size == expected["size"] and sha256(file) == expected["sha256"]:
                    saved = save_result(root, progress, attempt, before, {"size": file.stat().st_size, "sha256": sha256(file)}, recovered=True)
                    if inputs["digest"] and before.get("digest"):
                        return attempt["result"]
        if (root / attempt["scope"]).exists():
            checked_scope(root, attempt["scope"])
            require(remove_owned(root, attempt["scope"])["removed"], "Previous download cleanup incomplete")
        record["inputs"] = inputs
    else:
        record = {"inputs": inputs, "attempts": []}
        progress["checks"][key] = record
    identifier = uuid.uuid4().hex
    attempt = {"id": identifier, "scope": "asset-check-" + identifier, "exit_code": None}
    record["attempts"].append(attempt)
    atomic_json(root / "progress.json", progress)
    scratch = owned_directory(root, attempt["scope"])
    atomic_json(scratch / "origin.json", inputs)
    target = scratch / name
    if public:
        asset = next(a for a in remote["assets"] if a["name"] == name)
        download(asset["browser_download_url"], target)
    else:
        run(["gh", "release", "download", inputs["tag"], "--repo", info["github"], "--pattern", name, "--dir", scratch],
            env={**os.environ, "VENUS_RELEASE_ROOT": str(root)})
    require(target.is_file() and not target.is_symlink() and target.stat().st_size == expected["size"]
            and sha256(target) == expected["sha256"], "Remote asset differs: " + name)
    save_result(root, progress, attempt, inputs, {"size": target.stat().st_size, "sha256": sha256(target)})
    attempt["cleanup"] = remove_owned(root, attempt["scope"])
    atomic_json(root / "progress.json", progress)
    return attempt["result"]

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
            stage(root, progress, "upload:" + name)
            progress["upload_intent"] = name
            atomic_json(state_path, progress)
            # A lost response is reconciled by the next invocation's remote asset inventory.
            run(["gh", "release", "upload", tag, root / "finalize" / name, "--repo", info["github"]])
        current = github_release(info["github"], tag)
        require(current and current["id"] == release["id"], "Release changed during upload")
        check_remote_asset(root, info, current, name, facts, progress)


def published_record(root, *, repo=None):
    owner = owner_record(root, repo=repo)
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
        checked_scope(root, 'release-assets')
    for name, record in done['assets'].items():
        require(Path(name).name == name, 'Unsafe formal asset name')
        file = canonical_path(target / name)
        if not file.exists():
            original = canonical_path(root / 'finalize' / name)
            require(sha256(original) == record['sha256'], 'Final asset changed')
            temporary = canonical_path(target / ("." + name + ".copy"))
            if not temporary.exists() or temporary.stat().st_size != record["size"] or sha256(temporary) != record["sha256"]:
                with original.open("rb") as incoming, temporary.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
            require(temporary.stat().st_size == record["size"] and sha256(temporary) == record["sha256"], "Formal copy differs")
            os.replace(temporary, file)
            fd = os.open(target, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
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
    owner = owner_record(root)
    initial_state = canonical_path(root / "progress.json")
    if initial_state.exists():
        initial = read_json(initial_state)
        if initial.get("format") == 2:
            if initial.get("active"):
                atomic_json(root / "evidence" / (uuid.uuid4().hex + ".interrupted.json"),
                            {**initial["active"], "category": "previous-result-unknown", "exit_code": None})
            stage(root, initial, "preflight")
    published_path = root / "published-manifest.json"
    done = published_record(root) if published_path.exists() else None
    manifest = read_json(manifest_path) if done else verify_candidate(manifest_path)
    require(owner["id"] == manifest["owner_id"] and owner["source"] == manifest["source"], "Candidate owner changed")
    info = metadata(ROOT)
    require(all(manifest[k] == info[k] for k in info), "Source release metadata changed")
    require(args.confirm_version == "v" + info["version"], "Explicit version confirmation mismatch")
    check_source(ROOT, manifest["source"], info["github"])
    require(tools_snapshot() == manifest["tools"], "Tool environment changed since candidate")
    verify_acceptance(args.acceptance, manifest_path, manifest["source"])
    state_path = canonical_path(root / "progress.json")
    progress = read_json(state_path)
    require(progress.get("format") == 2, "Historical progress lacks resume evidence; preserve it for manual reconciliation")
    require(progress.get("execution") == execution_identity(), "Release execution code or runtime changed")
    require(progress["candidate_sha256"] == sha256(manifest_path), "Progress belongs to another candidate")
    inputs = {"candidate_sha256": sha256(manifest_path), "acceptance_sha256": sha256(args.acceptance),
              "source": manifest["source"], "owner_id": manifest["owner_id"], "version": info["version"],
              "tools": manifest["tools"], "execution": progress["execution"]}
    require(progress.get("inputs", inputs) == inputs, "Publish inputs changed")
    progress["inputs"] = inputs
    print("Completed checks: " + ", ".join(key for key, value in progress.get("checks", {}).items() if value["attempts"][-1].get("result")), flush=True)
    pending = [access + ":" + name for access in ("authenticated", "anonymous") for name in asset_names(info["version"]).values()
               if not progress.get("checks", {}).get(access + ":" + name, {}).get("attempts", [{}])[-1].get("result")]
    print("Missing check evidence: " + ", ".join(pending) + f"; recorded failures: {len(progress.get('errors', []))}", flush=True)
    print(f"Public: {bool(progress.get('public'))}; Provider: {bool(progress.get('provider', {}).get('result'))}; "
          f"Published record: {bool(done)}; cleanup: {bool(done and done.get('cleanup_complete'))}", flush=True)
    require(not recover_resources(root), "Owned resources require recovery before continuing")
    if done:
        require(done.get("inputs") == inputs and done.get("final_sha256") == sha256(root / "final-manifest.json"), "Published evidence identity changed")
        require(set(done["checks"]) == set(progress["checks"])
                and done.get("provider_evidence") == progress["provider"].get("result"), "Published evidence references changed")
        for key, reference in done["checks"].items():
            previous = [attempt for attempt in progress["checks"][key]["attempts"] if attempt.get("result") == reference]
            require(len(previous) == 1, "Published check reference disappeared")
            path = result_path(root, previous[0])
            require(reference["path"] == str(path.relative_to(root))
                    and (not path.exists() or sha256(path) == reference["sha256"]), "Published check evidence changed")
        final = read_json(root / "final-manifest.json")
        require(final["assets"] == done["assets"], "Published final assets changed")
        for folder, expected in (("candidate", manifest["candidate_inventory"]), ("previous", manifest["previous_inventory"]),
                                 ("finalize", final["inventory"])):
            if (root / folder).exists():
                verify_inventory(root / folder, expected)
        verify_inventory(root / "release-assets", {name: {"size": record["size"], "sha256": record["sha256"],
            "mode": stat.S_IMODE((root / "release-assets" / name).stat().st_mode)} for name, record in done["assets"].items()})
        tag = done["tag"]
        require(git(ROOT, "rev-parse", f"refs/tags/{tag}") == done["tag_object"] == progress["tag_object"], "Published tag changed")
    else:
        stage(root, progress, "tag-and-notarization")
        tag = ensure_tag(ROOT, info, manifest, progress, state_path)
        assets = release_assets(root / "candidate", info["version"])
        for kind in ("zip", "dmg"):
            stage(root, progress, "notarization:" + kind)
            notarize(root / "candidate" / assets[kind], manifest["notary_profile"], progress, state_path)
        stage(root, progress, "final")
        final = finalize(root, info, manifest)
    digest = sha256(root / "final-manifest.json")
    require(progress.get("final_sha256", digest) == digest, "Final manifest changed")
    progress["final_sha256"] = digest
    atomic_json(state_path, progress)
    check_source(ROOT, manifest["source"], info["github"])
    stage(root, progress, "remote-tag")
    remote_tag = git(ROOT, "ls-remote", "origin", f"refs/tags/{tag}")
    if remote_tag:
        require(remote_tag.split()[0] == progress["tag_object"], "Remote tag object differs")
    else:
        require(not done, "Published remote tag disappeared")
        git(ROOT, "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    require(git(ROOT, "ls-remote", "origin", f"refs/tags/{tag}").split()[0] == progress["tag_object"], "Tag push verification failed")
    stage(root, progress, "release-identity")
    if done:
        remote = github_release(info["github"], tag)
        require(remote and remote["id"] == done["release_id"] and not remote["draft"], "Published release missing or changed")
        marker = f"<!-- release-candidate:{manifest['owner_id']} source:{manifest['source']} -->"
        require(marker in remote.get("body", "") and remote["tag_name"] == tag
                and remote["target_commitish"] == manifest["source"] and not remote["prerelease"], "Published release identity changed")
    else:
        remote = ensure_release(info, manifest, progress, state_path)
        upload_assets(root, info, final, remote, progress, state_path)
        if remote["draft"]:
            stage(root, progress, "publish-release")
            check_source(ROOT, manifest["source"], info["github"])
            progress["publish_intent"] = True
            atomic_json(state_path, progress)
            run(["gh", "api", "--method", "PATCH", f"repos/{info['github']}/releases/{remote['id']}", "--input", "-"], input_text='{"draft":false,"make_latest":"true"}')
        remote = github_release(info["github"], tag)
        require(remote and not remote["draft"] and remote["id"] == progress["release_id"], "Public release verification failed")
        progress["public"] = True
        atomic_json(state_path, progress)
    require(len(remote["assets"]) == 5 and {a["name"] for a in remote["assets"]} == set(final["assets"]), "Public asset set differs")
    for name, facts in final["assets"].items():
        if done:
            # Revalidate both evidence identities; authentication never substitutes for public access.
            check_remote_asset(root, info, remote, name, facts, progress)
        check_remote_asset(root, info, remote, name, facts, progress, public=True)
    if done:
        record = progress["provider"]
        saved = saved_result(root, record, record["inputs"])
        require(saved is not None, "Published Provider completion missing")
        validate_provider_result(root, record, saved)
        provider = saved["result"]
        print(f"Reused Provider: {saved['completed_at']}; original process exit: {saved['exit_code']}", flush=True)
    else:
        provider = provider_smoke(root, root / "source", root / "previous/unpacked/Venus.app", root / "finalize", info, public=True)
        progress = read_json(state_path)
    temporary_scopes = [attempt["scope"] for check in progress["checks"].values() for attempt in check["attempts"]]
    temporary_scopes.append(progress["provider"]["scope"])
    cleanup_complete = bool(done and done.get("cleanup_complete") and not any((root / name).exists() for name in temporary_scopes))
    complete = {"source": manifest["source"], "tag": tag, "tag_object": progress["tag_object"], "github": info["github"],
                "release_id": remote["id"], "url": remote["html_url"], "assets": final["assets"], "provider": provider,
                "candidate_sha256": sha256(manifest_path), "acceptance_sha256": sha256(args.acceptance),
                "inputs": inputs, "final_sha256": digest,
                "checks": {k: v["attempts"][-1]["result"] for k, v in progress["checks"].items()},
                "provider_evidence": progress["provider"]["result"], "cleanup_complete": cleanup_complete}
    if not done:
        stage(root, progress, "retain-formal-assets")
        retain_formal_assets(root, complete)
    atomic_json(published_path, complete)
    if not done:
        stage(root, progress, "cleanup")
        cleanup_owned(root, manifest)
        complete["cleanup_complete"] = True
        atomic_json(published_path, complete)
    require(complete["cleanup_complete"], f"Published and verified; cleanup incomplete. Run: python3.11 scripts/release.py cleanup --root {root} --apply")
    progress.pop("active", None)
    atomic_json(state_path, progress)
    print("Published and verified; cleanup complete: " + remote["html_url"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("candidate", help="Build once; never tag, notarize or publish")
    build.add_argument("--source", required=True)
    build.add_argument("--previous-tag", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--previous-published-manifest", type=Path)
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
        message = safe_diagnostic(str(exc)) if isinstance(exc, ReleaseError) else type(exc).__name__
        print(f"Release stopped; preserve the working directory. {message}", file=sys.stderr)
        if args.command == "publish":
            try:
                root = canonical_path(args.candidate.parent)
                owner_record(root)
                state = canonical_path(root / "progress.json")
                progress = read_json(state)
                if progress.get("format") == 2:
                    error = {**progress.get("active", {"stage": "preflight"}),
                             "failed_at": datetime.now(UTC).isoformat(), "message": message,
                             "category": getattr(exc, "category", "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "unknown"),
                             "exit_code": getattr(exc, "exit_code", None)}
                    target = root / "evidence" / (uuid.uuid4().hex + ".error.json")
                    atomic_json(target, error)
                    progress.setdefault("errors", []).append({"path": str(target.relative_to(root)), "sha256": sha256(target)})
                    progress.pop("active", None)
                    atomic_json(state, progress)
                if (root / "published-manifest.json").exists():
                    print(f"Published record retained. If only cleanup remains: python3.11 scripts/release.py cleanup --root {root} --apply", file=sys.stderr)
                elif progress.get("publish_intent"):
                    print("Release may already be public; verification is incomplete. Preserve its evidence.", file=sys.stderr)
            except (ReleaseError, OSError, ValueError, KeyError):
                print("Could not safely save diagnostics; preserve existing evidence.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
