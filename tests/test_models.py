from __future__ import annotations

import pytest
from pydantic import ValidationError

from live_clipper.models import ClipCandidate, SelectedClip, TranscriptCorrection, TranscriptSentence, TranscriptWindow


def test_transcript_sentence_rejects_non_positive_time_range():
    with pytest.raises(ValidationError):
        TranscriptSentence(start=3, end=3, text="bad")


def test_transcript_window_rejects_non_positive_time_range():
    with pytest.raises(ValidationError):
        TranscriptWindow(id="w0001", start=10, end=9, sentences=[])


def test_transcript_correction_rejects_non_positive_time_range():
    with pytest.raises(ValidationError):
        TranscriptCorrection(
            start=5,
            end=5,
            original_text="old",
            corrected_text="new",
            reason="reason",
            confidence=0.9,
        )


def test_clip_candidate_rejects_invalid_time_and_negative_context():
    with pytest.raises(ValidationError):
        ClipCandidate(
            id="clip-1",
            start=10,
            end=9,
            score=8,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
        )

    with pytest.raises(ValidationError):
        ClipCandidate(
            id="clip-1",
            start=10,
            end=20,
            score=8,
            clip_type="insight",
            hook="hook",
            core_value="value",
            reason="reason",
            suggested_context_before=-1,
        )


def test_clip_candidate_rejects_unsafe_id():
    for unsafe_id in ["../clip-1", "clips/clip-1", "   "]:
        with pytest.raises(ValidationError):
            ClipCandidate(
                id=unsafe_id,
                start=10,
                end=20,
                score=8,
                clip_type="insight",
                hook="hook",
                core_value="value",
                reason="reason",
            )


def test_selected_clip_rejects_invalid_time_range():
    with pytest.raises(ValidationError):
        SelectedClip(
            clip_id="clip-1",
            source_start=30,
            source_end=30,
            title="bad",
        )


def test_selected_clip_rejects_unsafe_clip_id():
    for unsafe_id in ["../clip-1", "clips/clip-1", "   "]:
        with pytest.raises(ValidationError):
            SelectedClip(
                clip_id=unsafe_id,
                source_start=10,
                source_end=30,
                title="bad",
            )


def test_selected_clip_accepts_object_remove_ranges():
    from live_clipper.models import SelectedClip

    clip = SelectedClip(
        clip_id="w0001-c001",
        source_start=0.0,
        source_end=30.0,
        title="t",
        remove_ranges=[{"start": 4.0, "end": 6.0}],
    )
    assert clip.remove_ranges == [(4.0, 6.0)]


def test_selected_clip_accepts_array_remove_ranges():
    from live_clipper.models import SelectedClip

    clip = SelectedClip(
        clip_id="w0001-c001",
        source_start=0.0,
        source_end=30.0,
        title="t",
        remove_ranges=[[4.0, 6.0]],
    )
    assert clip.remove_ranges == [(4.0, 6.0)]
