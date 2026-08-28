from __future__ import annotations

import pytest
from test_project_result_api_v2 import result_api_fixture

from live_clipper.project_result_api import ResultAPIError


def test_media_supports_full_open_suffix_and_head_ranges(tmp_path):
    _repository, api, _project, _run, _path, media = result_api_fixture(
        tmp_path,
        output_container="mov,mp4,m4a,3gp,3g2,mj2",
    )

    full = api.media("output-1")
    assert (full.status, full.body, full.headers["Accept-Ranges"]) == (200, media, "bytes")
    assert full.headers["Content-Type"] == "video/mp4"
    middle = api.media("output-1", "bytes=2-5")
    assert middle.status == 206 and middle.body == media[2:6]
    assert middle.headers["Content-Type"] == "video/mp4"
    assert middle.headers["Content-Range"] == f"bytes 2-5/{len(media)}"
    assert api.media("output-1", "bytes=10-").body == media[10:]
    assert api.media("output-1", "bytes=-4").body == media[-4:]
    assert api.media("output-1", "bytes=0-3", head_only=True).body == b""

    with pytest.raises(ResultAPIError, match="Range") as exc:
        api.media("output-1", "bytes=0-1,3-4")
    assert exc.value.status == 416 and exc.value.code == "range_not_satisfiable"


def test_media_keeps_unknown_registered_containers_as_binary(tmp_path):
    _repository, api, _project, _run, _path, _media = result_api_fixture(
        tmp_path,
        output_container="unknown-container",
    )

    assert api.media("output-1").headers["Content-Type"] == "application/octet-stream"


def test_tampered_ready_media_is_blocked_and_creates_issue(tmp_path):
    repository, api, _project, _run, path, media = result_api_fixture(tmp_path)
    path.write_bytes(b"x" * len(media))

    with pytest.raises(ResultAPIError) as exc:
        api.media("output-1")
    assert exc.value.code == "output_unavailable"
    assert repository.get_run_output("output-1").status == "unreadable"
    issues = repository.list_issues(active_only=True)
    assert len(issues) == 1 and issues[0].issue_code == "output_unreadable"
