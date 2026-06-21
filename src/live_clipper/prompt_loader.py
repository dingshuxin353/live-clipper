from __future__ import annotations

from importlib import resources
from pathlib import Path

from .utils import read_required_text


def load_prompt(file_name: str, description: str) -> str:
    packaged = resources.files("live_clipper.prompts").joinpath(file_name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")

    repo_prompt = Path(__file__).resolve().parents[2] / "prompts" / file_name
    return read_required_text(repo_prompt, description)
