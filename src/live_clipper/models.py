from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator


SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_safe_id(value: str, field_name: str) -> str:
    if not SAFE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must contain only letters, numbers, dots, underscores, or hyphens")
    return value


class TranscriptSentence(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptSentence":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class TranscriptWindow(BaseModel):
    id: str
    start: float
    end: float
    sentences: list[TranscriptSentence]

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptWindow":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


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

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCorrection":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


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
    suggested_context_before: float = Field(default=0, ge=0)
    suggested_context_after: float = Field(default=0, ge=0)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_safe_id(value, "id")

    @model_validator(mode="after")
    def validate_time_range(self) -> "ClipCandidate":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class SelectedClip(BaseModel):
    clip_id: str
    source_start: float
    source_end: float
    title: str
    remove_ranges: list[tuple[float, float]] = Field(default_factory=list)
    subtitle_highlights: list[str] = Field(default_factory=list)
    format: str = "horizontal_highlight"
    priority: int = 1

    @field_validator("clip_id")
    @classmethod
    def validate_clip_id(cls, value: str) -> str:
        return validate_safe_id(value, "clip_id")

    @model_validator(mode="after")
    def validate_time_range(self) -> "SelectedClip":
        if self.source_start >= self.source_end:
            raise ValueError("source_start must be before source_end")
        return self
