from __future__ import annotations

import re
from typing import Literal

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
    def validate_time_range(self) -> TranscriptSentence:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class TranscriptWindow(BaseModel):
    id: str
    start: float
    end: float
    sentences: list[TranscriptSentence]

    @model_validator(mode="after")
    def validate_time_range(self) -> TranscriptWindow:
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
    def validate_time_range(self) -> TranscriptCorrection:
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
    def validate_time_range(self) -> ClipCandidate:
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

    @field_validator("remove_ranges", mode="before")
    @classmethod
    def normalize_remove_ranges(cls, value: object) -> object:
        """容忍 LLM 常见的对象写法 {"start": s, "end": e}，归一化为 [s, e]。

        同时兼容已有的 [s, e] / (s, e) 写法。无法识别的元素原样返回，交由
        pydantic 抛出清晰的类型错误。
        """
        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        for item in value:
            if isinstance(item, dict):
                start = item.get("start", item.get("from"))
                end = item.get("end", item.get("to"))
                if start is not None and end is not None:
                    normalized.append([start, end])
                else:
                    normalized.append(item)
            else:
                normalized.append(item)
        return normalized

    @field_validator("clip_id")
    @classmethod
    def validate_clip_id(cls, value: str) -> str:
        return validate_safe_id(value, "clip_id")

    @model_validator(mode="after")
    def validate_time_range(self) -> SelectedClip:
        if self.source_start >= self.source_end:
            raise ValueError("source_start must be before source_end")
        return self


class ReviewMaterial(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=3)
    description: str = Field(max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("titles")
    @classmethod
    def validate_titles(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("review titles must contain 1 to 100 characters")
        return [value.strip() for value in values]

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 30 for value in values):
            raise ValueError("review tags must contain 1 to 30 characters")
        return [value.strip() for value in values]


class ReviewDecision(BaseModel):
    candidate_id: str
    decision: Literal["selected", "rejected"]
    rank: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    rejection_reason_code: str | None = None
    selected_clip: SelectedClip | None = None
    material: ReviewMaterial | None = None

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        return validate_safe_id(value, "candidate_id")

    @model_validator(mode="after")
    def validate_decision_shape(self) -> ReviewDecision:
        if self.decision == "selected":
            if self.selected_clip is None or self.material is None:
                raise ValueError("selected decisions require selected_clip and material")
            if self.selected_clip.clip_id != self.candidate_id:
                raise ValueError("selected clip must reference the candidate_id")
            if self.rejection_reason_code is not None:
                raise ValueError("selected decisions cannot contain rejection_reason_code")
        else:
            if self.selected_clip is not None or self.material is not None:
                raise ValueError("rejected decisions cannot contain selected_clip or material")
            if not self.rejection_reason_code:
                raise ValueError("rejected decisions require rejection_reason_code")
        return self


class ProjectReviewResult(BaseModel):
    format_version: Literal[1]
    overall_summary: str = Field(max_length=2000)
    warnings: list[dict[str, object]] = Field(default_factory=list, max_length=50)
    decisions: list[ReviewDecision]

    @model_validator(mode="after")
    def validate_unique_decisions(self) -> ProjectReviewResult:
        candidate_ids = [item.candidate_id for item in self.decisions]
        ranks = [item.rank for item in self.decisions]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("review decisions contain duplicate candidate_id")
        if len(set(ranks)) != len(ranks):
            raise ValueError("review decisions contain duplicate rank")
        return self
