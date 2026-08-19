from live_clipper.project_projection import project_projection, queue_positions


def test_project_state_priority_and_workload():
    runs = [
        {"run_id": "f", "status": "failed"},
        {"run_id": "r", "status": "awaiting_review"},
        {"run_id": "p", "status": "processing"},
        {"run_id": "q", "status": "queued"},
        {"run_id": "c", "status": "completed"},
    ]
    projected = project_projection(activation_state="paused", runs=runs, blocked=False)
    assert projected.main_status == "failed"
    assert projected.activation_state == "paused"
    assert projected.workload.as_dict() == {
        "processing": 1,
        "queued": 1,
        "awaiting_review": 1,
        "failed": 1,
        "completed": 1,
    }


def test_paused_project_with_processing_work_stays_processing():
    projected = project_projection(
        activation_state="paused",
        runs=[{"run_id": "p", "status": "processing"}],
    )
    assert projected.main_status == "processing"
    assert project_projection(activation_state="paused", runs=[]).main_status == "paused"
    assert project_projection(activation_state="inactive", runs=[]).main_status == "inactive"


def test_queue_positions_are_stable_fifo_without_clock_reads():
    runs = [
        {"run_id": "b", "status": "queued", "queued_at": "2026-08-19T00:00:00Z"},
        {"run_id": "a", "status": "queued", "queued_at": "2026-08-19T00:00:00Z"},
        {"run_id": "x", "status": "processing", "queued_at": "2026-08-18T00:00:00Z"},
    ]
    assert queue_positions(runs) == {"a": 1, "b": 2}
