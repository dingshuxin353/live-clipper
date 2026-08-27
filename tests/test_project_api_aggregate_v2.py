from __future__ import annotations

from test_project_result_api_v2 import result_api_fixture

from live_clipper.project_api import ProjectAPI


def test_project_and_studio_aggregates_use_new_results_not_awaiting_review(tmp_path):
    repository, result_api, project, _run, _path, _media = result_api_fixture(tmp_path)
    result_api.repository.close()
    api = ProjectAPI(tmp_path / "service", result_api.settings)
    try:
        status, project_response = api.handle("GET", f"/api/projects/{project.project_id}")
        assert status == 200
        assert project_response["project"]["workload"]["new_results"] == 1
        assert "awaiting_review" not in project_response["project"]["workload"]
        status, studio = api.handle("GET", "/api/studio")
        assert status == 200 and studio["unseen_result_count"] == 1
        assert studio["recent_results"][0]["result_type"] == "clips_ready"
    finally:
        api.close()
