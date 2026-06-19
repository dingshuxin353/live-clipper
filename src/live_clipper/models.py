from __future__ import annotations

from pydantic import BaseModel, Field


class TranscriptSentence(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class TranscriptWindow(BaseModel):
    id: str
    start: float
    end: float
    sentences: list[TranscriptSentence]


class GlossaryTerm(BaseModel):
    canonical: str
    common_mistakes: list[str] = Field(default_factory=list)
    notes: str | None = None


class TranscriptCorrection(BaseModel):
    start: float
    end: float
    original_text: str
    corrected_text: str
    reason: str
    confidence: float = Field(ge=0, le=1)


class CorrectedTranscript(BaseModel):
    sentences: list[TranscriptSentence]
    corrections: list[TranscriptCorrection] = Field(default_factory=list)


class ClipCandidate(BaseModel):
    id: str
    start: float
    end: float
    score: float = Field(ge=0, le=10)
    clip_type: str
    hook: str
    core_value: str
    reason: str
    risk: str | None = None
    suggested_context_before: float = 0
    suggested_context_after: float = 0


class SelectedClip(BaseModel):
    clip_id: str
    source_start: float
    source_end: float
    title: str
    remove_ranges: list[tuple[float, float]] = Field(default_factory=list)
    subtitle_highlights: list[str] = Field(default_factory=list)
    format: str = "horizontal_highlight"
    priority: int = 1
