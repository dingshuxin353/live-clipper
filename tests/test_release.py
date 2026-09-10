"""Release safety checks use isolated files and injected command failures, never Apple writes."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_signature_runtime_is_parsed_from_codesign_flags():
    details = "Identifier=example\nCodeDirectory v=20500 flags=0x10000(runtime)\nCDHash=" + "a" * 40
    details += "\nAuthority=Developer ID Application: Test\nTimestamp=today\n"
    assert release.signature_facts(details, "example")["hardened_runtime"] is True
    with pytest.raises(release.ReleaseError):
        release.signature_facts(details.replace("0x10000(runtime)", "0x0(none)"), "example")


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
    lock = tmp_path / "frontend/package-lock.json"
    data = json.loads(lock.read_text())
    data["packages"][""]["version"] = "0.0.0"
    lock.write_text(json.dumps(data))
    assert original["version"] != "0.0.0"
    with pytest.raises(release.ReleaseError, match="Seven"):
        release.metadata(tmp_path)


def test_anonymous_download_rejects_non_https_before_writing(tmp_path):
    with pytest.raises(release.ReleaseError):
        release.download("http://example.invalid/asset", tmp_path / "asset")
    assert list(tmp_path.iterdir()) == []


def test_publish_rejects_pending_acceptance_before_tag_or_apple(tmp_path, monkeypatch):
    import argparse

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(release, "ROOT", repo)
    root = tmp_path / "release"
    root.mkdir()
    manifest = root / "candidate-manifest.json"
    manifest.write_text("{}")
    acceptance = root / "acceptance.json"
    acceptance.write_text(json.dumps(release.acceptance_template(manifest, "a" * 40)))
    info = {"version": "1.0.2", "github": "example/repo"}
    monkeypatch.setattr(release, "verify_candidate", lambda *_: {**info, "source": "a" * 40, "tools": {}})
    monkeypatch.setattr(release, "metadata", lambda *_: info)
    monkeypatch.setattr(release, "check_source", lambda *_: None)
    monkeypatch.setattr(release, "tools_snapshot", lambda: {})
    calls = []
    monkeypatch.setattr(release, "ensure_tag", lambda *_: calls.append("tag"))
    with pytest.raises(release.ReleaseError, match="reviewer"):
        release.publish(argparse.Namespace(candidate=manifest, acceptance=acceptance, confirm_version="v1.0.2"))
    assert calls == []


def test_partial_upload_reconciles_existing_bytes_and_only_uploads_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "run", lambda args, **kwargs: calls.append(args) or "")
    verified = []
    monkeypatch.setattr(release, "verify_remote_asset", lambda _info, _tag, name, *_: verified.append(name))
    (tmp_path / "finalize").mkdir()
    names = list(release.asset_names('1.0.2').values())
    for name in names:
        (tmp_path / 'finalize' / name).write_bytes(b'frozen')
    final = {'assets': {name: {'size': 6, 'sha256': release.sha256(tmp_path / 'finalize' / name)} for name in names}}
    release.upload_assets(tmp_path, {'version': '1.0.2', 'github': 'example/repo'}, final,
                          {'draft': True, 'assets': [{'name': name} for name in names[:-1]]}, {}, tmp_path / 'progress.json')
    assert len(calls) == 1 and str(calls[0][4]).endswith(names[-1])
    assert verified == names
    calls.clear()
    del final['assets'][names[-1]]
    with pytest.raises(release.ReleaseError, match='Five'):
        release.upload_assets(tmp_path, {'version': '1.0.2'}, final, {'assets': []}, {}, tmp_path / 'progress.json')
    assert calls == []



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
    args = argparse.Namespace(source="a" * 40, previous_tag="v1.0.2" if failure == "old_version" else "v1.0.1", output=tmp_path / "new-release")
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
    release.candidate(argparse.Namespace(source="a" * 40, previous_tag="v1.0.1", output=root))


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
        release.run([sys.executable, '-c', 'import signal; signal.pause()'], cwd=scope, env=env, timeout=0.2)
    assert release.resource_state(root)['processes'] == []
    process = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], cwd=scope, start_new_session=True)
    try:
        release.remember_process(root, process, scope)
        assert release.recover_resources(root) == []
        process.wait(timeout=2)
        assert release.resource_state(root)['processes'] == []
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


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
        **info, "source": "a" * 40, "previous_tag": "v1.0.3", "tools": frozen})
    monkeypatch.setattr(release, "tools_snapshot", lambda: changed)
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: pytest.fail("No build or external command allowed"))
    monkeypatch.setattr(release, "ensure_tag", lambda *_: pytest.fail("No tag allowed"))
    with pytest.raises(release.ReleaseError, match="Tool environment changed"):
        if operation == "candidate":
            release.candidate(argparse.Namespace(source="a" * 40, previous_tag="v1.0.3", output=root))
        else:
            release.publish(argparse.Namespace(candidate=manifest, acceptance=root / "acceptance.json", confirm_version="v1.0.4"))
    assert sorted(p.name for p in root.iterdir()) == ["candidate-manifest.json"]


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
    environment = release.isolated_env(tmp_path / 'environment', python=identity['executable'])
    environment['PIP_NO_INDEX'] = '1'
    assert Path(shutil.which('python3.11', path=environment['PATH'])).resolve() == real
    target = tmp_path / 'copied'
    release.run([identity['executable'], '-I', '-m', 'venv', '--copies', target], env=environment)
    python = target / 'bin/python'
    assert not python.is_symlink()
    facts = json.loads(release.run([python, '-I', '-c',
        'import encodings,ssl,sqlite3,venv,sys,sysconfig,json; '
        'print(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix,"stdlib":sysconfig.get_path("stdlib")}))'],
        env=environment))
    assert Path(facts['prefix']).resolve() == target.resolve()
    assert facts['base_prefix'] == identity['base_prefix']
    assert facts['stdlib'] == identity['stdlib']
    assert str(target) in release.run([python, '-I', '-m', 'pip', '--version'], env=environment)
    media = json.loads(release.run(['python3.11', '-I', '-c',
        'import sys,json; print(json.dumps({"executable":sys.executable,"base_prefix":sys.base_prefix}))'], env=environment))
    assert media['executable'] == identity['executable']
    assert media['base_prefix'] == identity['base_prefix']
    monkeypatch.setenv('PATH', str(target / 'bin') + os.pathsep + os.environ['PATH'])
    assert release.python_identity() == identity


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



def test_candidate_venv_uses_frozen_python_identity(tmp_path, monkeypatch):
    import argparse

    monkeypatch.setattr(release, 'ROOT', tmp_path / 'repo')
    monkeypatch.setattr(release, 'metadata', lambda *_: {'version': '1.0.4', 'github': 'example/repo'})
    monkeypatch.setattr(release, 'check_source', lambda *_: None)
    monkeypatch.setattr(release, 'git', lambda _repo, *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    monkeypatch.setattr(release, 'github_release', lambda *_: None)
    monkeypatch.setattr(release, 'tools_snapshot', lambda: {'python_identity': {'executable': '/selected/bin/python3.11'}})
    monkeypatch.setattr(release, 'space_preflight', lambda *_: {})
    monkeypatch.setattr(release, 'prune_media_cache', lambda *_: None)
    monkeypatch.setattr(release, 'cleanup_plan', lambda *_: {})
    monkeypatch.setattr(release, 'cache_keep_versions', lambda *_: [])
    purges = []
    monkeypatch.setattr(release, 'limit_download_cache', lambda cache, python: purges.append(python))
    monkeypatch.setenv('VENUS_NOTARY_PROFILE', 'test-only')

    def command(args, **kwargs):
        if args[0] == 'security':
            return '1) ' + 'A' * 40 + ' "Developer ID Application: Test"'
        if 'venv' in args:
            assert args == ['/selected/bin/python3.11', '-I', '-m', 'venv', '--copies', '.venv']
            assert kwargs['env']['PATH'].split(os.pathsep)[0] == '/selected/bin'
            raise release.ReleaseError('Reached selected venv boundary')
        assert args[0] in ('xcrun', 'git')
        return ''

    monkeypatch.setattr(release, 'run', command)
    with pytest.raises(release.ReleaseError, match='Reached selected venv boundary'):
        release.candidate(argparse.Namespace(source='a' * 40, previous_tag='v1.0.3', output=tmp_path / 'release'))
    assert purges == ['/selected/bin/python3.11']
