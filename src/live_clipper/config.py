from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    cheap_model_api_base: str | None = None
    cheap_model_api_key: str | None = None
    cheap_model_name: str | None = None
    asr_api_base: str | None = None
    asr_api_key: str | None = None
    asr_model: str | None = None


def load_settings() -> Settings:
    return Settings(
        cheap_model_api_base=os.getenv("CHEAP_MODEL_API_BASE"),
        cheap_model_api_key=os.getenv("CHEAP_MODEL_API_KEY"),
        cheap_model_name=os.getenv("CHEAP_MODEL_NAME"),
        asr_api_base=os.getenv("ASR_API_BASE"),
        asr_api_key=os.getenv("ASR_API_KEY"),
        asr_model=os.getenv("ASR_MODEL"),
    )

