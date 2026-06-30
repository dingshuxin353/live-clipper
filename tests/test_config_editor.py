from __future__ import annotations

from pathlib import Path

from live_clipper.config import load_settings
from live_clipper.config_editor import (
    load_editable_config,
    save_editable_config,
    validate_editable_config,
)


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


def test_load_editable_config_returns_whitelist_and_secret_status(monkeypatch, tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    monkeypatch.setenv("SECRET_LLM_KEY", "sk-secret")

    payload = load_editable_config(config_path=config_path)

    assert payload["ok"] is True
    assert payload["exists"] is True
    assert payload["config"]["recording_source_default"]["source_dir"] == str(source_dir)
    assert payload["config"]["llm"]["api_key_env"] == "SECRET_LLM_KEY"
    assert payload["env_status"]["SECRET_LLM_KEY"] is True
    assert "sk-secret" not in str(payload)
    assert payload["config"]["web"]["access_token_configured"] is True
    assert "secret-token" not in str(payload)
    assert payload["config"]["scheduler"]["timezone"] == "Asia/Shanghai"
    assert payload["config"]["scheduler_jobs"][0]["id"] == "weekly_recording_scan"


def test_validate_editable_config_rejects_invalid_values_with_chinese_errors(tmp_path):
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    draft = {
        "paths": {"input_dir": str(source_dir / "input"), "output_root": str(source_dir / "input")},
        "recording_source_default": {
            "source_dir": str(tmp_path / "missing-nas"),
            "input_dir": str(source_dir / "input"),
            "output_root": str(source_dir / "input"),
            "since_hours": 0,
            "min_age_minutes": 0,
            "stable_check_seconds": 3,
        },
        "llm": {"api_key_env": "bad-name", "timeout_seconds": 10, "request_attempts": 99},
        "asr": {"backend": "cloud-only", "api_key_env": "bad-name"},
        "service": {"scan_interval_minutes": 0, "cleanup_mode": "delete"},
        "web": {"port": 80},
        "scheduler": {"timezone": "Mars/Base", "tick_seconds": 2, "missed_policy": "catch_all"},
        "scheduler_jobs": [
            {
                "id": "Bad Job!",
                "name": "",
                "enabled": True,
                "type": "ai_review",
                "schedule": "weekly",
                "day_of_week": "funday",
                "time": "25:61",
                "skip_if_running": True,
            },
            {
                "id": "fast_interval",
                "name": "过快间隔",
                "enabled": True,
                "type": "maintenance_check",
                "schedule": "interval_minutes",
                "interval_minutes": 1,
                "skip_if_running": True,
            },
        ],
    }

    result = validate_editable_config(draft, base_dir=tmp_path)

    assert result["ok"] is False
    messages = "\n".join(error["message"] for error in result["errors"])
    assert "录播源目录不存在" in messages
    assert "输入目录和输出目录不能相同" in messages
    assert "必须在 1 到 720 之间" in messages
    assert "环境变量名只能使用大写字母" in messages
    assert "清理模式目前只允许 preview_only" in messages
    assert "调度时区无效" in messages
    assert "missed_policy 只能是 run_once 或 skip" in messages
    assert "任务 id 只能使用小写字母" in messages
    assert "V5 不支持 AI 自动审阅任务" in messages
    assert "星期必须是 mon 到 sun" in messages
    assert "时间必须使用 HH:MM 格式" in messages
    assert "间隔分钟数必须在 5 到 1440 之间" in messages


def test_save_editable_config_creates_backup_and_writes_loadable_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source_dir = tmp_path / "nas"
    source_dir.mkdir()
    config_path = tmp_path / "live-clipper.toml"
    _write_config(config_path, source_dir)
    draft = load_editable_config(config_path=config_path)["config"]
    draft["service"]["scan_interval_minutes"] = 15
    draft["recording_source_default"]["min_age_minutes"] = 20
    draft["web"]["port"] = 9876
    draft["scheduler"]["timezone"] = "Asia/Tokyo"
    draft["scheduler_jobs"][0]["enabled"] = False

    result = save_editable_config(
        draft,
        config_path=config_path,
        backup_root=tmp_path / "work" / "config_backups",
        base_dir=tmp_path,
    )

    assert result["ok"] is True
    assert Path(result["backup_path"]).exists()
    settings = load_settings(config_path)
    assert settings.service.scan_interval_minutes == 15
    assert settings.recording_source_default.min_age_minutes == 20
    assert settings.web.port == 9876
    assert settings.scheduler.timezone == "Asia/Tokyo"
    assert settings.scheduler.jobs[0].enabled is False
    assert result["requires_service_restart"] is True
    assert result["requires_web_restart"] is True


def test_save_editable_config_refuses_to_overwrite_unparseable_toml(tmp_path):
    config_path = tmp_path / "live-clipper.toml"
    config_path.write_text("[service\nbroken = true", encoding="utf-8")

    result = save_editable_config(
        {"service": {"scan_interval_minutes": 15}},
        config_path=config_path,
        backup_root=tmp_path / "work" / "config_backups",
        base_dir=tmp_path,
    )

    assert result["ok"] is False
    assert "配置文件解析失败" in result["message"]
    assert config_path.read_text(encoding="utf-8") == "[service\nbroken = true"
