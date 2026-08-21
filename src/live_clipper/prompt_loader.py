from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

from .utils import read_required_text

PROMPT_FILES = [
    "cheap_correct_transcript.md",
    "cheap_refine_candidate.md",
    "cheap_reverse_review.md",
    "cheap_scan_window.md",
    "codex_select_clips.md",
]


def _packaged_prompt_path(file_name: str):
    return resources.files("live_clipper.prompts").joinpath(file_name)


def load_prompt(file_name: str, description: str, *, prompt_dir: Path | None = None) -> str:
    if prompt_dir is not None:
        user_prompt = prompt_dir / file_name
        if user_prompt.exists():
            return user_prompt.read_text(encoding="utf-8")

    packaged = resources.files("live_clipper.prompts").joinpath(file_name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")

    repo_prompt = Path(__file__).resolve().parents[2] / "prompts" / file_name
    return read_required_text(repo_prompt, description)


def export_prompts(output_dir: Path, *, overwrite: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for file_name in PROMPT_FILES:
        destination = output_dir / file_name
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        packaged = _packaged_prompt_path(file_name)
        if packaged.is_file():
            destination.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            repo_prompt = Path(__file__).resolve().parents[2] / "prompts" / file_name
            shutil.copyfile(repo_prompt, destination)
        exported.append(destination)
    return exported
