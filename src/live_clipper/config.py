from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_CHEAP_MODEL_API_BASE = "https://apihub.agnes-ai.com/v1"
DEFAULT_CHEAP_MODEL_NAME = "agnes-2.0-flash"
DEFAULT_ASR_BACKEND = "mlx_whisper"
DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_OPENAI_ASR_API_BASE = "https://api.openai.com/v1"
DEFAULT_OPENAI_ASR_MODEL = "whisper-1"


@dataclass(frozen=True)
class Settings:
    cheap_model_api_base: str | None = None
    cheap_model_api_key: str | None = None
    cheap_model_name: str | None = None
    asr_backend: str | None = None
    asr_api_base: str | None = None
    asr_api_key: str | None = None
    asr_model: str | None = None
    hf_token: str | None = None


def load_settings() -> Settings:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=True)
    asr_backend = os.getenv("ASR_BACKEND", DEFAULT_ASR_BACKEND)
    asr_model = os.getenv("ASR_MODEL")
    if asr_model is None:
        asr_model = DEFAULT_OPENAI_ASR_MODEL if asr_backend == "openai" else DEFAULT_ASR_MODEL
    return Settings(
        cheap_model_api_base=os.getenv("CHEAP_MODEL_API_BASE", DEFAULT_CHEAP_MODEL_API_BASE),
        cheap_model_api_key=os.getenv("CHEAP_MODEL_API_KEY"),
        cheap_model_name=os.getenv("CHEAP_MODEL_NAME", DEFAULT_CHEAP_MODEL_NAME),
        asr_backend=asr_backend,
        asr_api_base=os.getenv("ASR_API_BASE", DEFAULT_OPENAI_ASR_API_BASE if asr_backend == "openai" else None),
        asr_api_key=os.getenv("ASR_API_KEY"),
        asr_model=asr_model,
        hf_token=os.getenv("HF_TOKEN"),
    )
