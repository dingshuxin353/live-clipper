from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from live_clipper.scan_windows import scan_windows_file
from live_clipper.utils import read_json, write_json


class FakeClient:
    def __init__(self):
        self.payloads = []

    def complete_json(self, system_prompt, user_payload, max_tokens=2048):
        self.payloads.append(user_payload)
        return {
            "window_id": user_payload["id"],
            "candidates": [
                {
                    "start": 10.0,
                    "end": 50.0,
                    "score": 8.5,
                    "clip_type": "insight",
                    "hook": "一个强开头",
                    "core_value": "核心观点",
                    "reason": "信息完整",
                    "risk": None,
                    "suggested_context_before": 2,
                    "suggested_context_after": 3,
                }
            ],
        }


def test_scan_windows_file_calls_client_for_each_window_and_writes_candidates(tmp_path):
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])
    client = FakeClient()

    candidates = scan_windows_file(windows_path, output_path, client)

    assert candidates[0].id == "w0001-c001"
    assert candidates[0].score == 8.5
    assert client.payloads[0]["id"] == "w0001"
    assert read_json(output_path)[0]["id"] == "w0001-c001"


def test_scan_windows_file_reports_window_progress(tmp_path, capsys):
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    scan_windows_file(windows_path, output_path, FakeClient())

    output = capsys.readouterr().out
    assert "[候选扫描] 开始: 已完成 0/1 个窗口, 已加载 0 条候选" in output
    assert "[候选扫描] 1/1 w0001: 正在请求 Agnes" in output
    assert "[候选扫描] 1/1 w0001: 完成, 新增 1 条, 跳过 0 条, 当前累计 1 条" in output
    assert f"[候选扫描] 全部完成: 共 1 条候选 -> {output_path}" in output


def test_scan_windows_file_checkpoints_completed_windows_before_failure(tmp_path):
    class FailingSecondWindowClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            if user_payload["id"] == "w0002":
                raise RuntimeError("api interrupted")
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "start": 10.0,
                        "end": 50.0,
                        "score": 8.5,
                        "clip_type": "insight",
                        "hook": "一个强开头",
                        "core_value": "核心观点",
                        "reason": "信息完整",
                    }
                ],
            }

    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 120.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容 1", "speaker": None}],
        },
        {
            "id": "w0002",
            "start": 120.0,
            "end": 240.0,
            "sentences": [{"start": 130.0, "end": 160.0, "text": "内容 2", "speaker": None}],
        },
    ])

    with pytest.raises(RuntimeError, match="api interrupted"):
        scan_windows_file(windows_path, output_path, FailingSecondWindowClient())

    checkpoint = read_json(tmp_path / "cheap_candidates.partial.json")
    assert checkpoint["processed_window_ids"] == ["w0001"]
    assert checkpoint["candidates"][0]["id"] == "w0001-c001"
    assert not output_path.exists()


def test_scan_windows_file_resume_skips_checkpointed_windows(tmp_path):
    class ResumeClient:
        def __init__(self):
            self.window_ids = []

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.window_ids.append(user_payload["id"])
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "start": 130.0,
                        "end": 160.0,
                        "score": 8.0,
                        "clip_type": "insight",
                        "hook": "第二段",
                        "core_value": "继续扫描",
                        "reason": "信息完整",
                    }
                ],
            }

    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    checkpoint_path = tmp_path / "cheap_candidates.partial.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 120.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容 1", "speaker": None}],
        },
        {
            "id": "w0002",
            "start": 120.0,
            "end": 240.0,
            "sentences": [{"start": 130.0, "end": 160.0, "text": "内容 2", "speaker": None}],
        },
    ])
    write_json(checkpoint_path, {
        "processed_window_ids": ["w0001"],
        "candidates": [
            {
                "id": "w0001-c001",
                "start": 10.0,
                "end": 50.0,
                "score": 8.5,
                "clip_type": "insight",
                "hook": "第一段",
                "core_value": "已完成",
                "reason": "信息完整",
            }
        ],
    })
    client = ResumeClient()

    candidates = scan_windows_file(windows_path, output_path, client, resume=True)

    assert client.window_ids == ["w0002"]
    assert [candidate.id for candidate in candidates] == ["w0001-c001", "w0002-c001"]
    assert [item["id"] for item in read_json(output_path)] == ["w0001-c001", "w0002-c001"]
    assert not checkpoint_path.exists()


def test_scan_windows_file_skips_invalid_model_candidate(tmp_path, monkeypatch):
    class InvalidClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "start": 10.0,
                        "end": 9.0,
                        "score": 99,
                        "clip_type": "insight",
                        "hook": "bad",
                        "core_value": "bad",
                        "reason": "bad",
                    }
                ],
            }

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, InvalidClient())

    assert candidates == []
    assert read_json(output_path) == []

    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    log = read_json(logs[0])
    assert log["window_id"] == "w0001"
    assert log["model_response"]["candidates"][0]["score"] == 99


def test_scan_windows_file_defaults_non_numeric_suggested_context_fields(tmp_path):
    class NonNumericContextClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "start": 10.0,
                        "end": 50.0,
                        "score": 8.5,
                        "clip_type": "insight",
                        "hook": "一个强开头",
                        "core_value": "核心观点",
                        "reason": "信息完整",
                        "suggested_context_before": "None needed, starts cold.",
                        "suggested_context_after": "Cut to them asking for help or trying to fix it.",
                    }
                ],
            }

    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, NonNumericContextClient())

    assert candidates[0].suggested_context_before == 0.0
    assert candidates[0].suggested_context_after == 0.0
    assert read_json(output_path)[0]["suggested_context_before"] == 0.0


def test_scan_windows_file_skips_response_missing_candidates(tmp_path, monkeypatch):
    class MissingCandidatesClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {"window_id": user_payload["id"]}

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, MissingCandidatesClient())

    assert candidates == []
    assert read_json(output_path) == []

    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    assert read_json(logs[0])["model_response"] == {"window_id": "w0001"}


def test_scan_windows_file_skips_non_object_window_response(tmp_path, monkeypatch):
    class NonObjectWindowClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return ["bad response"]

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, NonObjectWindowClient())

    assert candidates == []
    assert read_json(output_path) == []
    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    assert read_json(logs[0])["model_response"] == ["bad response"]


def test_scan_windows_file_skips_mismatched_window_id(tmp_path, monkeypatch):
    class MismatchedWindowClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {"window_id": "other", "candidates": []}

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, MismatchedWindowClient())

    assert candidates == []
    assert read_json(output_path) == []

    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    assert read_json(logs[0])["model_response"]["window_id"] == "other"


def test_scan_windows_file_skips_non_object_candidate(tmp_path, monkeypatch):
    class NonObjectCandidateClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    "not an object",
                    {
                        "start": 10.0,
                        "end": 50.0,
                        "score": 8.0,
                        "clip_type": "insight",
                        "hook": "好候选",
                        "core_value": "核心观点",
                        "reason": "信息完整",
                    },
                ],
            }

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, NonObjectCandidateClient())

    assert [candidate.id for candidate in candidates] == ["w0001-c002"]
    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1


def test_scan_windows_file_skips_candidate_outside_window(tmp_path, monkeypatch):
    class OutsideWindowClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "start": 250.0,
                        "end": 300.0,
                        "score": 8.0,
                        "clip_type": "insight",
                        "hook": "窗口外",
                        "core_value": "不应进入后续流程",
                        "reason": "bad",
                    }
                ],
            }

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, OutsideWindowClient())

    assert candidates == []
    assert read_json(output_path) == []

    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    log = read_json(logs[0])
    assert log["window_id"] == "w0001"
    assert log["candidate_index"] == 1
    assert log["model_response"]["candidates"][0]["start"] == 250.0


def test_scan_windows_file_skips_duplicate_candidate_ids(tmp_path, monkeypatch):
    class DuplicateCandidateIdClient:
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "window_id": user_payload["id"],
                "candidates": [
                    {
                        "id": "dup-clip",
                        "start": 10.0,
                        "end": 20.0,
                        "score": 8.0,
                        "clip_type": "insight",
                        "hook": "重复 1",
                        "core_value": "不应进入后续流程",
                        "reason": "bad",
                    },
                    {
                        "id": "dup-clip",
                        "start": 30.0,
                        "end": 40.0,
                        "score": 8.0,
                        "clip_type": "insight",
                        "hook": "重复 2",
                        "core_value": "不应进入后续流程",
                        "reason": "bad",
                    },
                ],
            }

    monkeypatch.chdir(tmp_path)
    windows_path = tmp_path / "windows.json"
    output_path = tmp_path / "cheap_candidates.json"
    write_json(windows_path, [
        {
            "id": "w0001",
            "start": 0.0,
            "end": 240.0,
            "sentences": [{"start": 10.0, "end": 50.0, "text": "内容", "speaker": None}],
        }
    ])

    candidates = scan_windows_file(windows_path, output_path, DuplicateCandidateIdClient())

    assert [candidate.id for candidate in candidates] == ["dup-clip"]
    assert [item["id"] for item in read_json(output_path)] == ["dup-clip"]

    logs = list(Path("work/logs").glob("scan_windows_validation_failure_*.json"))
    assert len(logs) == 1
    log = read_json(logs[0])
    assert log["candidate_id"] == "dup-clip"
