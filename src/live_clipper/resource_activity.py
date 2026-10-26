"""Resource occupation spans work, independently of job and request results."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import asr_models, jobs
from .project_domain import normalize_utc
from .project_storage import ProjectRepository, database_path


def _register(repository: ProjectRepository, task_id: str, resource_id: str | None, model: str | None) -> None:
    repository.connection.execute('INSERT INTO resource_tasks VALUES(?,?,?,?,?,NULL)',
        (task_id, resource_id, model, 'running', normalize_utc()))


def _finish(repository: ProjectRepository, task_id: str) -> None:
    try:
        with repository.transaction():
            repository.connection.execute("UPDATE resource_tasks SET state='finished',finished_at=? WHERE task_id=?",
                (normalize_utc(), task_id))
    except Exception as exc:
        logging.getLogger(__name__).warning('Resource occupation finalization failed: task=%s exception=%s', task_id, type(exc).__name__)
        raise RuntimeError('资源任务状态保存失败，请重新启动应用后检查。') from None


@contextmanager
def occupy(repository: ProjectRepository, *, resource_id: str | None, model: str | None) -> Iterator[str]:
    task_id = uuid.uuid4().hex
    with repository.transaction():
        _register(repository, task_id, resource_id, model)
    try:
        yield task_id
    finally:
        _finish(repository, task_id)


def start_preparation(repository: ProjectRepository, *, resource_id: str, model: str,
                      check_resource: Callable[[], None], fn: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    service_dir = repository.service_dir
    task_id = uuid.uuid4().hex
    registered = False

    def register() -> None:
        nonlocal registered
        with repository.transaction():
            check_resource()
            _register(repository, task_id, resource_id, model)
        registered = True

    def run() -> dict[str, Any]:
        try:
            return fn(task_id)
        finally:
            with ProjectRepository(service_dir) as worker_repository:
                _finish(worker_repository, task_id)

    try:
        return jobs.start_job(service_dir, kind=asr_models.DOWNLOAD_JOB_KIND, run_id=model, fn=run, before_start=register)
    except Exception:
        if registered:
            _finish(repository, task_id)
        raise


def active_tasks(repository: ProjectRepository, resource: dict[str, Any]) -> list[str]:
    model = resource['config'].get('model')
    tasks = [row[0] for row in repository.connection.execute(
        "SELECT task_id FROM resource_tasks WHERE state='running' AND (resource_id=? OR model=?)",
        (resource['resource_id'], model)).fetchall()]
    if resource['kind'] == 'local_asr' and model in asr_models.registry_ids():
        active = jobs.active_job_for(repository.service_dir, model, asr_models.DOWNLOAD_JOB_KIND)
        if active:
            tasks.append(active['id'])
    return tasks


def recover_interrupted(service_dir: Path) -> None:
    jobs.sweep_interrupted(service_dir)
    if database_path(service_dir).exists():
        with ProjectRepository(service_dir) as repository:
            with repository.transaction():
                repository.connection.execute("UPDATE resource_tasks SET state='interrupted',finished_at=datetime('now') WHERE state='running'")
