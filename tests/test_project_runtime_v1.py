from live_clipper import service
from live_clipper.config import PathsConfig, Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_runtime import (
    dispatch_queued,
    ensure_retention_confirmations,
    recover_processing,
    run_work_dir,
)
from live_clipper.project_service import ProjectManager, open_project_repository
from live_clipper.utils import write_json


def test_fifo_dispatch_failure_does_not_block_next_and_recovery_keeps_run_id(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    repo = open_project_repository(tmp_path / "service")
    project = ProjectManager(repo, Settings(cheap_model_api_key="fake")).create_project(
        name="P", config=default_project_config(source, output), activation_state="active"
    )
    failed = repo.create_normal_run(
        project_id=project.project_id,
        content_id="first",
        trigger_source="manual",
        first_seen_path=str(source / "first.mp4"),
        latest_seen_path=str(source / "first.mp4"),
        parameter_snapshot={"source": {"relative_path": "first.mp4", "bytes": 1}},
        queued_at="2026-08-20T00:00:00Z",
    ).run
    started = repo.create_normal_run(
        project_id=project.project_id,
        content_id="second",
        trigger_source="manual",
        first_seen_path=str(source / "second.mp4"),
        latest_seen_path=str(source / "second.mp4"),
        parameter_snapshot={},
        queued_at="2026-08-20T00:00:01Z",
    ).run

    def processor(run, _run_dir):
        if run.run_id == failed.run_id:
            raise RuntimeError("start failed")
        return None

    report = dispatch_queued(repo, work_dir=tmp_path / "work", processor=processor, capacity=2)
    assert report.failed_run_ids == (failed.run_id,)
    assert report.started_run_ids == (started.run_id,)
    assert repo.get_run(failed.run_id).status == "failed"
    assert repo.get_run(started.run_id).status == "processing"
    recovered = recover_processing(repo, validator=lambda _run, _run_dir: False, work_dir=tmp_path / "work")
    assert recovered.failed_run_ids == (started.run_id,)
    assert repo.get_run(started.run_id).run_id == started.run_id


def test_retention_confirmation_deletes_only_allowlisted_project_intermediates(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    work = tmp_path / "work"
    source.mkdir()
    output.mkdir()
    repo = open_project_repository(tmp_path / "service")
    project = ProjectManager(repo, Settings(cheap_model_api_key="fake")).create_project(
        name="P", config=default_project_config(source, output), activation_state="active"
    )
    snapshot = {"output": {"intermediate_retention": "remind_immediately"}}
    run = repo.create_normal_run(
        project_id=project.project_id,
        content_id="cleanup",
        trigger_source="manual",
        first_seen_path=str(source / "recording.mp4"),
        latest_seen_path=str(source / "recording.mp4"),
        parameter_snapshot=snapshot,
    ).run
    settings = Settings(paths=PathsConfig(work_dir=work))
    target = run_work_dir(work, run)
    local_source = target / "input" / "recording.mp4"
    original_source = source / "recording.mp4"
    local_source.parent.mkdir(parents=True)
    original_source.write_bytes(b"original")
    local_source.write_bytes(b"local")
    (target / "audio.wav").write_bytes(b"audio")
    (target / "clips").mkdir()
    final_clip = target / "clips" / "clip.mp4"
    final_clip.write_bytes(b"final")
    write_json(
        target / "run_metadata.json",
        {"pipeline": {"local_source_path": str(local_source), "original_source_path": str(original_source)}},
    )
    repo.transition_run(run.run_id, status="completed", stage="render", event_type="completed")

    created = ensure_retention_confirmations(repo, settings, service_dir=tmp_path / "service")
    confirmation = service.load_confirmations(tmp_path / "service")[0]
    approved = service.approve_confirmation(
        confirmation["id"], settings=settings, service_dir=tmp_path / "service"
    )

    assert created == [run.run_id]
    assert approved["ok"]
    assert not local_source.exists() and not (target / "audio.wav").exists()
    assert original_source.exists() and final_clip.exists()
