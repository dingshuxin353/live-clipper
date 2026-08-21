from __future__ import annotations

from pathlib import Path

from live_clipper.prompt_loader import export_prompts, load_prompt

ROOT = Path(__file__).resolve().parents[1]


def test_load_prompt_reads_packaged_prompt_when_cwd_is_not_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    prompt = load_prompt("cheap_scan_window.md", "cheap scan prompt")

    assert "Cheap Model: Scan Window" in prompt


def test_load_prompt_prefers_user_prompt_directory(tmp_path):
    prompt_dir = tmp_path / "prompts.local"
    prompt_dir.mkdir()
    (prompt_dir / "cheap_scan_window.md").write_text("custom scan prompt", encoding="utf-8")

    prompt = load_prompt("cheap_scan_window.md", "cheap scan prompt", prompt_dir=prompt_dir)

    assert prompt == "custom scan prompt"


def test_export_prompts_writes_editable_prompt_files(tmp_path):
    output_dir = tmp_path / "prompts.local"

    exported = export_prompts(output_dir)

    assert output_dir / "cheap_scan_window.md" in exported
    assert (output_dir / "cheap_scan_window.md").exists()
    assert "Cheap Model: Scan Window" in (output_dir / "cheap_scan_window.md").read_text(encoding="utf-8")


def test_packaged_prompts_match_root_prompt_sources():
    for root_path in sorted((ROOT / "prompts").glob("*.md")):
        name = root_path.name
        root_prompt = (ROOT / "prompts" / name).read_text(encoding="utf-8").rstrip() + "\n"
        packaged_prompt = (
            (ROOT / "src" / "live_clipper" / "prompts" / name)
            .read_text(encoding="utf-8")
            .rstrip()
            + "\n"
        )
        assert packaged_prompt == root_prompt


def test_correct_transcript_prompt_requires_object_not_array():
    prompt = (ROOT / "src" / "live_clipper" / "prompts" / "cheap_correct_transcript.md").read_text(encoding="utf-8")

    assert "Return exactly one JSON object" in prompt
    assert "Do not return a top-level JSON array" in prompt
    assert '"sentences"' in prompt
    assert '"corrections"' in prompt
