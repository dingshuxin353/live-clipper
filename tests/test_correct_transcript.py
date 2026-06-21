from __future__ import annotations

from pathlib import Path

import pytest

from live_clipper.correct_transcript import correct_transcript_file
from live_clipper.utils import read_json, write_json


class FakeClient:
    def __init__(self):
        self.calls = []

    def complete_json(self, system_prompt, user_payload, max_tokens=2048):
        self.calls.append((system_prompt, user_payload, max_tokens))
        return {
            "sentences": [
                {"start": 0.0, "end": 2.0, "text": "我们用 ffmpeg 渲染", "speaker": None},
            ],
            "corrections": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "original_text": "我们用 ThemPad 渲染",
                    "corrected_text": "我们用 ffmpeg 渲染",
                    "reason": "glossary match",
                    "confidence": 0.95,
                }
            ],
        }


def test_correct_transcript_file_preserves_timestamps_and_uses_glossary(tmp_path):
    raw_path = tmp_path / "transcript_raw.json"
    glossary_path = tmp_path / "common_terms.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "我们用 ThemPad 渲染"},
        ]
    })
    write_json(glossary_path, [
        {
            "canonical": "ffmpeg",
            "common_mistakes": ["ThemPad"],
            "notes": "video rendering tool",
        }
    ])
    client = FakeClient()

    corrected = correct_transcript_file(raw_path, glossary_path, output_path, client)

    assert corrected.sentences[0].start == 0.0
    assert corrected.sentences[0].end == 2.0
    assert corrected.sentences[0].text == "我们用 ffmpeg 渲染"
    assert read_json(output_path)["corrections"][0]["corrected_text"] == "我们用 ffmpeg 渲染"
    assert client.calls[0][1]["glossary"][0]["canonical"] == "ffmpeg"


def test_correct_transcript_file_allows_missing_glossary(tmp_path):
    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "原文"},
        ]
    })
    client = FakeClient()

    correct_transcript_file(raw_path, tmp_path / "missing.json", output_path, client)

    assert client.calls[0][1]["glossary"] == []


def test_correct_transcript_file_keeps_original_sentence_timestamps(tmp_path):
    class TimestampChangingClient(FakeClient):
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "sentences": [
                    {"start": 99.0, "end": 100.0, "text": "改正文本", "speaker": "changed"},
                ],
                "corrections": [],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 2.0, "end": 4.0, "text": "原文"},
        ]
    })

    corrected = correct_transcript_file(
        raw_path,
        tmp_path / "missing.json",
        output_path,
        TimestampChangingClient(),
    )

    assert corrected.sentences[0].start == 2.0
    assert corrected.sentences[0].end == 4.0
    assert corrected.sentences[0].speaker is None
    assert corrected.sentences[0].text == "改正文本"


def test_correct_transcript_file_accepts_bare_sentence_list_response(tmp_path):
    class BareListClient(FakeClient):
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return [
                {
                    "start": 99.0,
                    "end": 100.0,
                    "text": f"{item['text']} corrected",
                    "speaker": "changed",
                }
                for item in user_payload["sentences"]
            ]

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "第一句"},
            {"start": 1.0, "end": 2.0, "text": "第二句"},
        ]
    })

    corrected = correct_transcript_file(
        raw_path,
        tmp_path / "missing.json",
        output_path,
        BareListClient(),
    )

    assert [sentence.text for sentence in corrected.sentences] == [
        "第一句 corrected",
        "第二句 corrected",
    ]
    assert [(sentence.start, sentence.end, sentence.speaker) for sentence in corrected.sentences] == [
        (0.0, 1.0, None),
        (1.0, 2.0, None),
    ]
    assert corrected.corrections == []


def test_correct_transcript_file_batches_long_transcripts_and_merges_results(tmp_path):
    class BatchClient:
        def __init__(self):
            self.calls = []

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.calls.append((system_prompt, user_payload, max_tokens))
            return {
                "sentences": [
                    {
                        "start": 99.0,
                        "end": 100.0,
                        "text": f"{item['text']} corrected",
                        "speaker": "changed",
                    }
                    for item in user_payload["sentences"]
                ],
                "corrections": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "original_text": item["text"],
                        "corrected_text": f"{item['text']} corrected",
                        "reason": "batch",
                        "confidence": 0.9,
                    }
                    for item in user_payload["sentences"]
                ],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "第一句"},
            {"start": 1.0, "end": 2.0, "text": "第二句"},
            {"start": 2.0, "end": 3.0, "text": "第三句"},
        ]
    })
    client = BatchClient()

    corrected = correct_transcript_file(
        raw_path,
        tmp_path / "missing.json",
        output_path,
        client,
        batch_size=2,
    )

    assert [len(call[1]["sentences"]) for call in client.calls] == [2, 1]
    assert [sentence.text for sentence in corrected.sentences] == [
        "第一句 corrected",
        "第二句 corrected",
        "第三句 corrected",
    ]
    assert [(sentence.start, sentence.end, sentence.speaker) for sentence in corrected.sentences] == [
        (0.0, 1.0, None),
        (1.0, 2.0, None),
        (2.0, 3.0, None),
    ]
    assert len(corrected.corrections) == 3
    assert len(read_json(output_path)["sentences"]) == 3


def test_correct_transcript_file_default_batch_size_stays_conservative_for_model_reliability(tmp_path):
    class BatchSizeClient:
        def __init__(self):
            self.batch_lengths = []

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.batch_lengths.append(len(user_payload["sentences"]))
            return {
                "sentences": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "text": item["text"],
                        "speaker": item["speaker"],
                    }
                    for item in user_payload["sentences"]
                ],
                "corrections": [],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": float(index), "end": float(index + 1), "text": f"第{index}句"}
            for index in range(201)
        ]
    })
    client = BatchSizeClient()

    correct_transcript_file(raw_path, tmp_path / "missing.json", output_path, client)

    assert client.batch_lengths == [80, 80, 41]


def test_correct_transcript_file_checkpoints_completed_batches_before_failure(tmp_path):
    class FailingSecondBatchClient:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("api interrupted")
            return {
                "sentences": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "text": f"{item['text']} corrected",
                        "speaker": item["speaker"],
                    }
                    for item in user_payload["sentences"]
                ],
                "corrections": [],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "第一句"},
            {"start": 1.0, "end": 2.0, "text": "第二句"},
            {"start": 2.0, "end": 3.0, "text": "第三句"},
        ]
    })

    with pytest.raises(RuntimeError, match="api interrupted"):
        correct_transcript_file(
            raw_path,
            tmp_path / "missing.json",
            output_path,
            FailingSecondBatchClient(),
            batch_size=2,
        )

    checkpoint = read_json(tmp_path / "transcript.partial.json")
    assert checkpoint["processed_sentence_count"] == 2
    assert [item["text"] for item in checkpoint["sentences"]] == [
        "第一句 corrected",
        "第二句 corrected",
    ]
    assert not output_path.exists()


def test_correct_transcript_file_resume_skips_checkpointed_sentences(tmp_path):
    class ResumeClient:
        def __init__(self):
            self.payloads = []

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.payloads.append(user_payload)
            return {
                "sentences": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "text": f"{item['text']} corrected",
                        "speaker": item["speaker"],
                    }
                    for item in user_payload["sentences"]
                ],
                "corrections": [],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    checkpoint_path = tmp_path / "transcript.partial.json"
    write_json(raw_path, {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "第一句"},
            {"start": 1.0, "end": 2.0, "text": "第二句"},
            {"start": 2.0, "end": 3.0, "text": "第三句"},
        ]
    })
    write_json(checkpoint_path, {
        "processed_sentence_count": 2,
        "sentences": [
            {"start": 0.0, "end": 1.0, "text": "第一句 corrected", "speaker": None},
            {"start": 1.0, "end": 2.0, "text": "第二句 corrected", "speaker": None},
        ],
        "corrections": [],
    })
    client = ResumeClient()

    corrected = correct_transcript_file(
        raw_path,
        tmp_path / "missing.json",
        output_path,
        client,
        batch_size=2,
        resume=True,
    )

    assert [[item["text"] for item in payload["sentences"]] for payload in client.payloads] == [["第三句"]]
    assert [sentence.text for sentence in corrected.sentences] == [
        "第一句 corrected",
        "第二句 corrected",
        "第三句 corrected",
    ]
    assert len(read_json(output_path)["sentences"]) == 3
    assert not checkpoint_path.exists()


def test_correct_transcript_file_resume_checkpoint_counts_from_original_start(tmp_path):
    class FailingThirdResumeBatchClient:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("api interrupted")
            return {
                "sentences": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "text": f"{item['text']} corrected",
                        "speaker": item["speaker"],
                    }
                    for item in user_payload["sentences"]
                ],
                "corrections": [],
            }

    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    checkpoint_path = tmp_path / "transcript.partial.json"
    write_json(raw_path, {
        "segments": [
            {"start": float(index), "end": float(index + 1), "text": f"第{index}句"}
            for index in range(8)
        ]
    })
    write_json(checkpoint_path, {
        "processed_sentence_count": 2,
        "sentences": [
            {"start": 0.0, "end": 1.0, "text": "第0句 corrected", "speaker": None},
            {"start": 1.0, "end": 2.0, "text": "第1句 corrected", "speaker": None},
        ],
        "corrections": [],
    })

    with pytest.raises(RuntimeError, match="api interrupted"):
        correct_transcript_file(
            raw_path,
            tmp_path / "missing.json",
            output_path,
            FailingThirdResumeBatchClient(),
            batch_size=2,
            resume=True,
        )

    checkpoint = read_json(checkpoint_path)
    assert checkpoint["processed_sentence_count"] == 6
    assert [item["text"] for item in checkpoint["sentences"]] == [
        "第0句 corrected",
        "第1句 corrected",
        "第2句 corrected",
        "第3句 corrected",
        "第4句 corrected",
        "第5句 corrected",
    ]


def test_correct_transcript_file_falls_back_to_original_batch_on_sentence_count_mismatch(tmp_path, monkeypatch):
    class MismatchClient(FakeClient):
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "sentences": [],
                "corrections": [],
            }

    monkeypatch.chdir(tmp_path)
    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 2.0, "end": 4.0, "text": "原文"},
        ]
    })

    corrected = correct_transcript_file(
        raw_path,
        tmp_path / "missing.json",
        output_path,
        MismatchClient(),
    )

    assert corrected.sentences[0].text == "原文"
    assert read_json(output_path)["sentences"][0]["text"] == "原文"

    logs = list(Path("work/logs").glob("correct_transcript_validation_failure_*.json"))
    assert len(logs) == 1
    log = read_json(logs[0])
    assert log["raw_sentence_count"] == 1
    assert log["model_sentence_count"] == 0
    assert log["model_response"]["sentences"] == []


def test_correct_transcript_file_rejects_non_list_sentences(tmp_path, monkeypatch):
    class BadShapeClient(FakeClient):
        def complete_json(self, system_prompt, user_payload, max_tokens=2048):
            return {
                "sentences": {"text": "bad"},
                "corrections": [],
            }

    monkeypatch.chdir(tmp_path)
    raw_path = tmp_path / "transcript_raw.json"
    output_path = tmp_path / "transcript.json"
    write_json(raw_path, {
        "segments": [
            {"start": 2.0, "end": 4.0, "text": "原文"},
        ]
    })

    with pytest.raises(ValueError, match="sentences must be a list"):
        correct_transcript_file(
            raw_path,
            tmp_path / "missing.json",
            output_path,
            BadShapeClient(),
        )

    logs = list(Path("work/logs").glob("correct_transcript_validation_failure_*.json"))
    assert len(logs) == 1
    assert read_json(logs[0])["model_response"]["sentences"] == {"text": "bad"}
