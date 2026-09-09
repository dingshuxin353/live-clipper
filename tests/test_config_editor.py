import tomllib
from pathlib import Path

from live_clipper.config_editor import ensure_workspace_root


def _write_config(path: Path, source_dir: Path) -> None:
    path.write_text(
        "\n".join([
            "[paths]",
            "input_dir = 'input'",
            "output_root = 'output'",
            "work_dir = 'work'",
            "glossary_path = 'glossary/common_terms.json'",
            "",
            "[recording_source.default]",
            f"source_dir = '{source_dir}'",
            "input_dir = 'input'",
            "output_root = 'output'",
            "since_hours = 168",
            "min_age_minutes = 10",
            "stable_check_seconds = 60",
            "",
            "[llm]",
            "api_base = 'https://example.test/v1'",
            "api_key_env = 'SECRET_LLM_KEY'",
            "model = 'agnes-test'",
            "timeout_seconds = 300",
            "request_attempts = 5",
            "retry_delay_seconds = 3.0",
            "",
            "[asr]",
            "backend = 'mlx_whisper'",
            "model = 'mlx-community/whisper-large-v3-turbo'",
            "language = 'zh'",
            "api_key_env = 'ASR_API_KEY'",
            "hf_token_env = 'HF_TOKEN'",
            "",
            "[service]",
            "enabled = true",
            "scan_interval_minutes = 30",
            "auto_render_after_selection = true",
            "cleanup_mode = 'preview_only'",
            "",
            "[web]",
            "host = '127.0.0.1'",
            "port = 8765",
            "access_token = 'secret-token'",
        ]),
        encoding="utf-8",
    )


def test_ensure_workspace_root_migrates_one_field_without_other_file_changes(tmp_path):
    missing_nas = tmp_path / "offline-nas"
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, missing_nas)
    env_path = tmp_path / ".env"
    marker_path = tmp_path / "work" / "service" / "onboarding.json"
    env_path.write_bytes(b"ASR_API_KEY=keep\\n")
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(b"{}\\n")
    original = tomllib.loads(config_path.read_text(encoding="utf-8"))

    result = ensure_workspace_root(
        config_path=config_path,
        workspace_root=tmp_path / "workspace",
        backup_root=tmp_path / "backups",
    )

    assert result["ok"] is True
    assert result["migrated"] is True
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert raw["paths"]["workspace_root"] == str(tmp_path / "workspace")
    raw["paths"].pop("workspace_root")
    assert raw == original
    assert env_path.read_bytes() == b"ASR_API_KEY=keep\\n"
    assert marker_path.read_bytes() == b"{}\\n"


def test_ensure_workspace_root_preserves_existing_custom_value(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    original = config_path.read_text(encoding="utf-8").replace(
        "[paths]\n",
        f"[paths]\nworkspace_root = '{tmp_path / 'custom'}'\n",
    )
    config_path.write_text(original, encoding="utf-8")

    result = ensure_workspace_root(
        config_path=config_path,
        workspace_root=tmp_path / "default",
        backup_root=tmp_path / "backups",
    )

    assert result["migrated"] is False
    assert config_path.read_text(encoding="utf-8") == original
