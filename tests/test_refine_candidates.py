from __future__ import annotations

from live_clipper.models import ClipCandidate, CorrectedTranscript, TranscriptSentence
from live_clipper.refine_candidates import refine_candidates_file
from live_clipper.utils import read_json, write_json


class FakeRefineClient:
    def __init__(self):
        self.payloads = []

    def complete_json(self, system_prompt, user_payload, max_tokens=2048):
        self.payloads.append(user_payload)
        candidate_id = user_payload["candidate"]["id"]
        if candidate_id == "clip-drop":
            return {
                "candidate_id": candidate_id,
                "keep": False,
                "refined_score": 2,
                "commercial_fit": 1,
                "hook_strength": 2,
                "standalone_value": 2,
                "clarity": 3,
                "suggested_title": "弱片段",
                "selection_reason": "太弱",
                "weaknesses": ["上下文依赖"],
                "recommended_adjustments": [],
            }
        return {
            "candidate_id": candidate_id,
            "keep": True,
            "refined_score": 8.8,
            "commercial_fit": 9,
            "hook_strength": 8,
            "standalone_value": 9,
            "clarity": 8,
            "suggested_title": "AI 工作流真实案例",
            "selection_reason": "展示 Agnes 适合解决真实工作问题",
            "weaknesses": [],
            "recommended_adjustments": ["开头保留问题句"],
        }


def _candidate(clip_id: str, start: float, score: float) -> dict:
    return ClipCandidate(
        id=clip_id,
        start=start,
        end=start + 20,
        score=score,
        clip_type="workflow",
        hook="原始标题",
        core_value="真实工作流",
        reason="原始理由",
    ).model_dump()


def test_refine_candidates_file_keeps_refined_items_and_drops_weak_candidates(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "refined_candidates.json"
    write_json(candidates_path, [
        _candidate("clip-keep", 10, 9),
        _candidate("clip-drop", 40, 8),
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[
        TranscriptSentence(start=9, end=12, text="我们用 AI 做一个真实工具"),
        TranscriptSentence(start=15, end=20, text="这能解决工作流问题"),
        TranscriptSentence(start=41, end=44, text="闲聊"),
    ]).model_dump())
    client = FakeRefineClient()

    refined = refine_candidates_file(candidates_path, transcript_path, output_path, client, top_n=2)

    assert len(refined) == 1
    assert refined[0]["id"] == "clip-keep"
    assert refined[0]["score"] == 8.8
    assert refined[0]["hook"] == "AI 工作流真实案例"
    assert refined[0]["agnes_refinement"]["commercial_fit"] == 9.0
    assert read_json(output_path)[0]["agnes_refinement"]["selection_reason"] == "展示 Agnes 适合解决真实工作问题"
    assert client.payloads[0]["business_goal"].startswith("Promote Agnes")


def test_refine_candidates_file_limits_to_top_n(tmp_path):
    candidates_path = tmp_path / "merged_candidates.json"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "refined_candidates.json"
    write_json(candidates_path, [
        _candidate("clip-low", 10, 1),
        _candidate("clip-high", 40, 9),
    ])
    write_json(transcript_path, CorrectedTranscript(sentences=[
        TranscriptSentence(start=40, end=45, text="高分内容"),
    ]).model_dump())
    client = FakeRefineClient()

    refine_candidates_file(candidates_path, transcript_path, output_path, client, top_n=1)

    assert [payload["candidate"]["id"] for payload in client.payloads] == ["clip-high"]
    assert read_json(output_path)[0]["id"] == "clip-high"
