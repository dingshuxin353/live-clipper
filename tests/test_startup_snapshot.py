from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from live_clipper.first_run_detection import _read_database_facts
from live_clipper.project_domain import default_project_config
from live_clipper.project_storage import ProjectRepository, database_path


@pytest.mark.parametrize('remaining, expected_windows', [(0.0005, []), (0.0019, [1])])
def test_remaining_wait_is_rounded_down_and_submillisecond_budget_stops(tmp_path, monkeypatch, remaining, expected_windows):
    from live_clipper import first_run_detection

    clock = [0.0]
    monkeypatch.setattr(first_run_detection, 'time', SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    path = database_path(tmp_path)
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute('CREATE TABLE facts(value)')
    writer.execute('BEGIN EXCLUSIVE')
    connect = sqlite3.connect
    readers, statements = [], []

    class Reader(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append(sql)
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', 0) == sqlite3.SQLITE_BUSY:
                    clock[0] = 0.9995
                raise

    def nearly_expired_connect(*args, **kwargs):
        reader = connect(*args, **kwargs, factory=Reader)
        readers.append(reader)
        clock[0] = 1.0 - remaining
        return reader

    monkeypatch.setattr(sqlite3, 'connect', nearly_expired_connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match='budget'):
            with first_run_detection.readonly_database(path):
                pytest.fail('an exhausted lock-wait budget must not yield a reader')
        assert [int(sql.rsplit(' ', 1)[1]) for sql in statements if sql.startswith('PRAGMA busy_timeout')] == expected_windows
        assert writer.in_transaction
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            readers[0].execute('SELECT 1')
    finally:
        writer.close()


def test_queries_share_one_budget_and_expired_exit_discards_result(tmp_path, monkeypatch):
    from live_clipper import first_run_detection

    with ProjectRepository(tmp_path):
        pass
    clock = [0.0]
    monkeypatch.setattr(first_run_detection, 'time', SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    with pytest.raises(sqlite3.OperationalError, match='budget'):
        with first_run_detection.readonly_database(database_path(tmp_path)) as reader:
            reader.execute('SELECT 1').fetchone()
            clock[0] = 0.6
            reader.execute('SELECT 2').fetchone()
            clock[0] = 1.01
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        reader.execute('SELECT 1')


def _track_readers(monkeypatch, *, on_busy=None):
    connect = sqlite3.connect
    readers = []

    class Reader(sqlite3.Connection):
        progress = None
        cleared_before_close = False
        busy_count = 0

        def set_progress_handler(self, handler, interval):
            self.progress = (handler, interval)
            return super().set_progress_handler(handler, interval)

        def close(self):
            self.cleared_before_close = self.progress == (None, 0)
            return super().close()

        def execute(self, sql, *args):
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', 0) & 0xff == sqlite3.SQLITE_BUSY:
                    self.busy_count += 1
                    if on_busy is not None:
                        on_busy()
                raise

    def tracked_connect(*args, **kwargs):
        if not kwargs.get('uri') or not str(args[0]).endswith('?mode=ro'):
            return connect(*args, **kwargs)
        reader = connect(*args, **kwargs, factory=Reader)
        readers.append(reader)
        return reader

    monkeypatch.setattr(sqlite3, 'connect', tracked_connect)
    return readers


def _assert_readers_closed(readers):
    assert readers
    for reader in readers:
        assert reader.cleared_before_close
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            reader.execute('SELECT 1')


def test_lock_release_after_real_busy_allows_read_within_budget(tmp_path, monkeypatch):
    from live_clipper.first_run_detection import readonly_database

    path = database_path(tmp_path)
    ready, release = threading.Event(), threading.Event()
    failures = []

    def write():
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute('CREATE TABLE facts(value)')
            writer.execute("INSERT INTO facts VALUES ('committed')")
            writer.execute('BEGIN EXCLUSIVE')
            ready.set()
            assert release.wait(5)
            writer.execute('COMMIT')
        except BaseException as error:
            failures.append(repr(error))
        finally:
            writer.close()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert ready.wait(5)
        readers = _track_readers(monkeypatch, on_busy=release.set)
        started = time.monotonic()
        with readonly_database(path) as reader:
            assert reader.execute('SELECT value FROM facts').fetchone()[0] == 'committed'
        assert time.monotonic() - started < 2
        assert readers[0].busy_count >= 1
        _assert_readers_closed(readers)
    finally:
        release.set()
        writer.join(5)
        assert not writer.is_alive()
    assert not failures


def test_long_sql_is_interrupted_within_shared_budget_and_reader_closed(tmp_path, monkeypatch):
    from live_clipper.first_run_detection import readonly_database

    with ProjectRepository(tmp_path):
        pass
    readers = _track_readers(monkeypatch)
    threads = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        with readonly_database(database_path(tmp_path)) as reader:
            reader.execute(
                'WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n'
            ).fetchone()
    assert time.monotonic() - started < 2
    _assert_readers_closed(readers)
    assert {thread.ident for thread in threading.enumerate()} == threads


def test_empty_database_has_read_snapshot_before_connection_is_yielded(tmp_path):
    from live_clipper.first_run_detection import readonly_database

    path = database_path(tmp_path)
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    writer.execute('VACUUM')
    try:
        with readonly_database(path):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute('BEGIN EXCLUSIVE')
        writer.execute('BEGIN EXCLUSIVE')
        assert writer.in_transaction
    finally:
        writer.close()


def test_committed_wal_is_visible_without_closing_writer(tmp_path):
    with ProjectRepository(tmp_path) as writer:
        writer.set_data_mode('legacy')
    with ProjectRepository(tmp_path) as writer:
        writer.set_data_mode('projects')
        assert _read_database_facts(database_path(tmp_path)).data_mode == 'projects'
        writer.connection.execute('BEGIN IMMEDIATE')
        writer.connection.execute("UPDATE system_state SET value='legacy' WHERE key='data_mode'")
        assert _read_database_facts(database_path(tmp_path)).data_mode == 'projects'
        writer.connection.rollback()
        assert _read_database_facts(database_path(tmp_path)).data_mode == 'projects'
        writer.set_data_mode('legacy')
        assert _read_database_facts(database_path(tmp_path)).data_mode == 'legacy'
    assert _read_database_facts(database_path(tmp_path)).data_mode == 'legacy'


def test_all_queries_share_one_snapshot_and_next_read_observes_commit(tmp_path, monkeypatch):
    with ProjectRepository(tmp_path) as writer:
        writer.set_data_mode('legacy')
    connect = sqlite3.connect
    with ProjectRepository(tmp_path) as writer:
        class Reader(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'SELECT project_id FROM projects ORDER BY project_id':
                    with writer.transaction():
                        if not writer.list_projects():
                            writer.create_project('new', default_project_config(tmp_path/'in', tmp_path/'out'))
                            writer.set_data_mode('projects')
                return super().execute(sql, *args)

        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=Reader))
        facts = _read_database_facts(database_path(tmp_path))
        assert facts.data_mode == 'legacy' and facts.project_ids == ()
        latest = _read_database_facts(database_path(tmp_path))
        assert latest.data_mode == 'projects' and len(latest.project_ids) == 1


@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('kind', ['first_run', 'migration'])
def test_readonly_contract_preserves_schema_data_and_non_auxiliary_files(tmp_path, active, kind):
    from test_first_run_detection import _fingerprint

    from live_clipper.first_run_detection import readonly_database

    writer = ProjectRepository(tmp_path)
    if kind == 'first_run':
        writer.set_data_mode('projects')
        writer.begin_first_run_session()
        writer.create_project('retained', default_project_config(tmp_path/'in', tmp_path/'out'))
    else:
        writer.create_migration_session(
            migration_id='migration-1', source_fingerprint='a'*64, plan_version=3,
            plan_hash='b'*64, source_manifest=[], choices={}, request_id='request-1',
            request_hash='c'*64, backup_path='/backup',
        )
    contents = tuple(writer.connection.iterdump())
    if not active:
        writer.close()
    (tmp_path / 'config.toml').write_text('protected = true')
    before = _fingerprint(tmp_path)
    with readonly_database(database_path(tmp_path)) as reader:
        assert tuple(reader.iterdump()) == contents
        for statement in ('DELETE FROM projects', 'DROP TABLE projects', 'CREATE TABLE surprise(x)'):
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                reader.execute(statement)
    assert _fingerprint(tmp_path) == before
    with readonly_database(database_path(tmp_path)) as reader:
        assert tuple(reader.iterdump()) == contents
    if active:
        writer.set_data_mode('legacy')
        assert _read_database_facts(database_path(tmp_path)).data_mode == 'legacy'
        writer.close()


@pytest.mark.parametrize('case', ['absent', 'corrupt', 'directory', 'symlink', 'wal_symlink', 'shm_symlink', 'wal_directory'])
def test_invalid_database_paths_are_diagnostic_without_repair(tmp_path, case):
    path = database_path(tmp_path)
    if case == 'absent':
        assert _read_database_facts(path).data_mode == 'absent'
        assert not path.exists()
        return
    if case == 'corrupt':
        path.write_bytes(b'not a database')
    elif case == 'directory':
        path.mkdir()
    elif case == 'symlink':
        outside = tmp_path / 'other.db'
        outside.write_bytes(b'protected')
        path.symlink_to(outside)
    else:
        with ProjectRepository(tmp_path):
            pass
        auxiliary = path.with_name(path.name + ('-shm' if case == 'shm_symlink' else '-wal'))
        if case.endswith('directory'):
            auxiliary.mkdir()
        else:
            auxiliary.symlink_to(tmp_path / 'missing')
    assert _read_database_facts(path).unreadable
    assert not (tmp_path / 'missing').exists()


def test_read_lock_timeout_is_bounded_and_does_not_repair(tmp_path, monkeypatch):
    path = database_path(tmp_path)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute('CREATE TABLE facts(value)')
        writer.execute('BEGIN EXCLUSIVE')
        readers = _track_readers(monkeypatch)
        start = time.monotonic()
        assert _read_database_facts(path).unreadable
        assert time.monotonic() - start < 2
        assert writer.in_transaction
        _assert_readers_closed(readers)
    finally:
        writer.close()


def test_unreadable_database_is_diagnostic_without_permission_changes(tmp_path):
    import os

    from live_clipper.first_run_detection import inspect_startup

    if os.geteuid() == 0:
        pytest.skip('root bypasses file permissions')
    with ProjectRepository(tmp_path):
        pass
    path = database_path(tmp_path)
    path.chmod(0)
    try:
        decision = inspect_startup(config_path=tmp_path/'missing.toml', env_path=tmp_path/'missing.env',
                                   service_dir=tmp_path)
        assert decision.entry == 'diagnostic_required'
        assert path.stat().st_mode & 0o777 == 0
    finally:
        path.chmod(0o600)


def test_failed_read_and_cancelled_read_close_connection(tmp_path, monkeypatch):
    from live_clipper.first_run_detection import readonly_database

    with ProjectRepository(tmp_path):
        pass
    readers = _track_readers(monkeypatch)
    for exception in (ValueError, KeyboardInterrupt):
        with pytest.raises(exception), readonly_database(database_path(tmp_path)) as reader:
            reader.execute('SELECT * FROM system_state').fetchall()
            raise exception('stop')
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            reader.execute('SELECT 1')
    _assert_readers_closed(readers)


@pytest.mark.parametrize('route', ['/api/onboarding', '/api/migration'])
def test_http_startup_uses_one_snapshot_for_entry_and_migration(tmp_path, monkeypatch, route):
    import json
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.request import urlopen

    from live_clipper import first_run_detection
    from live_clipper.web import LiveClipperRequestHandler, WebPaths

    with ProjectRepository(tmp_path) as repository:
        original_session = repository.create_migration_session(
            migration_id='migration-1', source_fingerprint='a'*64, plan_version=3,
            plan_hash='b'*64, source_manifest=[], choices={}, request_id='request-1',
            request_hash='c'*64, backup_path='/backup',
        )
    (tmp_path/'runs.json').write_text('{"runs":[]}')
    original = first_run_detection._read_database_facts
    reads = []

    def commit_after_snapshot(path):
        facts = original(path)
        reads.append(facts)
        if len(reads) == 1:
            with ProjectRepository(tmp_path) as writer:
                writer.record_migration_failure(original_session.migration_id, original_session.revision,
                                                failure_code='test_failure', failure_summary='failed')
        return facts

    monkeypatch.setattr(first_run_detection, '_read_database_facts', commit_after_snapshot)
    class Handler(LiveClipperRequestHandler):
        paths = WebPaths(service_dir=tmp_path, config_path=tmp_path/'missing.toml',
                         input_dir=tmp_path/'in', output_root=tmp_path/'out')
        restricted_startup = 'migration_required'

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    keeper = sqlite3.connect(database_path(tmp_path))
    try:
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        with urlopen(f'http://127.0.0.1:{server.server_port}{route}', timeout=5) as response:
            payload = json.load(response)
        if route == '/api/onboarding':
            assert payload['entry']['reason_code'] == 'migration_resume'
            payload = payload['migration']
        assert payload['entry'] == 'executing'
        assert payload['session']['state'] == 'backing_up'
        assert database_path(tmp_path).with_name(database_path(tmp_path).name + '-wal').exists()
        assert len(reads) == 1
        with urlopen(f'http://127.0.0.1:{server.server_port}{route}', timeout=5) as response:
            payload = json.load(response)
        if route == '/api/onboarding':
            assert payload['entry']['reason_code'] == 'migration_retry'
            payload = payload['migration']
        assert payload['entry'] == 'failed'
        assert payload['session']['state'] == 'failed_rolled_back'
        assert len(reads) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        keeper.close()
