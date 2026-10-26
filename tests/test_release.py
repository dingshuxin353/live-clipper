"""Release safety checks use isolated files and injected command failures, never Apple writes."""

import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
REAL_RUN = release.run


def test_explicit_process_owner_preserves_io_fast_exit_and_safe_diagnostics(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    scope = release.owned_directory(root, 'probe')
    environment = release.isolated_env(scope)
    environment.update(API_KEY='test-only-secret', VENUS_RELEASE_ROOT=str(tmp_path / 'unknown'))
    log = root / 'evidence/output.log'
    script = 'import os,sys; print(sys.stdin.read()); print(os.environ["API_KEY"], file=sys.stderr)'
    assert release.run([sys.executable, '-c', script], cwd=repo, env=environment,
        release_root=root, input_text='original input', log=log) == 'original input\n'
    assert 'test-only-secret' not in log.read_text() and '[REDACTED]' in log.read_text()
    for _ in range(5):
        assert release.run(['/usr/bin/true'], cwd=repo, env=environment, release_root=root) == ''
    with pytest.raises(release.ReleaseError) as failed:
        release.run([sys.executable, '-c', 'import sys; print("failure"); sys.exit(7)'],
            env=environment, release_root=root)
    assert failed.value.exit_code == 7 and failed.value.category == 'external-command'
    assert release.resource_state(root) == {'processes': [], 'mounts': []}
    assert not (tmp_path / 'unknown').exists()


@pytest.mark.parametrize('failure', ['interrupt', 'caller', 'ignore-term'])
def test_running_process_scope_finishes_on_interruption_and_exception(tmp_path, monkeypatch, failure):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    scope = release.owned_directory(root, 'probe')
    ignore = 'signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if failure == 'ignore-term' else ''
    script = 'import signal; ' + ignore + 'print("ready", flush=True); signal.pause()'
    error = KeyboardInterrupt if failure == 'interrupt' else RuntimeError
    with pytest.raises(error):
        with release.ReleaseProcesses(root).running([sys.executable, '-c', script], cwd=scope,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            assert select.select([process.stdout], [], [], 5)[0]
            assert process.stdout.readline().strip() == 'ready'
            assert release.resource_state(root)['processes'][0]['pid'] == process.pid
            raise error('caller stopped')
    assert process.returncode == (-signal.SIGKILL if failure == 'ignore-term' else -signal.SIGTERM)
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert release.resource_state(root)['processes'] == []


@pytest.mark.parametrize('boundary', ['registration', 'removal'])
def test_process_record_write_failure_preserves_failure_and_cleans_created_process(tmp_path, monkeypatch, boundary):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    release.atomic_json(root / 'resources.json', {'processes': [], 'mounts': []})
    save = release.atomic_json
    observed = []

    def denied(path, value):
        if Path(path).name == 'resources.json' and bool(value['processes']) == (boundary == 'registration'):
            if value['processes']:
                observed.append(value['processes'][-1]['pid'])
            raise PermissionError('controlled record write failure')
        return save(path, value)

    monkeypatch.setattr(release, 'atomic_json', denied)
    expected = PermissionError if boundary == 'registration' else release.ReleaseError
    with pytest.raises(expected) as failure:
        with release.ReleaseProcesses(root).running([sys.executable, '-c', 'import signal; signal.pause()'],
            stdout=subprocess.PIPE) as process:
            observed.append(process.pid)
            raise release.ReleaseError('original command failed', category='external-command', exit_code=7)
    with pytest.raises(ProcessLookupError):
        os.kill(observed[0], 0)
    records = release.resource_state(root)['processes']
    if boundary == 'registration':
        assert records == []
    else:
        assert failure.value.category == 'external-command' and failure.value.exit_code == 7
        assert 'original command failed' in str(failure.value) and 'cleanup incomplete' in str(failure.value)
        assert isinstance(failure.value.__cause__, release.ReleaseError)
        assert records[0]['pid'] == observed[0]
        monkeypatch.setattr(release, 'atomic_json', save)
        assert release.recover_resources(root) == []
        assert release.resource_state(root)['processes'] == []


@pytest.mark.parametrize('processable', [1, 0])
def test_backend_smoke_uses_real_service_scope_and_releases_port(tmp_path, monkeypatch, processable):
    import socket

    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    app = tmp_path / 'temporary-service.app'
    binaries = app / 'Contents/Resources/bin'
    binaries.mkdir(parents=True)
    for name in ['ffmpeg', 'ffprobe']:
        executable = shutil.which(name)
        assert executable, name + ' is required for the existing synthetic-media check'
        (binaries / name).symlink_to(executable)
    backend = app / 'Contents/Resources/backend/live-clipper-backend'
    backend.parent.mkdir()
    backend.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
port = int(sys.argv[sys.argv.index('--port') + 1])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['LIVE_CLIPPER_WEB_TOKEN']:
            self.send_error(401)
            return
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.dumps({'ok': True, 'processable_files': ''' + str(processable) + '''}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = do_GET
    def log_message(self, *args):
        pass
server = HTTPServer(('127.0.0.1', port), Handler)
Path('observed-service.json').write_text(json.dumps({'pid': os.getpid(), 'port': port}))
server.serve_forever()
''')
    backend.chmod(0o700)
    if processable:
        assert release.backend_smoke(root, app)['scan_preview']['processable_files'] == 1
    else:
        with pytest.raises(release.ReleaseError, match='Synthetic recording scan failed'):
            release.backend_smoke(root, app)
    facts = release.read_json(root / 'backend-smoke/observed-service.json')
    with pytest.raises(ProcessLookupError):
        os.kill(facts['pid'], 0)
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', facts['port'])) != 0
    assert release.resource_state(root)['processes'] == []


@pytest.mark.parametrize('fault', ['missing', 'changed', 'group', 'not-session-leader', 'group-leader-only', 'query', 'empty-query'])
def test_history_recovery_keeps_uncertain_real_process_without_signalling(tmp_path, monkeypatch, fault):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    process = subprocess.Popen([sys.executable, '-c', 'import signal; print("ready", flush=True); signal.pause()'],
        start_new_session=fault not in {'not-session-leader', 'group-leader-only'},
        process_group=0 if fault == 'group-leader-only' else None, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == 'ready'
        release.remember_process(root, process, tmp_path)
        state = release.resource_state(root)
        assert state['processes'][0]['identity'] == subprocess.check_output(
            ['ps', '-p', str(process.pid), '-o', 'lstart=', '-o', 'command='], text=True).strip()
        if fault == 'missing':
            state['processes'][0]['identity'] = None
        elif fault == 'changed':
            state['processes'][0]['identity'] = 'old process'
        elif fault == 'group':
            state['processes'][0]['pgid'] = process.pid + 1
        release.atomic_json(root / 'resources.json', state)
        before = (root / 'resources.json').read_bytes()
        original_run = subprocess.run

        def identity_query(args, **kwargs):
            if fault in {'query', 'empty-query'} and args[0] == 'ps':
                return subprocess.CompletedProcess(args, 2 if fault == 'query' else 0, '', '')
            return original_run(args, **kwargs)

        monkeypatch.setattr(release.subprocess, 'run', identity_query)
        monkeypatch.setattr(release.os, 'killpg', lambda *_: pytest.fail('Uncertain process must not be signalled'))
        assert release.recover_resources(root)
        assert (root / 'resources.json').read_bytes() == before
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(5)
        process.stdout.close()


def test_recovery_after_real_manager_termination_and_repeat_is_safe(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    script = '''import importlib.util, json, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('release', sys.argv[1])
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
release.ROOT = Path(sys.argv[3])
with release.ReleaseProcesses(Path(sys.argv[2])).running(['/bin/sleep', '120']) as child:
    print(json.dumps(release.resource_state(Path(sys.argv[2]))['processes'][0]), flush=True)
    signal.pause()
'''
    manager = subprocess.Popen([sys.executable, '-I', '-B', '-u', '-c', script,
        str(Path(release.__file__).resolve()), str(root), str(repo)], stdout=subprocess.PIPE,
        text=True, start_new_session=True)
    record = None
    real_killpg = os.killpg
    signals = []
    try:
        assert select.select([manager.stdout], [], [], 5)[0]
        record = json.loads(manager.stdout.readline())
        assert release.process_identity(record['pid']) == record['identity']
        manager.kill()
        assert manager.wait(5) == -signal.SIGKILL
        assert release.process_identity(record['pid']) == record['identity']

        def track_signal(pgid, signum):
            signals.append((pgid, signum))
            return real_killpg(pgid, signum)

        monkeypatch.setattr(release.os, 'killpg', track_signal)
        assert release.recover_resources(root) == []
        assert release.process_identity(record['pid']) is None
        finished = (root / 'resources.json').read_bytes()
        assert release.recover_resources(root) == []
        assert (root / 'resources.json').read_bytes() == finished
        assert signals == [(record['pgid'], signal.SIGTERM)]
    finally:
        if manager.poll() is None:
            manager.kill()
            manager.wait(5)
        manager.stdout.close()
        if record and release.process_identity(record['pid']) == record['identity']:
            real_killpg(record['pgid'], signal.SIGKILL)


def test_exited_process_port_is_retained_without_ending_other_listener(tmp_path, monkeypatch):
    import socket

    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    with socket.socket() as other_listener:
        other_listener.bind(('127.0.0.1', 0))
        other_listener.listen()
        port = other_listener.getsockname()[1]
        with pytest.raises(release.ReleaseError, match='port still occupied'):
            with release.ReleaseProcesses(root).running(['/usr/bin/true'], port=port) as process:
                process.wait(5)
        recorded = release.resource_state(root)['processes']
        assert recorded[0]['port'] == port
        monkeypatch.setattr(release.os, 'killpg', lambda *_: pytest.fail('Other listener must not be signalled'))
        assert release.recover_resources(root) == [{'port': port, 'reason': 'port occupied; owner not signalled'}]
        assert release.resource_state(root)['processes'] == recorded
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    assert release.recover_resources(root) == []
    assert release.resource_state(root)['processes'] == []


def test_real_cleanup_preview_and_apply_preserve_unresolved_release(tmp_path):
    parent = tmp_path / 'releases'
    parent.mkdir()
    root = parent / 'release'
    release.create_root(root, release.ROOT, 'a' * 40)
    unknown = parent / 'unknown'
    unknown.mkdir()
    (unknown / 'user-file').write_text('preserve')
    process = subprocess.Popen(['/bin/sleep', '120'], start_new_session=True)
    try:
        release.remember_process(root, process, tmp_path)
        before = (root / 'resources.json').read_bytes()
        env = release.isolated_env(tmp_path / 'cli-environment')
        command = [sys.executable, str(Path(release.__file__).resolve()), 'cleanup', '--root', str(root)]
        preview = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        assert preview.returncode == 0, preview.stderr
        assert (root / 'resources.json').read_bytes() == before and process.poll() is None
        applied = subprocess.run([*command, '--apply'], env=env, text=True, capture_output=True, timeout=20)
        assert applied.returncode == 1, applied.stderr
        assert process.wait(5) == -signal.SIGTERM
        report = release.read_json(root / 'cleanup-report.json')
        assert not report['cleanup_complete'] and str(root) in report['preserved_unresolved']
        assert report['resource_issues'] == []
        assert release.resource_state(root)['processes'] == []
        assert (unknown / 'user-file').read_text() == 'preserve'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)


def test_history_recovery_does_not_escalate_term_to_kill(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    process = subprocess.Popen([sys.executable, '-c',
        'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); signal.pause()'],
        start_new_session=True, stdout=subprocess.PIPE, text=True)
    real_killpg = os.killpg
    signals = []
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == 'ready'
        release.remember_process(root, process, tmp_path)

        def track(pgid, signum):
            signals.append(signum)
            return real_killpg(pgid, signum)

        monkeypatch.setattr(release.os, 'killpg', track)
        assert release.recover_resources(root) == [{'pid': process.pid, 'reason': 'owned process did not exit gracefully'}]
        assert signals == [signal.SIGTERM] and process.poll() is None
        assert release.resource_state(root)['processes'][0]['pid'] == process.pid
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
        process.stdout.close()


def test_only_explicit_validated_owner_controls_registration(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    release.atomic_json(root / 'resources.json', {'processes': [], 'mounts': []})
    script = 'import json,sys; from pathlib import Path; root=Path(sys.stdin.read()); print(len(json.loads((root/"resources.json").read_text())["processes"]))'
    environment = release.isolated_env(tmp_path / 'environment')
    environment['VENUS_RELEASE_ROOT'] = str(root)
    assert release.run([sys.executable, '-c', script], cwd=repo, env=environment, input_text=str(root)).strip() == '0'
    environment['VENUS_RELEASE_ROOT'] = str(tmp_path / 'unknown')
    assert release.run([sys.executable, '-c', script], cwd=repo, env=environment,
        input_text=str(root), release_root=root).strip() == '1'
    assert release.resource_state(root)['processes'] == []
    marker = tmp_path / 'must-not-start'
    with pytest.raises((release.ReleaseError, OSError)):
        release.run([sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'],
            env=environment, release_root=tmp_path / 'unknown')
    assert not marker.exists() and not (tmp_path / 'unknown').exists()


def test_process_scope_removes_only_its_own_record(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    with release.ReleaseProcesses(root).running(['/bin/sleep', '120']) as process:
        state = release.resource_state(root)
        previous = {**state['processes'][0], 'identity': 'previous process identity'}
        state['processes'].append(previous)
        release.atomic_json(root / 'resources.json', state)
    assert process.poll() is not None
    assert release.resource_state(root)['processes'] == [previous]
    assert release.recover_resources(root) == []
    assert release.resource_state(root)['processes'] == []


def test_signature_runtime_is_parsed_from_codesign_flags():
    details = "Identifier=example\nCodeDirectory v=20500 flags=0x10000(runtime)\nCDHash=" + "a" * 40
    details += "\nAuthority=Developer ID Application: Test\nTimestamp=today\n"
    assert release.signature_facts(details, "example")["hardened_runtime"] is True
    with pytest.raises(release.ReleaseError):
        release.signature_facts(details.replace("0x10000(runtime)", "0x0(none)"), "example")


@pytest.mark.parametrize('state', ['signed', 'unsigned', 'no_timestamp'])
def test_packages_require_signed_dmg_before_freezing(tmp_path, monkeypatch, state):
    directory = tmp_path / 'candidate'
    app = directory / 'mac-arm64/Venus.app'
    dmg = directory / 'Venus-1.0.4-arm64.dmg'
    identity = 'A' * 40
    monkeypatch.setattr(release, 'app_facts', lambda *_: {})
    monkeypatch.setattr(release, 'check_packaged_privacy', lambda *_: None)
    monkeypatch.setattr(release, 'read_updater_config', lambda *_: {})
    monkeypatch.setattr(release, 'release_assets', lambda *_: {'dmg': dmg.name})
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[0] != 'codesign':
            return ''
        if '--verify' in args:
            assert args == ['codesign', '--verify', '--strict', '--verbose=2',
                            '-R', f'=certificate leaf = H"{identity}"', app, dmg]
            if state == 'unsigned':
                raise release.ReleaseError('code object is not signed at all')
            return ''
        assert args == ['codesign', '-d', '--verbose=4', dmg]
        details = 'CodeDirectory v=20100 flags=0x0(none)\nCDHash=' + 'b' * 40
        details += '\nAuthority=Developer ID Application: Test\n'
        return details + ('Timestamp=today\n' if state != 'no_timestamp' else '')

    def after_signature(*_):
        raise release.ReleaseError('Reached verified container boundary')

    monkeypatch.setattr(release, 'run', command)
    monkeypatch.setattr(release, 'check_media_source', after_signature)
    expected = {'signed': 'Reached verified container boundary',
                'unsigned': 'not signed at all', 'no_timestamp': 'Timestamp'}[state]
    with pytest.raises(release.ReleaseError, match=expected):
        release.check_packages(tmp_path, directory, {'version': '1.0.4'}, tmp_path / 'source',
                               'candidate', identity)
    assert any('-R' in args for args in calls)
    assert not (tmp_path / 'candidate-manifest.json').exists()


@pytest.mark.parametrize("value", ["1.0", "v1.2.3", "1.2.3-beta", "01.2.3", "../1.2.3"])
def test_version_rejects_ambiguous_names(value):
    with pytest.raises(release.ReleaseError):
        release.version_tuple(value)


def test_inventory_detects_tampering_but_ignores_mtime(tmp_path):
    (tmp_path / "asset.zip").write_bytes(b"signed bytes")
    facts = release.inventory(tmp_path)
    (tmp_path / "asset.zip").touch()
    release.verify_inventory(tmp_path, facts)
    (tmp_path / "asset.zip").write_bytes(b"other bytes")
    with pytest.raises(release.ReleaseError):
        release.verify_inventory(tmp_path, facts)


def test_inventory_rejects_escape_symlinks(tmp_path):
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(release.ReleaseError):
        release.inventory(tmp_path)


def test_owned_root_refuses_unknown_or_alias_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for target in (repo, tmp_path, repo / "release"):
        with pytest.raises(release.ReleaseError):
            release.validate_output(target, repo)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(release.ReleaseError):
        release.create_root(existing, repo, "a" * 40)
    alias = tmp_path / "alias"
    alias.symlink_to(existing)
    with pytest.raises(release.ReleaseError):
        release.validate_output(alias / "child", repo)


def test_lock_is_shared_and_nonblocking(tmp_path):
    with release.exclusive_lock(tmp_path):
        with pytest.raises(release.ReleaseError), release.exclusive_lock(tmp_path):
            pass


def test_acceptance_requires_all_items_and_bound_evidence(tmp_path):
    manifest = tmp_path / "candidate-manifest.json"
    manifest.write_text(json.dumps({"source": "a" * 40}))
    acceptance = release.acceptance_template(manifest, "a" * 40)
    document = tmp_path / "acceptance.json"
    document.write_text(json.dumps(acceptance))
    with pytest.raises(release.ReleaseError):
        release.verify_acceptance(document, manifest, "a" * 40)
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("human observations")
    acceptance.update(reviewer="Independent tester", reviewed_at="2026-09-04T10:00:00Z")
    for item in acceptance["checks"].values():
        item.update(status="passed", evidence=[{"path": "evidence.txt", "sha256": release.sha256(evidence)}])
    document.write_text(json.dumps(acceptance))
    release.verify_acceptance(document, manifest, "a" * 40)
    evidence.write_text("changed")
    with pytest.raises(release.ReleaseError):
        release.verify_acceptance(document, manifest, "a" * 40)


def test_notary_ambiguous_submit_never_retries(tmp_path):
    progress = {}
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        raise release.ReleaseError("connection lost")

    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"asset")
    for _ in range(2):
        with pytest.raises(release.ReleaseError):
            release.notarize(asset, "profile", progress, tmp_path / "progress.json", failed)
    assert len(calls) == 1
    assert progress["notary"]["asset.zip"]["state"] == "submitting"


def test_notary_existing_submission_only_waits(tmp_path):
    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"asset")
    progress = {"notary": {"asset.zip": {"id": "submission", "sha256": release.sha256(asset)}}}
    calls = []

    def accepted(args, **kwargs):
        calls.append(args)
        return '{"id":"submission","status":"Accepted"}'

    release.notarize(asset, "profile", progress, tmp_path / "progress.json", accepted)
    assert len(calls) == 1 and calls[0][2] == "wait"
    release.notarize(asset, "profile", progress, tmp_path / "progress.json", accepted)
    assert len(calls) == 1


def test_source_gate_uses_real_git_and_stops_before_remote_on_dirty_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master", repo], check=True, capture_output=True)
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "Release test")):
        subprocess.run(["git", "-C", repo, "config", key, value], check=True)
    (repo / "file").write_text("source")
    subprocess.run(["git", "-C", repo, "add", "file"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-m", "test"], check=True, capture_output=True)
    head = release.git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("dirty")
    with pytest.raises(release.ReleaseError, match="clean"):
        release.check_source(repo, head, "example/repository")
    with pytest.raises(release.ReleaseError, match="differ"):
        release.check_source(repo, "0" * 40, "example/repository")


def test_source_versions_are_dynamic_and_all_seven_checked(tmp_path):
    repo = Path(__file__).parents[1]
    for name in ("pyproject.toml", "CHANGELOG.md", "desktop/electron-builder.yml", "desktop/build/app-update.yml",
                 "frontend/package.json", "frontend/package-lock.json", "desktop/package.json", "desktop/package-lock.json"):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / name, destination)
    original = release.metadata(tmp_path)
    updater = tmp_path / "desktop/build/app-update.yml"
    original_updater = updater.read_text()
    updater.write_text(original_updater + "unparsed content\n")
    with pytest.raises(release.ReleaseError, match="updater configuration"):
        release.metadata(tmp_path)
    updater.write_text(original_updater)
    lock = tmp_path / "frontend/package-lock.json"
    data = json.loads(lock.read_text())
    data["packages"][""]["version"] = "0.0.0"
    lock.write_text(json.dumps(data))
    assert original["version"] != "0.0.0"
    with pytest.raises(release.ReleaseError, match="Seven"):
        release.metadata(tmp_path)


@pytest.mark.parametrize('change', ['reordered', 'value', 'missing', 'unparsed', 'duplicate', 'extra'])
def test_packaged_updater_compares_complete_configuration(tmp_path, monkeypatch, change):
    source = Path(__file__).parents[1]
    updater = tmp_path / 'candidate/mac-arm64/Venus.app/Contents/Resources/app-update.yml'
    updater.parent.mkdir(parents=True)
    # The signed builder output has the same four values, in this different order.
    text = ('owner: dingshuxin353\nrepo: live-clipper\nprovider: github\n'
            'updaterCacheDirName: live-clipper-desktop-updater\n')
    if change == 'value':
        text = text.replace('live-clipper-desktop-updater', 'different-updater')
    elif change == 'missing':
        text = text.replace('provider: github\n', '')
    elif change in ('unparsed', 'duplicate', 'extra'):
        text += {'unparsed': 'unparsed content\n', 'duplicate': 'provider: github\n',
                 'extra': 'extra: value\n'}[change]
    updater.write_text(text)
    monkeypatch.setattr(release, 'app_facts', lambda *_: {})
    monkeypatch.setattr(release, 'check_packaged_privacy', lambda *_: None)

    def next_package_check(*args, **kwargs):
        raise release.ReleaseError('Reached backend bundle check')

    monkeypatch.setattr(release, 'run', next_package_check)
    expected = 'Reached backend bundle check' if change == 'reordered' else 'updater configuration'
    with pytest.raises(release.ReleaseError, match=expected):
        release.check_packages(tmp_path, tmp_path / 'candidate', {}, source, 'candidate', 'A' * 40)


def test_anonymous_download_rejects_non_https_before_writing(tmp_path):
    with pytest.raises(release.ReleaseError):
        release.download("http://example.invalid/asset", tmp_path / "asset")
    assert list(tmp_path.iterdir()) == []


def test_publish_rejects_pending_acceptance_before_tag_or_apple(publish_case):
    root, remote, args = publish_case
    acceptance = release.read_json(root / 'acceptance.json')
    acceptance['reviewer'] = ''
    release.atomic_json(root / 'acceptance.json', acceptance)
    assert release.main(args) == 1
    assert remote['writes'] == []


def test_partial_upload_reconciles_existing_bytes_and_only_uploads_missing(publish_case):
    _root, remote, args = publish_case
    remote['lost_response'] = 'upload'
    assert release.main(args) == 1
    remote['lost_response'] = None
    assert release.main(args) == 1
    uploads = [item for item in remote['writes'] if item.startswith('upload:')]
    assert len(uploads) == len(set(uploads)) == 3


def test_unowned_existing_release_is_never_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "github_release", lambda *_: {"body": "someone else's release"})
    with pytest.raises(release.ReleaseError, match="ownership"):
        release.ensure_release({"version": "1.0.2", "github": "example/repo"},
                               {"owner_id": "mine", "source": "a" * 40}, {}, tmp_path / "progress.json")


def test_runtime_environment_does_not_inherit_secrets_or_real_app_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", "/real/user/data")
    monkeypatch.setenv("CHEAP_MODEL_API_KEY", "private-test-value")
    monkeypatch.setenv("GH_TOKEN", "private-test-token")
    monkeypatch.setenv("PYTHONPATH", "/real/other/source")
    environment = release.isolated_env(tmp_path)
    assert "CHEAP_MODEL_API_KEY" not in environment and "GH_TOKEN" not in environment
    assert "PYTHONPATH" not in environment
    assert Path(environment["LIVE_CLIPPER_HOME"]).is_relative_to(tmp_path)
    assert Path(environment["HOME"]).is_relative_to(tmp_path)


def test_cleanup_stops_on_occupied_owned_path_and_keeps_other_data(tmp_path, monkeypatch):
    release.atomic_json(tmp_path / 'owner.json', {'format': 1, 'id': 'mine', 'repo': str(release.ROOT)})
    source = release.owned_directory(tmp_path, 'source')
    unrelated = tmp_path / 'keep'
    unrelated.write_text('user data')
    monkeypatch.setattr(release.subprocess, 'run', lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, '123', ''))
    result = release.remove_owned(tmp_path, 'source')
    assert not result['removed'] and 'occupied' in result['reason']
    assert source.is_dir() and unrelated.read_text() == 'user data'



@pytest.mark.parametrize("failure", ["disk", "credentials", "dirty", "old_version"])
def test_candidate_preflight_failure_has_no_build_or_publish_side_effects(tmp_path, monkeypatch, failure):
    import argparse

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(release, "ROOT", repo)
    monkeypatch.setattr(release, "metadata", lambda *_: {"version": "1.0.2", "github": "example/repo"})
    monkeypatch.setattr(release, "check_source", lambda *_: release.require(failure != "dirty", "dirty"))
    monkeypatch.setattr(release, "git", lambda _repo, *args: "b" * 40 if args[0] == "rev-parse" else "")
    monkeypatch.setattr(release, "github_release", lambda *_: None)
    monkeypatch.setattr(release, "tools_snapshot", lambda: {})
    monkeypatch.setattr(release.shutil, "disk_usage", lambda *_: shutil._ntuple_diskusage(1, 0, 0 if failure == "disk" else 20 * 1024**3))
    monkeypatch.delenv("VENUS_NOTARY_PROFILE", raising=False)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args[:2] == ["security", "find-identity"]
        return '1) ' + 'A' * 40 + ' "Developer ID Application: Test"'

    monkeypatch.setattr(release, "run", command)
    args = argparse.Namespace(previous_published_manifest=None, source="a" * 40, previous_tag="v1.0.2" if failure == "old_version" else "v1.0.1", output=tmp_path / "new-release")
    with pytest.raises(release.ReleaseError):
        release.candidate(args)
    assert not args.output.exists()
    assert len(calls) <= 1


def test_candidate_existing_frozen_directory_is_verified_without_rebuilding(tmp_path, monkeypatch):
    import argparse

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(release, "ROOT", repo)
    root = tmp_path / "release"
    root.mkdir()
    (root / "candidate-manifest.json").write_text("{}")
    monkeypatch.setattr(release, "metadata", lambda *_: {"version": "1.0.2", "github": "example/repo"})
    monkeypatch.setattr(release, "check_source", lambda *_: None)
    monkeypatch.setattr(release, "git", lambda *_: "a" * 40)
    monkeypatch.setattr(release, "verify_candidate", lambda *_: {"source": "a" * 40, "previous_tag": "v1.0.1", "tools": {}})
    monkeypatch.setattr(release, "tools_snapshot", lambda: {})
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: pytest.fail("Must not rebuild or write externally"))
    (root / 'evidence').mkdir()
    release.atomic_json(root / 'evidence/previous-input.json', {'input': {'mode': 'network'}})
    monkeypatch.setattr(release, 'previous_input', lambda *_: ({'mode': 'network'}, None, None))
    release.candidate(argparse.Namespace(previous_published_manifest=None, source="a" * 40, previous_tag="v1.0.1", output=root))


def test_archive_rejects_escape_before_extracting(tmp_path):
    import zipfile

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../outside", b"unsafe")
    with pytest.raises(release.ReleaseError, match="Unsafe"):
        release.extract_zip(archive, tmp_path / "unpacked", lambda *_: pytest.fail("No extraction"))
    assert not (tmp_path / "unpacked").exists()


def test_archive_cannot_write_through_its_own_symlink(tmp_path):
    import stat
    import zipfile

    archive = tmp_path / "unsafe-link.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        link = zipfile.ZipInfo("alias")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        stream.writestr(link, ".")
        stream.writestr("alias/data", "must not extract")
    with pytest.raises(release.ReleaseError, match="symlink"):
        release.extract_zip(archive, tmp_path / "unpacked", lambda *_: pytest.fail("No extraction"))


def test_blockmap_helper_uses_pinned_builder_without_publishing(tmp_path):
    source = Path(__file__).parents[1]
    if not (source / "desktop/node_modules/app-builder-lib").is_dir():
        pytest.skip("Desktop dependencies required for blockmap integration check")
    asset = tmp_path / "asset.zip"
    asset.write_bytes(bytes(range(256)) * 200)
    subprocess.run(["node", source / "scripts/release-smoke.cjs", "blockmap", asset, source], check=True)
    import gzip

    blockmap = json.loads(gzip.decompress(Path(str(asset) + ".blockmap").read_bytes()))
    assert blockmap["version"] == "2"
    assert sum(blockmap["files"][0]["sizes"]) == asset.stat().st_size


@pytest.mark.skipif(not all(os.environ.get(key) for key in ("VENUS_RELEASE_TEST_PREVIOUS_ZIP", "VENUS_RELEASE_TEST_CURRENT_ZIP")), reason="Explicit retained release ZIPs required")
def test_retained_release_backend_and_previous_updater(tmp_path):
    """Read-only original assets; all extraction, probes and cache remain under pytest tmp_path."""
    old = Path(os.environ["VENUS_RELEASE_TEST_PREVIOUS_ZIP"]).resolve()
    current = Path(os.environ["VENUS_RELEASE_TEST_CURRENT_ZIP"]).resolve()
    originals = {file: release.sha256(file) for file in (old, current)}
    release.atomic_json(tmp_path / "owner.json", {"format": 1, "repo": str(release.ROOT), "id": "retained-test", "source": "test"})
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    release.extract_zip(old, tmp_path / "previous")
    release.extract_zip(current, tmp_path / "current")
    source = Path(__file__).parents[1]
    info = release.metadata(source)
    app = tmp_path / "current/Venus.app"
    assert release.app_facts(app, info, evidence, "retained")["hardened_runtime"]
    assert release.backend_smoke(tmp_path, app)["scan_preview"]["processable_files"] == 1
    serving = tmp_path / "assets"
    serving.mkdir()
    for name in release.asset_names(info['version']).values():
        original = current.parent / name
        if name.endswith('-media-sources.tar.gz') and os.environ.get('VENUS_RELEASE_TEST_MEDIA_SOURCE'):
            original = Path(os.environ['VENUS_RELEASE_TEST_MEDIA_SOURCE']).resolve()
            originals[original] = release.sha256(original)
        shutil.copy2(original, serving / name)
    release.release_assets(serving, info['version'])
    release.check_media_source(serving, app, info)
    result = release.provider_smoke(tmp_path, source, tmp_path / "previous/Venus.app", serving, info)
    assert release.version_tuple(result["previous_version"]) < release.version_tuple(result["version"])
    assert result["version"] == info["version"]
    assert result["native_installer_executed"] is False
    assert all(release.sha256(file) == digest for file, digest in originals.items())


def test_five_assets_and_corresponding_source_notice_are_mandatory(tmp_path):
    version = '1.0.3'
    for name in (f'Venus-{version}-arm64.dmg', f'Venus-{version}-arm64-mac.zip',
                 f'Venus-{version}-arm64-mac.zip.blockmap', 'latest-mac.yml'):
        (tmp_path / name).write_bytes(b'asset')
    with pytest.raises(release.ReleaseError, match='Five'):
        release.release_assets(tmp_path, version)
    source = tmp_path / f'Venus-{version}-media-sources.tar.gz'
    source.write_bytes(b'corresponding source')
    assert release.release_assets(tmp_path, version)['media_source'] == source.name
    app = tmp_path / 'Venus.app'
    notice = app / 'Contents/Resources/licenses/ffmpeg/CORRESPONDING-SOURCE.md'
    notice.parent.mkdir(parents=True)
    notice.write_text(f'https://github.com/example/repo/releases/download/v{version}/{source.name}\n{release.sha256(source)}')
    release.check_media_source(tmp_path, app, {'version': version, 'github': 'example/repo'})
    source.write_bytes(b'changed')
    with pytest.raises(release.ReleaseError):
        release.check_media_source(tmp_path, app, {'version': version, 'github': 'example/repo'})


def test_shared_cache_environment_is_owned_and_separate_from_mutable_environments(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    cache = release.prepare_download_cache(tmp_path, repo)
    one = release.isolated_env(tmp_path / 'one', cache=cache)
    two = release.isolated_env(tmp_path / 'two', cache=cache)
    for key in ('npm_config_cache', 'PIP_CACHE_DIR', 'ELECTRON_CACHE', 'ELECTRON_BUILDER_CACHE'):
        assert one[key] == two[key]
        assert Path(one[key]).is_relative_to(cache)
    assert one['HOME'] != two['HOME']
    assert 'PYTHONPATH' not in one
    owner = cache / 'owner.json'
    owner.write_text('{}')
    with pytest.raises(release.ReleaseError):
        release.prepare_download_cache(tmp_path, repo)


def test_cleanup_requires_registered_identity_not_directory_names(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    known = release.owned_directory(root, 'scratch')
    (known / 'file').write_text('temporary')
    unknown = root / 'source'
    unknown.mkdir()
    (unknown / 'user').write_text('keep')
    report = release.remove_owned(root, 'source')
    assert not report['removed'] and unknown.is_dir()
    report = release.remove_owned(root, 'scratch')
    assert report['removed'] and not known.exists()
    assert (unknown / 'user').read_text() == 'keep'


def test_owned_cleanup_rejects_replaced_directory_and_escaping_link(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    target = release.owned_directory(root, 'scratch')
    target.rename(root / 'original')
    target.mkdir()
    assert not release.remove_owned(root, 'scratch')['removed']
    safe = release.owned_directory(root, 'other')
    (safe / 'escape').symlink_to(tmp_path)
    assert not release.remove_owned(root, 'other')['removed']
    assert safe.exists()


def test_recovery_does_not_signal_reused_pid(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    release.atomic_json(root / 'resources.json', {'processes': [{'pid': 12345, 'identity': 'old', 'pgid': 12345}], 'mounts': []})
    monkeypatch.setattr(release, 'process_identity', lambda pid: 'different owner')
    monkeypatch.setattr(release.os, 'killpg', lambda *_: pytest.fail('Must not signal reused PID'))
    assert release.recover_resources(root)


def test_space_report_blocks_before_install_and_identifies_unknown_residue(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    (tmp_path / 'unknown-old-build').mkdir()
    monkeypatch.setattr(release.shutil, 'disk_usage', lambda *_: shutil._ntuple_diskusage(100, 100, 0))
    with pytest.raises(release.ReleaseError, match='space'):
        release.space_preflight(tmp_path / 'new', tmp_path / 'download-cache', repo)
    assert not (tmp_path / 'new').exists()


def test_real_process_timeout_and_recovery_leave_no_owned_service(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    scope = release.owned_directory(root, 'probe')
    env = release.isolated_env(scope)
    with pytest.raises(subprocess.TimeoutExpired):
        release.run([sys.executable, '-c', 'import signal; signal.pause()'], cwd=scope, env=env, timeout=0.2, release_root=root)
    assert release.resource_state(root)['processes'] == []
    process = subprocess.Popen(
        [sys.executable, '-c', 'import signal; print("ready", flush=True); signal.pause()'],
        cwd=scope, start_new_session=True, stdout=subprocess.PIPE, text=True,
    )
    try:
        # A framework launcher may replace its executable before the child is ready.
        assert select.select([process.stdout], [], [], 5)[0], 'Owned child did not become ready'
        assert process.stdout.readline().strip() == 'ready'
        release.remember_process(root, process, scope)
        assert release.recover_resources(root) == []
        process.wait(timeout=2)
        assert release.resource_state(root)['processes'] == []
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        process.stdout.close()


def test_cleanup_retains_two_versions_resolves_failure_and_retries(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    source = 'a' * 40
    roots = []
    for version in ('1.0.1', '1.0.2', '1.0.3'):
        root = tmp_path / version
        owner = release.create_root(root, repo, source)
        release.atomic_json(root / 'candidate-manifest.json', {'source': source, 'owner_id': owner['id']})
        final = release.owned_directory(root, 'finalize')
        for name in release.asset_names(version).values():
            (final / name).write_text(version + name)
        release.owned_directory(root, 'source')
        release.atomic_json(root / 'published-manifest.json', {
            'source': source, 'tag': 'v' + version, 'github': 'test/repo', 'release_id': version,
            'candidate_sha256': release.sha256(root / 'candidate-manifest.json'),
            'assets': release.asset_records(final, version),
        })
        roots.append(root)
    failed = tmp_path / 'failed'
    owner = release.create_root(failed, repo, source)
    release.owned_directory(failed, 'source')
    release.atomic_json(failed / 'resolution.json', {
        'owner_id': owner['id'], 'successor': str(roots[-1]), 'reason': 'test-only incident resolved',
        'successor_candidate_sha256': release.sha256(roots[-1] / 'candidate-manifest.json'),
    })
    unknown = tmp_path / 'manual'
    unknown.mkdir()
    (unknown / 'user').write_text('keep')
    monkeypatch.setattr(release, 'git', lambda *_: '')
    monkeypatch.setattr(release, 'github_release', lambda _, tag: {
        'id': tag[1:], 'draft': False, 'assets': [
            {'name': name, 'browser_download_url': 'https://example.test/' + name}
            for name in release.asset_names(tag[1:]).values()],
    })
    monkeypatch.setattr(release, 'verify_remote_asset', lambda *_: None)
    # A real open file must prevent source removal. Retry cannot call publication.
    with (roots[-1] / 'source/busy').open('w'):
        report = release.cleanup(roots[-1], apply=True)
        assert not report['cleanup_complete']
        assert (roots[-1] / 'source').exists()
    assert release.cleanup(roots[-1], apply=True)['cleanup_complete']
    assert release.cleanup(roots[-1], apply=True)['cleanup_complete']
    assert not (roots[0] / 'release-assets').exists()
    assert not (failed / 'source').exists()
    for root in roots[1:]:
        done = release.published_record(root)
        assert release.asset_records(root / 'release-assets', done['tag'][1:]) == done['assets']
    assert (unknown / 'user').read_text() == 'keep'
    release.prepare_download_cache(tmp_path, repo)
    original_limit = release.limit_download_cache
    def blocked_cache(_cache):
        raise release.ReleaseError('cache occupied')
    monkeypatch.setattr(release, 'limit_download_cache', blocked_cache)
    assert not release.cleanup(roots[-1], apply=True)['cleanup_complete']
    assert not release.published_record(roots[-1])['cleanup_complete']
    monkeypatch.setattr(release, 'limit_download_cache', original_limit)
    assert release.cleanup(roots[-1], apply=True)['cleanup_complete']


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS mount lifecycle')
def test_owned_mount_detaches_after_exception(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    root = tmp_path / 'release'
    release.create_root(root, repo, 'a' * 40)
    data = release.owned_directory(root, 'image-source')
    (data / 'marker').write_text('isolated mount check')
    dmg = root / 'test.dmg'
    release.run(['hdiutil', 'create', '-srcfolder', data, '-format', 'UDZO', dmg])
    mount = root / 'mounted'
    with pytest.raises(RuntimeError, match='injected'):
        with release.mounted_dmg(dmg, mount, root / 'evidence'):
            assert (mount / 'marker').read_text() == 'isolated mount check'
            raise RuntimeError('injected probe interruption')
    assert not os.path.ismount(mount)
    assert release.resource_state(root)['mounts'] == []
    assert not mount.exists()


@pytest.fixture
def apple_tools(monkeypatch):
    monkeypatch.setattr(release, "python_identity", lambda: {"version": "3.11.9", "executable": "/base/bin/python3.11", "sha256": "a" * 64, "base_prefix": "/base", "stdlib": "/base/lib/python3.11"})
    monkeypatch.setattr(release.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(release.platform, "machine", lambda: "arm64")
    for key in ("DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(release.shutil, "which", lambda name, **kwargs: "/usr/bin/" + name)
    responses = {
        ("python3.11", "--version"): "Python 3.11.9",
        ("node", "--version"): "v24.1.0",
        ("npm", "--version"): "11.1.0",
        ("/usr/bin/xcode-select", "-p"): "/Library/Developer/CommandLineTools",
        ("/usr/bin/clang", "--version"): "Apple clang version 17.0.0",
        ("/usr/bin/xcrun", "--show-sdk-version"): "26.2",
        ("/usr/bin/xcrun", "--show-sdk-path"): "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    }
    calls = []

    def command(args, **kwargs):
        calls.append(tuple(args))
        assert "xcodebuild" not in args
        if args[0].startswith("/usr/bin/"):
            assert kwargs["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        if tuple(args) in responses:
            return responses[tuple(args)]
        if args[:2] == ["/usr/bin/xcrun", "--find"]:
            return "/Library/Developer/CommandLineTools/usr/bin/" + args[2]
        return "available"

    monkeypatch.setattr(release, "run", command)
    return responses, calls


def test_tools_snapshot_accepts_clt_and_records_actual_apple_tools(apple_tools):
    snapshot = release.tools_snapshot()
    assert snapshot["developer_dir"] == "/Library/Developer/CommandLineTools"
    assert snapshot["clang"] == "Apple clang version 17.0.0"
    assert snapshot["sdk_version"] == "26.2"
    assert snapshot["sdk_path"].endswith("MacOSX.sdk")
    assert snapshot["tool_paths"]["vtool"].endswith("/vtool")
    assert snapshot["tool_paths"]["make"] == "/usr/bin/make"
    assert "xcode" not in snapshot


@pytest.mark.parametrize("probe", [
    ("/usr/bin/xcode-select", "-p"), ("/usr/bin/clang", "--version"),
    ("/usr/bin/xcrun", "--show-sdk-version"), ("/usr/bin/xcrun", "--show-sdk-path"),
    ("/usr/bin/xcrun", "--find", "vtool"), ("/usr/bin/xcrun", "--find", "notarytool"),
    ("/usr/bin/xcrun", "--find", "stapler"), ("/usr/bin/xcrun", "--find", "clang"),
])
@pytest.mark.parametrize("failure", ["empty", "exit"])
def test_tools_snapshot_rejects_unusable_apple_probe(apple_tools, monkeypatch, probe, failure):
    original = release.run

    def command(args, **kwargs):
        if tuple(args) == probe:
            if failure == "exit":
                raise release.ReleaseError("probe failed")
            return "  "
        return original(args, **kwargs)

    monkeypatch.setattr(release, "run", command)
    with pytest.raises(release.ReleaseError):
        release.tools_snapshot()


@pytest.mark.parametrize("tool", ["make", "otool", "codesign"])
def test_tools_snapshot_rejects_missing_required_tool(apple_tools, monkeypatch, tool):
    monkeypatch.setattr(release.shutil, "which", lambda name, **kwargs: None if name == tool else "/usr/bin/" + name)
    with pytest.raises(release.ReleaseError, match="Missing"):
        release.tools_snapshot()


@pytest.mark.parametrize("key", ["DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS"])
def test_tools_snapshot_rejects_build_environment_overrides(apple_tools, monkeypatch, key):
    monkeypatch.setenv(key, "/different/toolchain")
    with pytest.raises(release.ReleaseError, match="override"):
        release.tools_snapshot()
    assert apple_tools[1] == []


@pytest.mark.parametrize("field", ["developer_dir", "clang", "sdk_version", "sdk_path", "tool_paths", "python_path", "python_hash"])
@pytest.mark.parametrize("operation", ["candidate", "publish"])
def test_changed_tool_snapshot_stops_before_build_or_external_writes(tmp_path, monkeypatch, apple_tools, field, operation):
    import argparse
    import copy

    frozen = release.tools_snapshot()
    changed = copy.deepcopy(frozen)
    if field in ("python_path", "python_hash"):
        changed["python_identity"]["executable" if field == "python_path" else "sha256"] += "-changed"
    elif field == "tool_paths":
        changed[field]["notarytool"] = "/different/notarytool"
    else:
        changed[field] += "-changed"
    root = tmp_path / "release"
    root.mkdir()
    manifest = root / "candidate-manifest.json"
    manifest.write_text("{}")
    repo = tmp_path / "repo"
    repo.mkdir()
    info = {"version": "1.0.4", "github": "example/repo"}
    monkeypatch.setattr(release, "ROOT", repo)
    monkeypatch.setattr(release, "metadata", lambda *_: info)
    monkeypatch.setattr(release, "check_source", lambda *_: None)
    monkeypatch.setattr(release, "git", lambda *_: "a" * 40)
    monkeypatch.setattr(release, "verify_candidate", lambda *_: {
        **info, "owner_id": "test-owner", "source": "a" * 40, "previous_tag": "v1.0.3", "tools": frozen})
    release.atomic_json(root / "owner.json", {"format": 1, "repo": str(repo), "id": "test-owner", "source": "a" * 40})
    monkeypatch.setattr(release, "tools_snapshot", lambda: changed)
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: pytest.fail("No build or external command allowed"))
    monkeypatch.setattr(release, "ensure_tag", lambda *_: pytest.fail("No tag allowed"))
    with pytest.raises(release.ReleaseError, match="Tool environment changed"):
        if operation == "candidate":
            release.candidate(argparse.Namespace(previous_published_manifest=None, source="a" * 40, previous_tag="v1.0.3", output=root))
        else:
            release.publish(argparse.Namespace(candidate=manifest, acceptance=root / "acceptance.json", confirm_version="v1.0.4"))
    assert sorted(p.name for p in root.iterdir()) == ["candidate-manifest.json", "owner.json"]


def test_resolved_python_creates_working_copied_venv_and_media_environment(tmp_path, monkeypatch):
    entry = tmp_path / 'entry'
    entry.mkdir()
    real = Path(subprocess.check_output([
        'python3.11', '-I', '-c', 'import os,sys; print(os.path.realpath(sys._base_executable))'
    ], text=True).strip())
    (entry / 'python3.11').symlink_to(real)
    monkeypatch.setenv('PATH', str(entry) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('PYTHONHOME', '/unrelated/python')
    monkeypatch.setenv('PYTHONPATH', '/unrelated/modules')
    monkeypatch.setenv('__PYVENV_LAUNCHER__', '/unrelated/launcher')
    identity = release.python_identity()
    assert identity['executable'] == str(real)
    assert identity['sha256'] == release.sha256(real)
    repository = tmp_path / 'test-repo'
    repository.mkdir()
    monkeypatch.setattr(release, 'ROOT', repository)
    outer = tmp_path / 'outer'
    outer.mkdir()
    outer_owner = {'format': 1, 'repo': '/unrelated/repository', 'id': 'outer'}
    release.atomic_json(outer / 'owner.json', outer_owner)
    release.atomic_json(outer / 'resources.json', {'processes': [], 'mounts': []})
    before = {file.name: file.read_bytes() for file in outer.iterdir()}
    with pytest.raises(release.ReleaseError, match='ownership mismatch'):
        release.owner_record(outer)
    inner = outer / 'inner'
    release.create_root(inner, release.ROOT, 'a' * 40)
    scope = release.owned_directory(inner, 'environment')
    environment = release.isolated_env(scope, python=identity['executable'])
    assert 'VENUS_RELEASE_ROOT' not in environment
    environment['PIP_NO_INDEX'] = '1'
    assert Path(shutil.which('python3.11', path=environment['PATH'])).resolve() == real
    target = release.owned_directory(inner, 'copied')
    release.run([identity['executable'], '-I', '-m', 'venv', '--copies', target], env=environment, release_root=inner)
    python = target / 'bin/python'
    assert not python.is_symlink()
    facts = json.loads(release.run([python, '-I', '-c',
        'import encodings,ssl,sqlite3,venv,sys,sysconfig,json; '
        'print(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix,"stdlib":sysconfig.get_path("stdlib")}))'],
        env=environment, release_root=inner))
    assert Path(facts['prefix']).resolve() == target.resolve()
    assert facts['base_prefix'] == identity['base_prefix']
    assert facts['stdlib'] == identity['stdlib']
    assert str(target) in release.run([python, '-I', '-m', 'pip', '--version'], env=environment, release_root=inner)
    media = json.loads(release.run(['python3.11', '-I', '-c',
        'import sys,json; print(json.dumps({"executable":sys.executable,"base_prefix":sys.base_prefix}))'], env=environment, release_root=inner))
    assert media['executable'] == identity['executable']
    assert media['base_prefix'] == identity['base_prefix']
    monkeypatch.setenv('PATH', str(target / 'bin') + os.pathsep + os.environ['PATH'])
    assert release.python_identity() == identity
    assert {name: (outer / name).read_bytes() for name in before} == before
    assert release.resource_state(inner) == {'processes': [], 'mounts': []}


@pytest.mark.parametrize('pinned', [True, False])
def test_cache_purge_uses_selected_base_python(tmp_path, monkeypatch, pinned):
    cache = release.prepare_download_cache(tmp_path, release.ROOT)
    monkeypatch.setattr(release, 'python_identity', lambda: {'executable': '/selected/bin/python3.11'})
    sizes = iter([release.CACHE_LIMIT + 1, 0, 0])
    monkeypatch.setattr(release, 'directory_bytes', lambda *_: next(sizes))
    monkeypatch.setattr(release.subprocess, 'run', lambda *_a, **_kw: subprocess.CompletedProcess([], 1, '', ''))
    calls = []
    monkeypatch.setattr(release, 'run', lambda args, **kwargs: calls.append(args))
    release.limit_download_cache(cache, **({'python': '/selected/bin/python3.11'} if pinned else {}))
    assert calls[1][:5] == ['/selected/bin/python3.11', '-I', '-m', 'pip', '--isolated']



@pytest.mark.parametrize('boundary', ['venv', 'signing'])
def test_candidate_uses_preflight_identities(tmp_path, monkeypatch, boundary):
    import argparse

    monkeypatch.setattr(release, 'ROOT', tmp_path / 'repo')
    monkeypatch.setattr(release, 'metadata', lambda *_: {'version': '1.0.4', 'github': 'example/repo'})
    monkeypatch.setattr(release, 'check_source', lambda *_: None)
    monkeypatch.setattr(release, 'git', lambda _repo, *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    monkeypatch.setattr(release, 'github_release', lambda *_: None)
    monkeypatch.setattr(release, 'tools_snapshot', lambda: {'python_identity': {'executable': '/selected/bin/python3.11'}})
    monkeypatch.setattr(release, 'space_preflight', lambda *_: {})
    monkeypatch.setattr(release, 'prune_media_cache', lambda *_: {'complete': True})
    monkeypatch.setattr(release, 'cleanup_plan', lambda *_: {})
    monkeypatch.setattr(release, 'cache_keep_versions', lambda *_: [])
    monkeypatch.setattr(release, 'previous_app', lambda *_: tmp_path / 'previous.app')
    purges = []
    monkeypatch.setattr(release, 'limit_download_cache', lambda cache, python: purges.append(python))
    monkeypatch.setenv('VENUS_NOTARY_PROFILE', 'test-only')

    def command(args, **kwargs):
        if args[0] == 'security':
            return '1) ' + 'A' * 40 + ' "Developer ID Application: Test"'
        if 'venv' in args:
            assert args == ['/selected/bin/python3.11', '-I', '-m', 'venv', '--copies', '.venv']
            assert kwargs['env']['PATH'].split(os.pathsep)[0] == '/selected/bin'
            if boundary == 'venv':
                raise release.ReleaseError('Reached selected venv boundary')
        if Path(args[0]).name == 'electron-builder':
            assert kwargs['env']['CSC_NAME'] == 'A' * 40
            assert '-c.forceCodeSigning=true' in args and '-c.mac.notarize=false' in args
            builder = (Path(__file__).parents[1] / 'desktop/electron-builder.yml').read_text()
            assert '\ndmg:\n  sign: true\n' in builder
            raise release.ReleaseError('Reached selected signing boundary')
        if 'audit' in args:
            return '{"metadata": {"vulnerabilities": {"total": 0}}}'
        assert args[0] in ('xcrun', 'git', 'npm', 'bash', '/selected/bin/python3.11',
                          '.venv/bin/pip', '.venv/bin/python', '.venv/bin/ruff')
        return ''

    monkeypatch.setattr(release, 'run', command)
    with pytest.raises(release.ReleaseError, match=f'Reached selected {boundary} boundary'):
        release.candidate(argparse.Namespace(previous_published_manifest=None, source='a' * 40, previous_tag='v1.0.3', output=tmp_path / 'release'))
    assert purges and set(purges) == {'/selected/bin/python3.11'}


@pytest.fixture
def previous_input_files(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    tag, source, tag_object = 'v1.0.1', 'a' * 40, 'b' * 40
    def git(_repo, *args):
        if '--git-common-dir' in args:
            return str(_repo / '.git')
        if args[0] == 'ls-remote':
            return f'{tag_object}\trefs/tags/{tag}\n{source}\trefs/tags/{tag}^{{}}'
        return source if args[-1].endswith('^{commit}') else tag_object
    monkeypatch.setattr(release, 'git', git)
    root = tmp_path / 'published'
    owner = release.create_root(root, repo, source)
    assets = release.owned_directory(root, 'release-assets')
    name = release.asset_names(tag[1:])['zip']
    archive = assets / name
    archive.write_bytes(b'final stapled bytes')
    records = {n: {'size': archive.stat().st_size, 'sha256': release.sha256(archive)}
               for n in release.asset_names(tag[1:]).values()}
    candidate = {'owner_id': owner['id'], 'source': source, 'github': 'example/repo', 'version': tag[1:]}
    release.atomic_json(root / 'candidate-manifest.json', candidate)
    candidate_hash = release.sha256(root / 'candidate-manifest.json')
    final = {'candidate_sha256': candidate_hash, 'source': source, 'assets': records,
             'signature': {'cdhash': 'c' * 40, 'timestamp': 'today', 'hardened_runtime': True}}
    release.atomic_json(root / 'final-manifest.json', final)
    done = {'candidate_sha256': candidate_hash, 'source': source, 'tag': tag, 'tag_object': tag_object,
            'github': 'example/repo', 'release_id': 123, 'assets': records}
    manifest = root / 'published-manifest.json'
    release.atomic_json(manifest, done)
    remote = {'draft': False, 'prerelease': False, 'id': 123, 'tag_name': tag,
              'assets': [{'id': 456, 'name': name, 'size': archive.stat().st_size,
                          'digest': 'sha256:' + release.sha256(archive), 'browser_download_url': 'https://example.test/old.zip'}]}
    monkeypatch.setattr(release, 'github_release', lambda *_: remote)
    monkeypatch.setattr(release, 'download', lambda *_: pytest.fail('Explicit local input must never download'))
    return manifest, archive, remote


@pytest.mark.parametrize('fault', ['repo', 'tag', 'source', 'tag_object', 'release_id', 'candidate_hash',
                                   'final_hash', 'final_source', 'final_assets', 'remote_id', 'remote_tag',
                                   'remote_size', 'remote_digest', 'missing_digest', 'duplicate_zip',
                                   'truncated', 'zip_directory', 'zip_symlink', 'manifest_symlink', 'parent_symlink',
                                   'owner_repo', 'owner_source', 'owner_id', 'candidate_repo', 'candidate_version'])
def test_previous_input_rejects_invalid_files_and_remote_identity(previous_input_files, monkeypatch, tmp_path, fault):
    manifest, archive, remote = previous_input_files
    if fault.startswith('remote_') or fault in ('missing_digest', 'duplicate_zip'):
        if fault == 'remote_id':
            remote['id'] += 1
        elif fault == 'remote_tag':
            remote['tag_name'] = 'v9.9.9'
        elif fault == 'duplicate_zip':
            remote['assets'].append(dict(remote['assets'][0]))
        elif fault == 'missing_digest':
            remote['assets'][0].pop('digest')
        else:
            remote['assets'][0][fault[7:]] = 1 if fault == 'remote_size' else 'sha256:' + '0' * 64
    elif fault in ('truncated', 'zip_directory', 'zip_symlink'):
        archive.unlink()
        if fault == 'truncated':
            archive.write_bytes(b'short')
        elif fault == 'zip_directory':
            archive.mkdir()
        else:
            target = tmp_path / 'outside.zip'
            target.write_bytes(b'final stapled bytes')
            archive.symlink_to(target)
    elif fault == 'manifest_symlink':
        target = tmp_path / 'outside.json'
        manifest.rename(target)
        manifest.symlink_to(target)
    elif fault == 'parent_symlink':
        target = tmp_path / 'alias'
        target.symlink_to(manifest.parent, target_is_directory=True)
        manifest = target / manifest.name
    else:
        path = manifest
        if fault.startswith('final_'):
            path = manifest.parent / 'final-manifest.json'
        elif fault.startswith('owner_'):
            path = manifest.parent / 'owner.json'
        elif fault.startswith('candidate_') and fault != 'candidate_hash':
            path = manifest.parent / 'candidate-manifest.json'
        data = release.read_json(path)
        key = {'repo': 'github', 'candidate_hash': 'candidate_sha256', 'final_hash': 'candidate_sha256',
               'final_source': 'source', 'final_assets': 'assets', 'owner_repo': 'repo', 'owner_source': 'source',
               'owner_id': 'id', 'candidate_repo': 'github', 'candidate_version': 'version'}.get(fault, fault)
        data[key] = {} if key == 'assets' else str(tmp_path / 'other-repo') if fault == 'owner_repo' else 'wrong'
        release.atomic_json(path, data)
    before = {p: p.read_bytes() for p in manifest.parent.glob('*.json') if p.is_file()}
    with pytest.raises((release.ReleaseError, OSError, ValueError)):
        release.previous_input('example/repo', 'v1.0.1', manifest)
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize('fault', ['signature', 'provider', 'copy_changed_source', 'copy_corrupt'])
def test_previous_copy_and_app_checks_stop_on_mismatch(previous_input_files, monkeypatch, tmp_path, fault):
    manifest, archive, _ = previous_input_files
    root = tmp_path / 'new'
    release.create_root(root, release.ROOT, 'd' * 40)
    original_bytes = archive.read_bytes()
    copy = release.shutil.copyfileobj
    def copy_bytes(source, target, length):
        copy(source, target, length)
        if fault == 'copy_changed_source':
            archive.write_bytes(b'changed during copy')
        if fault == 'copy_corrupt':
            target.write(b'extra')
    monkeypatch.setattr(release.shutil, 'copyfileobj', copy_bytes)
    def extract(_archive, target):
        app = target / 'Venus.app/Contents/Resources'
        app.mkdir(parents=True)
        (app / 'app-update.yml').write_text('provider: github\nowner: example\nrepo: wrong\nupdaterCacheDirName: cache\n')
    monkeypatch.setattr(release, 'extract_zip', extract)
    signature = release.read_json(manifest.parent / 'final-manifest.json')['signature']
    if fault == 'signature':
        signature['cdhash'] = '0' * 40
    monkeypatch.setattr(release, 'app_facts', lambda *_: signature)
    with pytest.raises(release.ReleaseError, match={'signature': 'signature', 'provider': 'Provider',
                            'copy_changed_source': 'changed during copy', 'copy_corrupt': 'bytes mismatch'}[fault]):
        release.previous_app(root, 'example/repo', 'v1.0.1', {}, manifest)
    assert not (root / 'evidence/previous-input.json').exists()
    if fault != 'copy_changed_source':
        assert archive.read_bytes() == original_bytes
    assert archive.stat().st_ino != (root / 'previous' / archive.name).stat().st_ino


@pytest.mark.parametrize('local', [True, False])
def test_previous_failure_precedes_dependency_installation(tmp_path, monkeypatch, local):
    import argparse
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(release, 'ROOT', repo)
    monkeypatch.setattr(release, 'metadata', lambda *_: {'version': '1.0.2', 'github': 'example/repo'})
    monkeypatch.setattr(release, 'check_source', lambda *_: None)
    monkeypatch.setattr(release, 'git', lambda _repo, *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    monkeypatch.setattr(release, 'github_release', lambda *_: None)
    monkeypatch.setattr(release, 'tools_snapshot', lambda: {})
    monkeypatch.setattr(release, 'space_preflight', lambda *_: {})
    monkeypatch.setenv('VENUS_NOTARY_PROFILE', 'test-only')
    calls = []
    def command(args, **kwargs):
        calls.append(args[0])
        assert args[0] in ('security', 'xcrun')
        return '1) ' + 'A' * 40 + ' "Developer ID Application: Test"'
    monkeypatch.setattr(release, 'run', command)
    selected = tmp_path / 'published-manifest.json' if local else None
    def previous(root, github, tag, info, manifest):
        assert manifest == selected
        assert not (root / 'source').exists() and not (root.parent / 'download-cache').exists()
        raise release.ReleaseError('Previous input rejected')
    monkeypatch.setattr(release, 'previous_app', previous)
    with pytest.raises(release.ReleaseError, match='Previous input rejected'):
        release.candidate(argparse.Namespace(source='a' * 40, previous_tag='v1.0.1',
                          output=tmp_path / 'release', previous_published_manifest=selected))
    assert calls == ['security', 'xcrun']


@pytest.mark.parametrize('change', ['mode', 'manifest_sha256', 'asset_id', 'sha256'])
def test_frozen_previous_input_change_is_rejected(tmp_path, monkeypatch, change):
    import argparse
    root = tmp_path / 'release'
    (root / 'evidence').mkdir(parents=True)
    (root / 'candidate-manifest.json').write_text('{}')
    original = {'mode': 'published', 'manifest_sha256': 'a' * 64, 'asset_id': 1, 'sha256': 'b' * 64}
    release.atomic_json(root / 'evidence/previous-input.json', {'input': original})
    changed = {**original, change: 'different'}
    monkeypatch.setattr(release, 'metadata', lambda *_: {'version': '1.0.2', 'github': 'example/repo'})
    monkeypatch.setattr(release, 'check_source', lambda *_: None)
    monkeypatch.setattr(release, 'git', lambda *_: 'a' * 40)
    monkeypatch.setattr(release, 'verify_candidate', lambda *_: {'source': 'a' * 40, 'previous_tag': 'v1.0.1', 'tools': {}})
    monkeypatch.setattr(release, 'tools_snapshot', lambda: {})
    monkeypatch.setattr(release, 'previous_input', lambda *_: (changed, None, None))
    monkeypatch.setattr(release, 'run', lambda *_a, **_kw: pytest.fail('No build or external write'))
    with pytest.raises(release.ReleaseError, match='Frozen previous input changed'):
        release.candidate(argparse.Namespace(source='a' * 40, previous_tag='v1.0.1', output=root,
                          previous_published_manifest=None))


@pytest.mark.skipif(not all(os.environ.get(key) for key in ('VENUS_RELEASE_TEST_PREVIOUS_MANIFEST',
                    'VENUS_RELEASE_TEST_CURRENT_ZIP', 'VENUS_RELEASE_TEST_SOURCE')),
                    reason='Explicit published input, target ZIP and installed probe runtime required')
def test_published_previous_input_real_signature_and_provider(tmp_path):
    """Run the production input path and updater against retained bytes, without installing an App."""
    manifest = Path(os.environ['VENUS_RELEASE_TEST_PREVIOUS_MANIFEST'])
    current = Path(os.environ['VENUS_RELEASE_TEST_CURRENT_ZIP'])
    source = Path(os.environ['VENUS_RELEASE_TEST_SOURCE'])
    for name in ('scripts/release-smoke.cjs', 'desktop/package-lock.json'):
        assert release.sha256(source / name) == release.sha256(release.ROOT / name)
    info = release.metadata(release.ROOT)
    tag = release.read_json(manifest)['tag']
    old_zip = manifest.parent / 'release-assets' / release.asset_names(tag[1:])['zip']
    originals = [manifest.parent / name for name in ('owner.json', 'candidate-manifest.json',
                                                    'final-manifest.json', 'published-manifest.json')]
    originals.extend((old_zip, current))
    before = {str(p): release.input_file(p) for p in originals}
    directories = {str(p): (p.stat().st_dev, p.stat().st_ino) for p in (manifest.parent, old_zip.parent, current.parent)}
    root = tmp_path / 'tool-check'
    release.create_root(root, release.ROOT, release.git(release.ROOT, 'rev-parse', 'HEAD'))
    result = None
    try:
        app = release.previous_app(root, info['github'], tag, info, manifest)
        copied = root / 'previous' / old_zip.name
        assert copied.stat().st_ino != old_zip.stat().st_ino
        result = release.provider_smoke(root, source, app, current.parent, info)
        assert result['previous_version'] == tag[1:] and result['version'] == info['version']
        assert result['sha256'] == before[str(current)]['sha256'] and not result['native_installer_executed']
    finally:
        after = {str(p): release.input_file(p) for p in originals}
        assert before == after
        assert directories == {str(p): (p.stat().st_dev, p.stat().st_ino) for p in map(Path, directories)}
        release.atomic_json(root / 'evidence/real-input-check.json', {
            'before': before, 'after': after, 'directory_identity': directories, 'provider': result})
        if (root / 'previous').exists():
            assert release.remove_owned(root, 'previous')['removed']
        assert release.resource_state(root) == {'processes': [], 'mounts': []}


@pytest.mark.parametrize('name', ['owner.json', 'candidate-manifest.json', 'final-manifest.json'])
@pytest.mark.parametrize('kind', ['symlink', 'directory'])
def test_previous_related_records_must_be_regular_files(previous_input_files, tmp_path, name, kind):
    manifest, _, _ = previous_input_files
    path = manifest.parent / name
    target = tmp_path / name
    path.rename(target)
    if kind == 'symlink':
        path.symlink_to(target)
    else:
        path.mkdir()
    with pytest.raises(release.ReleaseError):
        release.previous_input('example/repo', 'v1.0.1', manifest)


def test_previous_metadata_error_does_not_fall_back(previous_input_files, monkeypatch):
    manifest, archive, _ = previous_input_files
    original = archive.read_bytes()
    def unavailable(*_):
        raise release.ReleaseError('Metadata unavailable')
    monkeypatch.setattr(release, 'github_release', unavailable)
    with pytest.raises(release.ReleaseError, match='Metadata unavailable'):
        release.previous_input('example/repo', 'v1.0.1', manifest)
    assert archive.read_bytes() == original


def test_previous_read_only_input_does_not_authorize_deletion(previous_input_files):
    manifest, archive, _ = previous_input_files
    owner_file = manifest.parent / 'owner.json'
    owner = release.read_json(owner_file)
    owner['directories']['release-assets']['device'] += 1
    release.atomic_json(owner_file, owner)
    original = archive.read_bytes()
    result = release.remove_owned(manifest.parent, 'release-assets')
    assert not result['removed'] and archive.read_bytes() == original
    assert release.read_json(owner_file) == owner


def test_unfrozen_root_is_preserved_before_previous_input(tmp_path, monkeypatch):
    import argparse
    root = tmp_path / 'failed'
    root.mkdir()
    partial = root / 'partial.zip'
    partial.write_bytes(b'failed download evidence')
    monkeypatch.setattr(release, 'metadata', lambda *_: {'version': '1.0.2', 'github': 'example/repo'})
    monkeypatch.setattr(release, 'check_source', lambda *_: None)
    monkeypatch.setattr(release, 'git', lambda *_: 'a' * 40)
    monkeypatch.setattr(release, 'previous_input', lambda *_: pytest.fail('Must preserve unfinished root'))
    with pytest.raises(release.ReleaseError, match='Unfinished/unknown release root'):
        release.candidate(argparse.Namespace(source='a' * 40, previous_tag='v1.0.1', output=root,
                          previous_published_manifest=None))
    assert partial.read_bytes() == b'failed download evidence'


def test_previous_network_zip_digest_is_checked_before_extraction(previous_input_files, monkeypatch, tmp_path):
    root = tmp_path / 'network'
    release.create_root(root, release.ROOT, 'd' * 40)
    monkeypatch.setattr(release, 'download', lambda _url, target: target.write_bytes(b'corrupted download'))
    monkeypatch.setattr(release, 'extract_zip', lambda *_: pytest.fail('Invalid digest must stop extraction'))
    with pytest.raises(release.ReleaseError, match='bytes mismatch'):
        release.previous_app(root, 'example/repo', 'v1.0.1', {})
    assert not (root / 'evidence/previous-input.json').exists()


@pytest.fixture
def publish_case(tmp_path, monkeypatch, apple_tools):
    """Real publish orchestration and files; only external command/download I/O is replaced."""
    original_run = release.run
    repo = tmp_path / 'repo'
    repo.mkdir()
    for name in ('pyproject.toml', 'CHANGELOG.md', 'frontend/package.json', 'frontend/package-lock.json',
                 'desktop/package.json', 'desktop/package-lock.json', 'desktop/electron-builder.yml',
                 'desktop/build/app-update.yml', 'scripts/release.py', 'scripts/release-smoke.cjs'):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SPEC.origin if name == 'scripts/release.py' else Path(SPEC.origin).parents[1] / name, target)
    (repo / '.gitignore').write_text('desktop/node_modules/\n')
    def local_git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    local_git('init', '-b', 'master')
    local_git('config', 'user.email', 'release-test@example.test')
    local_git('config', 'user.name', 'Release test')
    local_git('add', '.')
    local_git('commit', '-qm', 'Isolated release fixture')
    source = local_git('rev-parse', 'HEAD')
    monkeypatch.setattr(release, 'ROOT', repo)
    info = release.metadata(repo)
    local_git('remote', 'add', 'origin', 'https://github.com/' + info['github'] + '.git')
    root = tmp_path / 'release'
    owner = release.create_root(root, repo, source)
    frozen = release.owned_directory(root, 'source')
    subprocess.run(['git', 'clone', '-q', str(repo), str(frozen)], check=True)
    (frozen / 'desktop/node_modules').mkdir()
    previous = release.owned_directory(root, 'previous')
    (previous / 'input.zip').write_bytes(b'previous package')
    if os.environ.get('VENUS_RELEASE_TEST_PREVIOUS_ZIP'):
        import zipfile
        with zipfile.ZipFile(os.environ['VENUS_RELEASE_TEST_PREVIOUS_ZIP']) as bundle:
            for name in ('Contents/Resources/app.asar', 'Contents/Resources/app-update.yml'):
                target = previous / 'unpacked/Venus.app' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read('Venus.app/' + name))
    assets = release.owned_directory(root, 'candidate')
    for name in release.asset_names(info['version']).values():
        (assets / name).write_bytes(('release fixture ' + name).encode())
    archive = assets / release.asset_names(info['version'])['zip']
    (assets / 'latest-mac.yml').write_text(f"version: {info['version']}\nfiles:\n  - url: {archive.name}\n    sha512: {release.sha512(archive)}\n    size: {archive.stat().st_size}\npath: {archive.name}\nsha512: {release.sha512(archive)}\nreleaseDate: 2026-10-16T00:00:00.000Z\n")
    notice = assets / 'mac-arm64/Venus.app/Contents/Resources/licenses/ffmpeg/CORRESPONDING-SOURCE.md'
    notice.parent.mkdir(parents=True)
    media = assets / release.asset_names(info['version'])['media_source']
    notice.write_text(f"https://github.com/{info['github']}/releases/download/v{info['version']}/{media.name}\n{release.sha256(media)}")
    final = release.owned_directory(root, 'finalize')
    shutil.copytree(assets, final, dirs_exist_ok=True)
    tools = release.tools_snapshot()
    manifest = {'format': 1, **info, 'owner_id': owner['id'], 'source': source, 'tools': tools,
                'notary_profile': 'test-only', 'candidate_inventory': release.inventory(assets),
                'assets': release.asset_records(assets, info['version']), 'evidence': {},
                'previous_inventory': release.inventory(previous), 'node_dependencies': {}}
    release.atomic_json(root / 'candidate-manifest.json', manifest)
    final_manifest = {'candidate_sha256': release.sha256(root / 'candidate-manifest.json'), 'source': source,
                      'inventory': release.inventory(final), 'assets': manifest['assets']}
    release.atomic_json(root / 'final-manifest.json', final_manifest)
    evidence = root / 'evidence/human.txt'
    evidence.write_text('Isolated fixture acceptance, never production approval')
    acceptance = {'candidate_sha256': final_manifest['candidate_sha256'], 'source': source,
                  'reviewer': 'Fixture', 'reviewed_at': '2026-10-16T00:00:00Z',
                  'checks': {key: {'status': 'passed', 'evidence': [{'path': 'evidence/human.txt', 'sha256': release.sha256(evidence)}]}
                             for key in release.HUMAN_CHECKS}}
    release.atomic_json(root / 'acceptance.json', acceptance)
    execution = {'release_sha256': release.sha256(repo / 'scripts/release.py'),
                 'probe_sha256': release.sha256(repo / 'scripts/release-smoke.cjs'),
                 'python': sys.version, 'executable': str(Path(sys.executable).resolve())}
    progress = {'format': 2, 'execution': execution, 'candidate_sha256': final_manifest['candidate_sha256'],
                'notary': {name: {'sha256': manifest['assets'][name]['sha256'], 'id': 'accepted-' + kind, 'state': 'accepted'}
                           for kind, name in release.asset_names(info['version']).items() if kind in ('zip', 'dmg')}}
    release.atomic_json(root / 'progress.json', progress)
    remote_bytes = {name: (assets / name).read_bytes() for name in manifest['assets']}
    remote = {'release': None, 'tag': None, 'downloads': [], 'writes': [], 'fail_at': 3}
    def command(args, **kwargs):
        args = [str(a) for a in args]
        if args[0] == 'node' and 'provider' in args:
            import http.server
            import threading
            class GithubHTTP(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.endswith('.atom'):
                        content = f'<feed><entry><link href="https://github.com/{info["github"]}/releases/tag/v{info["version"]}"/><title>Fixture release</title><content>Fixture</content></entry></feed>'.encode()
                    elif self.path.endswith('/latest'):
                        content = json.dumps({'tag_name': 'v' + info['version']}).encode()
                    else:
                        name = self.path.split('?')[0].rsplit('/', 1)[-1]
                        if name not in remote_bytes:
                            self.send_error(404)
                            return
                        content = remote_bytes[name]
                        if name.endswith('.zip'):
                            remote['provider_downloads'] = remote.get('provider_downloads', 0) + 1
                            remote['provider_bytes'] = remote.get('provider_bytes', 0) + len(content)
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                def log_message(self, *_args):
                    pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), GithubHTTP)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            preload = tmp_path / 'github-http.cjs'
            preload.write_text("const https=require('https'), http=require('http'); https.request=(options,callback)=>{ if(!['github.com','api.github.com'].includes(options.hostname)) throw Error('Unexpected remote'); return http.request({...options,protocol:'http:',hostname:'127.0.0.1',host:'127.0.0.1',port:" + str(server.server_port) + "},callback); };\n")
            thread.start()
            try:
                dependency_source = Path(os.environ.get('VENUS_RELEASE_TEST_SOURCE', str(Path(__file__).parents[1])))
                result = REAL_RUN([args[0], '--require', preload, *args[1:]], **{**kwargs,
                    'env': {**kwargs['env'], 'NODE_PATH': str(dependency_source / 'desktop/node_modules')}})
                release.atomic_json(tmp_path / 'observed-remote.json', remote)
                if remote.get('lost_response') == 'provider':
                    raise release.ReleaseError('Provider stdout lost', category='unknown')
                return result
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        if args[0] == 'git' and '-C' in args:
            if 'ls-remote' in args:
                return ((source if 'refs/heads/master' in args else remote['tag']) or '')
            if 'push' in args:
                remote['tag'] = local_git('rev-parse', 'refs/tags/v' + info['version'])
                remote['writes'].append('tag')
                return ''
            return subprocess.check_output(args, text=True).strip()
        if args[:2] == ['gh', 'api'] and '--slurp' in args:
            return json.dumps([[remote['release']] if remote['release'] else []])
        if args[:2] == ['gh', 'api'] and 'POST' in args:
            remote['release'] = {**json.loads(kwargs['input_text']), 'id': 7, 'assets': [], 'html_url': 'https://example.test/release'}
            remote['writes'].append('create')
            return json.dumps(remote['release'])
        if args[:2] == ['gh', 'api'] and 'PATCH' in args:
            remote['release']['draft'] = False
            remote['writes'].append('publish')
            if remote.get('lost_response') == 'publish':
                raise release.ReleaseError('Publication response lost', category='external-command')
            return '{}'
        if args[:3] == ['gh', 'release', 'upload']:
            name = Path(args[4]).name
            facts = manifest['assets'][name]
            remote['release']['assets'].append({'id': len(remote['release']['assets']) + 100, 'name': name,
                'size': facts['size'], 'digest': 'sha256:' + facts['sha256'], 'updated_at': '2026-10-16T00:00:00Z',
                'browser_download_url': 'https://example.test/' + name})
            remote['writes'].append('upload:' + name)
            if remote.get('lost_response') == 'upload':
                raise release.ReleaseError('Upload response lost', category='external-command')
            return ''
        if args[:3] == ['gh', 'release', 'download']:
            name = args[args.index('--pattern') + 1]
            remote['downloads'].append(('authenticated', name))
            release.atomic_json(tmp_path / 'observed-remote.json', remote)
            if len(remote['downloads']) == remote['fail_at']:
                if remote.get('signal'):
                    import signal
                    if remote.get('full_download'):
                        (Path(args[args.index('--dir') + 1]) / name).write_bytes(remote_bytes[name])
                        remote['authenticated_bytes'] = remote.get('authenticated_bytes', 0) + len(remote_bytes[name])
                        release.atomic_json(tmp_path / 'observed-remote.json', remote)
                    os.kill(os.getpid(), getattr(signal, remote['signal']))
                raise release.ReleaseError('Injected external download failure')
            (Path(args[args.index('--dir') + 1]) / name).write_bytes(remote_bytes[name])
            remote['authenticated_bytes'] = remote.get('authenticated_bytes', 0) + len(remote_bytes[name])
            release.atomic_json(tmp_path / 'observed-remote.json', remote)
            return ''
        return original_run(args, **kwargs)
    def fetch(url, destination):
        name = url.rsplit('/', 1)[-1]
        remote['downloads'].append(('anonymous', name))
        release.atomic_json(tmp_path / 'observed-remote.json', remote)
        if len(remote['downloads']) == remote['fail_at']:
            raise release.ReleaseError('Injected external download failure')
        destination.write_bytes(remote_bytes[name])
        remote['anonymous_bytes'] = remote.get('anonymous_bytes', 0) + len(remote_bytes[name])
        release.atomic_json(tmp_path / 'observed-remote.json', remote)
    monkeypatch.setattr(release, 'run', command)
    monkeypatch.setattr(release, 'download', fetch)
    args = ['publish', '--candidate', str(root / 'candidate-manifest.json'), '--acceptance', str(root / 'acceptance.json'),
            '--confirm-version', 'v' + info['version']]
    return root, remote, args


def test_publish_resumes_completed_asset_checks(publish_case):
    root, remote, args = publish_case
    assert release.main(args) == 1
    first = remote['downloads'][:2]
    assert len(first) == 2
    remote['fail_at'] = len(remote['downloads']) + 2
    assert release.main(args) == 1
    assert all(remote['downloads'].count(item) == 1 for item in first)
    assert len(list((root / 'evidence').glob('*.complete.json'))) >= 3


def test_anonymous_checks_resume_and_adopt_atomic_result(publish_case):
    root, remote, args = publish_case
    remote['fail_at'] = 9
    assert release.main(args) == 1
    assert len(remote['downloads']) == 9
    state = release.read_json(root / 'progress.json')
    first = next(v for k, v in state['checks'].items() if k.startswith('anonymous:'))
    del first['attempts'][-1]['result']  # Crash after atomic evidence, before progress replacement.
    release.atomic_json(root / 'progress.json', state)
    remote['fail_at'] = 11
    assert release.main(args) == 1
    assert len(remote['downloads']) == 11
    assert len(set(remote['downloads'][:8])) == 8
    assert all(remote['downloads'].count(item) == 1 for item in remote['downloads'][:8])
    assert remote['writes'].count('publish') == 1


@pytest.mark.parametrize('change', ['asset_id', 'digest', 'receipt', 'acceptance', 'final', 'probe'])
def test_resume_rejects_changed_identity_before_more_transfers(publish_case, change):
    root, remote, args = publish_case
    assert release.main(args) == 1
    before = list(remote['downloads'])
    if change in ('asset_id', 'digest'):
        remote['release']['assets'][0]['id' if change == 'asset_id' else 'digest'] = 999 if change == 'asset_id' else 'sha256:' + '0' * 64
    elif change == 'receipt':
        next((root / 'evidence').glob('*.complete.json')).write_text('{}')
    elif change == 'acceptance':
        data = release.read_json(root / 'acceptance.json')
        data['reviewer'] = 'Changed reviewer'
        release.atomic_json(root / 'acceptance.json', data)
    elif change == 'final':
        with (root / 'final-manifest.json').open('a') as stream:
            stream.write(' ')
    else:
        (release.ROOT / 'scripts/release-smoke.cjs').write_text('// changed')
    assert release.main(args) == 1
    assert remote['downloads'] == before


def test_missing_remote_digest_rechecks_only_that_asset(publish_case):
    root, remote, args = publish_case
    assert release.main(args) == 1
    first, second = remote['downloads'][:2]
    remote['release']['assets'][0]['digest'] = None
    remote['fail_at'] = len(remote['downloads']) + 2
    assert release.main(args) == 1
    assert remote['downloads'].count(first) == 2
    assert remote['downloads'].count(second) == 1


@pytest.mark.skipif(not os.environ.get('VENUS_RELEASE_TEST_PREVIOUS_ZIP'), reason='Retained previous release supplied for real updater verification')
@pytest.mark.parametrize('recovery', ['complete', 'partial', 'missing-discovery', 'wrong-target'])
def test_provider_saves_result_before_stdout_and_recovers_download(tmp_path, recovery):
    import zipfile
    source = Path(os.environ.get('VENUS_RELEASE_TEST_SOURCE', str(Path(__file__).parents[1])))
    app = tmp_path / 'previous/Venus.app'
    archive = Path(os.environ['VENUS_RELEASE_TEST_PREVIOUS_ZIP'])
    with zipfile.ZipFile(archive) as bundle:
        for name in ('Contents/Resources/app.asar', 'Contents/Resources/app-update.yml'):
            target = app / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read('Venus.app/' + name))
    feed = tmp_path / 'feed'
    feed.mkdir()
    zip_file = feed / 'Venus-9.0.0-arm64-mac.zip'
    zip_file.write_bytes(b'local transport verification\n' * 100)
    digest = release.sha512(zip_file)
    (feed / 'latest-mac.yml').write_text(f'version: 9.0.0\nfiles:\n  - url: {zip_file.name}\n    sha512: {digest}\n    size: {zip_file.stat().st_size}\npath: {zip_file.name}\nsha512: {digest}\nreleaseDate: 2026-10-16T00:00:00.000Z\n')
    scope = tmp_path / 'probe'
    scope.mkdir()
    result = tmp_path / 'probe.complete.json'
    identity = {'expected': {'name': zip_file.name, 'size': zip_file.stat().st_size, 'sha256': release.sha256(zip_file), 'sha512': digest},
                'metadata_sha256': release.sha256(feed / 'latest-mac.yml'),
                'config_sha256': release.sha256(app / 'Contents/Resources/app-update.yml'),
                'asar_sha256': release.sha256(app / 'Contents/Resources/app.asar')}
    inputs = tmp_path / 'probe.input.json'
    release.atomic_json(inputs, identity)
    probe = Path(__file__).parents[1] / 'scripts/release-smoke.cjs'
    with release.asset_server(feed) as url:
        command = ['node', str(probe), 'provider', str(app), str(source), str(scope), '9.0.0', url, str(inputs), str(result)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        assert result.is_file()
        first = release.read_json(result)
        assert first['result']['sha256'] == release.sha256(zip_file)
        assert first['result']['native_installer_executed'] is False
        result.unlink()  # Completed bytes and durable discovery, but parent never received the result.
        if recovery == 'complete':
            zip_file.unlink()  # A second network download cannot succeed.
        elif recovery == 'partial':
            (scope / 'download.zip').write_bytes(b'partial')
        elif recovery == 'missing-discovery':
            Path(str(result) + '.discovery.json').unlink()
            (scope / 'download.zip').write_bytes(b'no origin')
        else:
            discovery = Path(str(result) + '.discovery.json')
            data = release.read_json(discovery)
            data['version'] = '8.0.0'
            release.atomic_json(discovery, data)
            assert subprocess.run(command, stdout=subprocess.DEVNULL).returncode != 0
            assert not result.exists()
            return
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        recovered = release.read_json(result)
        assert recovered['result']['sha256'] == first['result']['sha256']
        assert recovered['exit_code'] is None
        assert recovered['recovery'] == ('local-content-check' if recovery == 'complete' else 'download')


def test_command_diagnostics_preserve_exit_and_redact_secrets(tmp_path):
    log = tmp_path / 'failure.log'
    output = 'Authorization: Bearer header-secret\nhttps://user:password@example.test/file?signature=signed-secret\nenv-secret'
    with pytest.raises(release.ReleaseError) as error:
        release.run([sys.executable, '-c', 'import sys; print(sys.argv[1]); sys.exit(7)', output],
                    env={**os.environ, 'TEST_TOKEN': 'env-secret'}, log=log)
    assert error.value.exit_code == 7
    assert error.value.category == 'external-command'
    text = log.read_text() + str(error.value)
    assert not any(secret in text for secret in ('header-secret', 'password', 'signed-secret', 'env-secret'))


@pytest.mark.skipif(not os.environ.get('VENUS_RELEASE_TEST_PREVIOUS_ZIP'), reason='Retained updater fixture required')
def test_publish_completion_survives_lost_provider_stdout_and_cleanup(publish_case):
    root, remote, args = publish_case
    remote['fail_at'] = 0
    remote['lost_response'] = 'provider'
    assert release.main(args) == 1
    assert remote['provider_downloads'] == 1
    assert not (root / 'published-manifest.json').exists()
    remote['lost_response'] = None
    with (root / 'source/busy').open('w'):
        assert release.main(args) == 1
        done = release.read_json(root / 'published-manifest.json')
        assert not done['cleanup_complete']
        downloads, writes = list(remote['downloads']), list(remote['writes'])
        assert release.main(args) == 1
        assert remote['downloads'] == downloads and remote['writes'] == writes
    assert release.main(['cleanup', '--root', str(root), '--apply']) == 0
    assert release.main(args) == 0
    assert release.main(args) == 0
    assert remote['provider_downloads'] == 1
    assert remote['downloads'] == downloads and remote['writes'] == writes
    assert release.read_json(root / 'published-manifest.json')['cleanup_complete']
    assert release.asset_records(root / 'release-assets', '1.0.6') == done['assets']


@pytest.mark.parametrize('signal_name,full_download', [('SIGTERM', False), ('SIGKILL', True)])
def test_publish_new_process_resumes_after_real_termination(publish_case, signal_name, full_download):
    import multiprocessing
    import signal
    root, remote, args = publish_case
    remote.update(signal=signal_name, full_download=full_download)
    context = multiprocessing.get_context('fork')
    def invoke():
        raise SystemExit(release.main(args))
    process = context.Process(target=invoke)
    process.start()
    try:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == (-signal.SIGKILL if signal_name == 'SIGKILL' else 1)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    remote.update(release.read_json(root.parent / 'observed-remote.json'))
    first = list(remote['downloads'])
    remote.update(signal=None, fail_at=5 if full_download else 6)
    process = context.Process(target=invoke)
    process.start()
    try:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    observed = release.read_json(root.parent / 'observed-remote.json')
    for item in first[:3 if full_download else 2]:
        assert observed['downloads'].count(item) == 1
    assert len(observed['downloads']) == (5 if full_download else 6)
    if full_download:
        results = [release.read_json(p) for p in (root / 'evidence').glob('*.complete.json')]
        recovered = [r for r in results if r['recovery'] == 'local-content-check']
        assert len(recovered) == 1 and recovered[0]['exit_code'] is None


def test_publish_lost_publication_response_is_reconciled(publish_case):
    _root, remote, args = publish_case
    remote.update(fail_at=0, lost_response='publish')
    assert release.main(args) == 1
    remote.update(fail_at=6, lost_response=None)
    assert release.main(args) == 1
    assert remote['writes'].count('publish') == 1
    assert len(remote['downloads']) == 6


@pytest.mark.skipif(not os.environ.get('VENUS_RELEASE_TEST_PREVIOUS_ZIP'), reason='Retained updater fixture required')
@pytest.mark.parametrize('boundary,after', [('receipt', False), ('receipt', True), ('copy', False), ('copy', True), ('published', False), ('published', True)])
def test_atomic_completion_copy_and_publication_survive_kill(publish_case, boundary, after):
    import multiprocessing
    import signal
    root, remote, args = publish_case
    remote['fail_at'] = 0
    original_replace = os.replace
    def invoke():
        def replace(source, target):
            target = Path(target)
            matches = ((boundary == 'receipt' and target.name.endswith('.complete.json')) or
                       (boundary == 'copy' and target.parent.name == 'release-assets' and not target.name.startswith('.')) or
                       (boundary == 'published' and target.name == 'published-manifest.json'))
            if matches and not after:
                os.kill(os.getpid(), signal.SIGKILL)
            original_replace(source, target)
            if matches:
                os.kill(os.getpid(), signal.SIGKILL)
        os.replace = replace
        raise SystemExit(release.main(args))
    process = multiprocessing.get_context('fork').Process(target=invoke)
    process.start()
    try:
        process.join(timeout=30)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    remote.update(release.read_json(root.parent / 'observed-remote.json'))
    downloaded = list(remote['downloads'])
    copied = {file.name: (file.stat().st_ino, file.stat().st_mtime_ns)
              for file in (root / 'release-assets').glob('*') if file.is_file() and not file.name.startswith('.')}
    exit_code = release.main(args)
    if boundary == 'published' and after:
        assert exit_code == 1
        assert release.main(['cleanup', '--root', str(root), '--apply']) == 0
        assert release.main(args) == 0
    else:
        assert exit_code == 0
    assert all(remote['downloads'].count(item) == 1 for item in downloaded)
    assert all(((root / 'release-assets' / name).stat().st_ino, (root / 'release-assets' / name).stat().st_mtime_ns) == identity
               for name, identity in copied.items())
    assert remote['writes'].count('publish') == 1
    assert remote['provider_downloads'] == 1
    for evidence in (root / 'evidence').glob('*.json'):
        assert isinstance(release.read_json(evidence), dict)


@pytest.mark.parametrize('fault', ['historical', 'notary-unknown', 'unfinished-final', 'unowned-release'])
def test_resume_preserves_explicit_stop_conditions(publish_case, fault):
    root, remote, args = publish_case
    state_path = root / 'progress.json'
    state = release.read_json(state_path)
    if fault == 'historical':
        del state['format']
        release.atomic_json(state_path, state)
    elif fault == 'notary-unknown':
        del next(iter(state['notary'].values()))['id']
        release.atomic_json(state_path, state)
    elif fault == 'unfinished-final':
        (root / 'final-manifest.json').unlink()
    else:
        assert release.main(args) == 1
        remote['release']['body'] = 'Unowned release'
    before_writes = list(remote['writes'])
    before_downloads = list(remote['downloads'])
    assert release.main(args) == 1
    assert remote['writes'] == before_writes and remote['downloads'] == before_downloads
    assert (root / 'finalize').is_dir()


def test_resume_unknown_transfer_directory_is_preserved(publish_case):
    root, remote, args = publish_case
    assert release.main(args) == 1
    state = release.read_json(root / 'progress.json')
    record = list(state['checks'].values())[-1]
    scope = root / record['attempts'][-1]['scope']
    keep = scope / 'user-file'
    keep.write_text('preserve')
    owner = release.read_json(root / 'owner.json')
    del owner['directories'][scope.name]
    release.atomic_json(root / 'owner.json', owner)
    before = list(remote['downloads'])
    assert release.main(args) == 1
    assert remote['downloads'] == before and keep.read_text() == 'preserve'


@pytest.mark.skipif(not os.environ.get('VENUS_RELEASE_TEST_PREVIOUS_ZIP'), reason='Retained updater fixture required')
def test_published_supplementary_check_can_resume_after_failure(publish_case):
    root, remote, args = publish_case
    remote['fail_at'] = 0
    assert release.main(args) == 0
    asset = remote['release']['assets'][0]
    original_digest = asset['digest']
    asset['digest'] = None
    remote['fail_at'] = len(remote['downloads']) + 1
    assert release.main(args) == 1
    assert release.read_json(root / 'published-manifest.json')['cleanup_complete']
    asset['digest'] = original_digest
    remote['fail_at'] = 0
    assert release.main(args) == 0
    assert remote['provider_downloads'] == 1
    assert remote['writes'].count('publish') == 1


def prepare_media_fixture(desktop, cache, version='1.0.1', flavor='default'):
    """Exercise prepareTools with tiny test tools and an identity-only test builder."""
    desktop.mkdir(parents=True)
    identity = {'version': version, 'inputs': {'fixture': flavor}}
    scripts = desktop / 'scripts'
    scripts.mkdir()
    (scripts / 'build-media-tools.py').write_text(
        'import sys\nassert sys.argv[1:] == ["--identity"], "unexpected build"\nprint(' + repr(json.dumps(identity)) + ')\n')
    directory = desktop / 'vendor/media-tools/darwin-arm64'
    (directory / 'licenses').mkdir(parents=True)
    manifest = {'identity': identity, 'outputs': {}, 'tools': {}}
    for name in ('ffmpeg', 'ffprobe'):
        version_line = name + ' version 9.0.1 fixture\n'
        file = directory / name
        file.write_text('#!/bin/sh\nif [ "$1" = "-L" ]; then echo "GNU General Public License version 2"; else echo "' + version_line.strip() + '"; fi\n')
        file.chmod(0o755)
        manifest['tools'][name] = {'version': version_line}
    for name in ('media-sources.tar.gz', 'licenses/SOURCE.md', 'licenses/COMPONENTS.md', 'licenses/GPL-2.0.txt', 'licenses/CORRESPONDING-SOURCE.md'):
        (directory / name).write_text('isolated test fixture')
    for file in directory.rglob('*'):
        if file.is_file():
            manifest['outputs'][str(file.relative_to(directory))] = {'size': file.stat().st_size, 'sha256': release.sha256(file)}
    (directory / 'build-manifest.json').write_text(json.dumps(manifest))
    env = {key: value for key, value in os.environ.items() if key not in ('VENUS_RELEASE_MEDIA_CACHE', 'VENUS_RELEASE_ARCHIVE_CACHE', 'NODE_OPTIONS')}
    env['VENUS_RELEASE_MEDIA_CACHE'] = str(cache / 'media')
    result = subprocess.run(['node', '-e', 'require(process.argv[1]).prepareTools(process.argv[2])',
                             str(Path(__file__).parents[1] / 'desktop/scripts/prepare-media-tools.js'), str(desktop)],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return next(key for key, facts in release.read_json(cache / 'media/entries.json').items() if facts['version'] == version)


def media_cleanup_case(tmp_path):
    root = tmp_path / 'release'
    owner = release.create_root(root, release.ROOT, 'a' * 40)
    release.atomic_json(root / 'candidate-manifest.json', {'source': owner['source'], 'owner_id': owner['id']})
    assets = release.owned_directory(root, 'release-assets')
    for name in release.asset_names('1.0.3').values():
        (assets / name).write_text('isolated formal asset fixture')
    release.atomic_json(root / 'published-manifest.json', {
        'source': owner['source'], 'tag': 'v1.0.3', 'github': 'test/repo', 'release_id': 3,
        'candidate_sha256': release.sha256(root / 'candidate-manifest.json'), 'assets': release.asset_records(assets, '1.0.3'),
    })
    cache = release.prepare_download_cache(tmp_path, release.ROOT)
    key = prepare_media_fixture(tmp_path / 'fixture-desktop', cache)
    return root, cache, key


def test_cleanup_media_preview_then_apply_through_real_node(tmp_path):
    root, cache, key = media_cleanup_case(tmp_path)
    index_before = (cache / 'media/entries.json').read_bytes()
    entry = cache / 'media' / key
    before = release.inventory(entry)
    plan = release.cleanup(root)
    item = next(item for item in plan['media_cache']['items'] if item['key'] == key)
    assert item['action'] == 'delete' and item['version'] == '1.0.1' and item['bytes_before'] > 0
    assert (cache / 'media/entries.json').read_bytes() == index_before
    assert release.inventory(entry) == before
    report = release.cleanup(root, apply=True)
    assert report['cleanup_complete']
    assert next(item for item in report['cache']['media']['items'] if item['key'] == key)['action'] == 'deleted'
    assert not entry.exists() and release.read_json(cache / 'media/entries.json') == {}
    assert release.cleanup(root, apply=True)['cleanup_complete']


def media_cleanup_command(root, *, apply=False, env=None):
    args = [sys.executable, str(Path(__file__).parents[1] / 'scripts/release.py'), 'cleanup', '--root', str(root)]
    if apply:
        args.append('--apply')
    process = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    return process.returncode, json.loads(process.stdout)


def test_media_cleanup_keeps_two_releases_and_active_versions_and_unknowns(tmp_path):
    root, cache, expired = media_cleanup_case(tmp_path)
    for version in ('1.0.1', '1.0.2'):
        older = tmp_path / version
        owner = release.create_root(older, release.ROOT, 'a' * 40)
        release.atomic_json(older / 'candidate-manifest.json', {'source': owner['source'], 'owner_id': owner['id']})
        assets = release.owned_directory(older, 'release-assets')
        for name in release.asset_names(version).values():
            (assets / name).write_text(version)
        records = release.asset_records(assets, version)
        release.atomic_json(older / 'published-manifest.json', {
            'source': owner['source'], 'tag': 'v' + version, 'github': 'test/repo', 'release_id': version,
            'candidate_sha256': release.sha256(older / 'candidate-manifest.json'), 'assets': records,
        })
        if version == '1.0.1':
            # This historical release has already completed public recovery and local cleanup.
            release.atomic_json(older / 'public-recovery.json', {'release_id': version, 'assets': records})
            shutil.rmtree(assets)
    active = tmp_path / 'active'
    owner = release.create_root(active, release.ROOT, 'a' * 40)
    release.atomic_json(active / 'owner.json', {**owner, 'version': '1.0.4'})
    for version, flavor in [('1.0.2', 'a'), ('1.0.2', 'b'), ('1.0.3', 'a'), ('1.0.4', 'a')]:
        prepare_media_fixture(tmp_path / ('fixture-' + version + flavor), cache, version, flavor)
    unknown = cache / 'media' / ('f' * 64)
    unknown.mkdir()
    (unknown / 'keep').write_text('unregistered')
    before = release.inventory(cache / 'media')
    original_index = (cache / 'media/entries.json').read_bytes()
    code, plan = media_cleanup_command(root)
    assert code == 0
    assert release.inventory(cache / 'media') == before
    assert (cache / 'media/entries.json').read_bytes() == original_index
    items = plan['media_cache']['items']
    assert len([item for item in items if item['action'] == 'keep']) == 4
    assert next(item for item in items if item['key'] == expired)['action'] == 'delete'
    assert next(item for item in items if item['path'] == str(unknown))['action'] == 'unknown'
    retained = {key: release.inventory(cache / 'media' / key) for key in release.read_json(cache / 'media/entries.json') if key != expired}
    code, report = media_cleanup_command(root, apply=True)
    assert code == 0 and report['cleanup_complete']
    assert len(release.read_json(cache / 'media/entries.json')) == 4
    for key, facts in retained.items():
        assert release.inventory(cache / 'media' / key) == facts
    assert (unknown / 'keep').read_text() == 'unregistered'
    (tmp_path / 'media-retention-report.json').write_text(json.dumps(report, indent=2))


@pytest.mark.parametrize('fault', ['device', 'replaced', 'entry_link', 'index_link', 'root_link', 'escape', 'bad_index', 'bad_entry', 'index_fifo'])
def test_media_cleanup_rejects_unsafe_ownership_and_preserves_external_files(tmp_path, fault):
    root, cache, key = media_cleanup_case(tmp_path)
    entry = cache / 'media' / key
    index = cache / 'media/entries.json'
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'sentinel').write_text('keep original')
    if fault == 'device':
        records = release.read_json(index)
        records[key]['device'] += 1
        release.atomic_json(index, records)
    elif fault in ('replaced', 'entry_link'):
        entry.rename(tmp_path / 'original-entry')
        if fault == 'replaced':
            entry.mkdir()
            (entry / 'replacement').write_text('not owned')
        else:
            entry.symlink_to(external, target_is_directory=True)
    elif fault == 'index_link':
        index.rename(external / 'entries.json')
        index.symlink_to(external / 'entries.json')
    elif fault == 'root_link':
        (cache / 'media').rename(tmp_path / 'original-media')
        (cache / 'media').symlink_to(tmp_path / 'original-media', target_is_directory=True)
    elif fault == 'escape':
        (entry / 'escaped').symlink_to(external, target_is_directory=True)
    elif fault == 'bad_index':
        index.write_text('{')
    elif fault == 'index_fifo':
        index.unlink()
        os.mkfifo(index)
    else:
        records = release.read_json(index)
        records[key]['inode'] = 'invalid'
        release.atomic_json(index, records)
    original_index = index.read_bytes() if fault != 'index_fifo' else index.lstat().st_ino
    code, plan = media_cleanup_command(root)
    assert code == 1 and not plan['media_cache']['complete']
    code, report = media_cleanup_command(root, apply=True)
    assert code == 1 and not report['cleanup_complete']
    assert (index.read_bytes() if fault != 'index_fifo' else index.lstat().st_ino) == original_index
    assert entry.exists() and (external / 'sentinel').read_text() == 'keep original'


def test_media_cleanup_refuses_real_occupancy_but_saves_other_progress(tmp_path):
    import select

    root, cache, key = media_cleanup_case(tmp_path)
    free_key = prepare_media_fixture(tmp_path / 'other-desktop', cache, '1.0.0')
    file = cache / 'media' / key / 'vendor/media-tools/darwin-arm64/ffprobe'
    holder = subprocess.Popen([sys.executable, '-c',
        'import sys; f=open(sys.argv[1]); print("ready", flush=True); sys.stdin.read()', str(file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([holder.stdout], [], [], 5)[0] and holder.stdout.readline().strip() == 'ready'
        code, report = media_cleanup_command(root, apply=True)
        assert code == 1 and not report['cleanup_complete']
        items = {item['key']: item for item in report['cache']['media']['items']}
        assert items[key]['action'] == 'refused' and 'occupied' in items[key]['reason']
        assert items[free_key]['action'] == 'deleted' and items[free_key]['index_saved']
        assert set(release.read_json(cache / 'media/entries.json')) == {key}
    finally:
        holder.communicate(timeout=5)
        assert holder.returncode == 0
    code, report = media_cleanup_command(root, apply=True)
    assert code == 0 and report['cleanup_complete']
    assert [item['key'] for item in report['cache']['media']['items']] == [key]
    assert release.read_json(cache / 'media/entries.json') == {}


@pytest.mark.parametrize('boundary', ['before', 'partial', 'deleted', 'replaced'])
def test_media_cleanup_new_process_recovers_after_real_node_termination(tmp_path, boundary):
    import select
    import signal

    root, cache, key = media_cleanup_case(tmp_path)
    entry = cache / 'media' / key
    index_before = (cache / 'media/entries.json').read_bytes()
    fifo = tmp_path / 'checkpoint'
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    injection = tmp_path / 'interrupt.cjs'
    injection.write_text('''const fs = require('node:fs');
const original = fs.rmSync;
fs.rmSync = function(file, options) {
  if (file === TARGET) {
    if (BOUNDARY === 'partial') fs.unlinkSync(file + '/vendor/media-tools/darwin-arm64/ffprobe');
    if (['deleted', 'replaced'].includes(BOUNDARY)) original(file, options);
    fs.writeFileSync(FIFO, String(process.pid));
    process.kill(process.pid, 'SIGSTOP');
  }
  return original(file, options);
};
'''.replace('TARGET', json.dumps(str(entry))).replace('BOUNDARY', json.dumps(boundary)).replace('FIFO', json.dumps(str(fifo))))
    env = {**os.environ, 'NODE_OPTIONS': '--require=' + str(injection)}
    process = subprocess.Popen([sys.executable, str(Path(__file__).parents[1] / 'scripts/release.py'),
        'cleanup', '--root', str(root), '--apply'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    node_pid = None
    try:
        assert select.select([fd], [], [], 10)[0]
        node_pid = int(os.read(fd, 128))
        os.kill(node_pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 1, (stdout, stderr)
        assert not json.loads(stdout)['cleanup_complete']
    finally:
        os.close(fd)
        if process.poll() is None:
            if node_pid:
                os.kill(node_pid, signal.SIGKILL)
            process.kill()
            process.communicate(timeout=5)
    assert (cache / 'media/entries.json').read_bytes() == index_before
    if boundary == 'replaced':
        entry.mkdir()
        (entry / 'sentinel').write_text('replacement')
    code, report = media_cleanup_command(root, apply=True)
    if boundary == 'replaced':
        assert code == 1 and not report['cleanup_complete']
        assert (entry / 'sentinel').read_text() == 'replacement'
        assert (cache / 'media/entries.json').read_bytes() == index_before
    else:
        assert code == 0 and report['cleanup_complete']
        assert not entry.exists() and release.read_json(cache / 'media/entries.json') == {}


def test_media_cleanup_stops_after_index_save_failure_then_retries(tmp_path):
    root, cache, first = media_cleanup_case(tmp_path)
    second = prepare_media_fixture(tmp_path / 'second-desktop', cache, '1.0.0')
    injection = tmp_path / 'fail-index.cjs'
    injection.write_text('''const fs = require('node:fs');
const rename = fs.renameSync;
fs.renameSync = function(from, to) {
  if (to === INDEX) throw new Error('test index save failure');
  return rename(from, to);
};
'''.replace('INDEX', json.dumps(str(cache / 'media/entries.json'))))
    code, report = media_cleanup_command(root, apply=True, env={**os.environ, 'NODE_OPTIONS': '--require=' + str(injection)})
    assert code == 1 and not report['cleanup_complete']
    items = report['cache']['media']['items']
    assert items[0]['key'] == first and items[0]['action'] == 'deleted' and not items[0]['index_saved']
    assert items[1]['key'] == second and items[1]['action'] == 'pending'
    assert (cache / 'media' / second).is_dir()
    assert set(release.read_json(cache / 'media/entries.json')) == {first, second}
    code, report = media_cleanup_command(root, apply=True)
    assert code == 0 and report['cleanup_complete']
    assert release.read_json(cache / 'media/entries.json') == {}


@pytest.mark.parametrize('response', ['nonzero', 'invalid_json', 'missing_fields', 'invalid_item', 'invalid_action'])
def test_cleanup_reports_node_protocol_failure_without_deleting_media(tmp_path, response):
    root, cache, key = media_cleanup_case(tmp_path)
    injection = tmp_path / 'protocol.cjs'
    body = {
        'nonzero': "console.error('test command failure'); process.exit(7);",
        'invalid_json': "console.log('not json'); process.exit(0);",
        'missing_fields': "console.log('{}'); process.exit(0);",
        'invalid_item': "const r = JSON.parse(require('node:fs').readFileSync(0, 'utf8')); console.log(JSON.stringify({...r, complete:true, items:[{}]})); process.exit(0);",
        'invalid_action': "const r = JSON.parse(require('node:fs').readFileSync(0, 'utf8')); console.log(JSON.stringify({...r, complete:true, items:[{key:null,path:r.root,version:null,bytes_before:0,action:[],reason:''}]})); process.exit(0);",
    }[response]
    injection.write_text(body)
    before = release.inventory(cache / 'media')
    code, report = media_cleanup_command(root, apply=True, env={**os.environ, 'NODE_OPTIONS': '--require=' + str(injection)})
    assert code == 1 and not report['cleanup_complete'] and report['cache']['reason']
    assert release.inventory(cache / 'media') == before
    assert (cache / 'media' / key).is_dir()


def test_cleanup_requires_available_real_occupancy_checker(tmp_path):
    root, cache, key = media_cleanup_case(tmp_path)
    bin_dir = tmp_path / 'restricted-bin'
    bin_dir.mkdir()
    for tool in ('node', 'git'):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    code, report = media_cleanup_command(root, apply=True, env={**os.environ, 'PATH': str(bin_dir)})
    assert code == 1 and not report['cleanup_complete']
    assert 'occupancy check failed' in report['cache']['media']['items'][0]['reason']
    assert (cache / 'media' / key).is_dir()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS mounted media cache boundary')
def test_media_cleanup_refuses_real_mount_without_detaching_it(tmp_path):
    root, cache, key = media_cleanup_case(tmp_path)
    data = tmp_path / 'image-data'
    data.mkdir()
    (data / 'sentinel').write_text('mounted original')
    image = tmp_path / 'test.dmg'
    subprocess.run(['hdiutil', 'create', '-srcfolder', str(data), '-format', 'UDZO', str(image)], check=True, capture_output=True)
    mount = cache / 'media' / key / 'mounted'
    mount.mkdir()
    subprocess.run(['hdiutil', 'attach', '-nobrowse', '-readonly', '-mountpoint', str(mount), str(image)], check=True, capture_output=True)
    try:
        assert os.path.ismount(mount)
        code, report = media_cleanup_command(root, apply=True)
        assert code == 1 and not report['cleanup_complete']
        assert 'Mounted' in report['cache']['media']['items'][0]['reason']
        assert os.path.ismount(mount) and (mount / 'sentinel').read_text() == 'mounted original'
    finally:
        subprocess.run(['hdiutil', 'detach', str(mount)], check=True, capture_output=True)
    assert not os.path.ismount(mount)


def test_media_cleanup_partial_delete_error_keeps_registration_and_continues(tmp_path):
    root, cache, first = media_cleanup_case(tmp_path)
    second = prepare_media_fixture(tmp_path / 'second-desktop', cache, '1.0.0')
    entry = cache / 'media' / first
    injection = tmp_path / 'partial.cjs'
    injection.write_text('''const fs = require('node:fs');
const remove = fs.rmSync;
fs.rmSync = function(file, options) {
  if (file === TARGET) {
    fs.unlinkSync(file + '/vendor/media-tools/darwin-arm64/ffprobe');
    throw new Error('test partial deletion failure');
  }
  return remove(file, options);
};
'''.replace('TARGET', json.dumps(str(entry))))
    code, report = media_cleanup_command(root, apply=True, env={**os.environ, 'NODE_OPTIONS': '--require=' + str(injection)})
    assert code == 1 and not report['cleanup_complete']
    items = {item['key']: item for item in report['cache']['media']['items']}
    assert items[first]['action'] == 'refused' and 'partial deletion failure' in items[first]['reason']
    assert items[second]['action'] == 'deleted' and items[second]['index_saved']
    assert set(release.read_json(cache / 'media/entries.json')) == {first}
    assert entry.is_dir() and not (entry / 'vendor/media-tools/darwin-arm64/ffprobe').exists()
    code, report = media_cleanup_command(root, apply=True)
    assert code == 0 and report['cleanup_complete']
    assert not entry.exists() and release.read_json(cache / 'media/entries.json') == {}
