from __future__ import annotations

from pathlib import Path


def test_ai_assistant_guide_exists_and_covers_beginner_safety():
    guide = Path("docs/ai-assistant-guide.md")

    text = guide.read_text(encoding="utf-8")

    assert "# live-clipper AI 使用说明" in text
    assert "不要把 API key" in text
    assert "录制检测任务" in text
    assert "选片与收尾任务" in text
    assert "配置完成清单" in text


def test_ai_assistant_guide_tells_ai_to_detect_environment_before_questions():
    text = Path("docs/ai-assistant-guide.md").read_text(encoding="utf-8")

    assert "先自动检测" in text
    assert "不要询问用户电脑系统" in text
    assert "不要询问用户是否安装了 Python" in text
    assert "安装前必须先说明目的" in text
    assert "征得用户同意" in text
    assert "当前只支持本机 ASR" in text
    assert "不要询问用户是否使用云端 ASR" in text
    assert "`.env` 写模型服务密钥" in text
    assert "`live-clipper.toml` 写非敏感配置" in text


def test_ai_assistant_guide_does_not_ask_environment_or_cloud_asr_questions():
    text = Path("docs/ai-assistant-guide.md").read_text(encoding="utf-8")

    assert "你是否已经安装 Python" not in text
    assert "你是否已经安装 ffmpeg" not in text
    assert "你的电脑系统是什么" not in text
    assert "还是云端服务" not in text


def test_ai_assistant_guide_explains_llm_examples_asr_download_and_agent_schedules():
    text = Path("docs/ai-assistant-guide.md").read_text(encoding="utf-8")

    assert "模型服务是用来做文字理解和判断" in text
    assert "火山方舟" in text
    assert "阿里百炼" in text
    assert "Agnes" in text
    assert "本地 ASR 模型下载" in text
    assert "mlx-community/whisper-large-v3-turbo" in text
    assert "先说明下载目的" in text
    assert "不要把定时任务限定为 Codex" in text
    assert "根据当前运行的 Agent 软件判断" in text


def test_readme_guides_local_asr_install_and_first_download():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "## 本地 ASR 安装" in text
    assert "mlx-community/whisper-large-v3-turbo" in text
    assert ".venv/bin/python -m pip install -e '.[dev,mlx]'" in text
    assert "首次运行会下载本地 ASR 模型" in text
    assert "本地 ASR 模型可用" in text


def test_readme_documents_service_core_commands_and_safety():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "## 本机常驻服务" in text
    assert ".venv/bin/live-clipper service start" in text
    assert ".venv/bin/live-clipper service status --json" in text
    assert "work/service/" in text
    assert "cleanup_mode = \"preview_only\"" in text
    assert "不会自动删除" in text
    assert "不会主动终止已经启动的 pipeline 子进程" in text


def test_readme_documents_mcp_tools_and_confirmation_safety():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "## MCP 工具面" in text
    assert "live_clipper.mcp_tools" in text
    assert "get_service_status" in text
    assert "write_selected_clips" in text
    assert "confirmation_required" in text
    assert "work/service/confirmations.json" in text
    assert "不会直接删除任何文件" in text


def test_web_static_exposes_v3_console_sections():
    html = Path("src/live_clipper/web_static/index.html").read_text(encoding="utf-8")
    app = Path("src/live_clipper/web_static/app.js").read_text(encoding="utf-8")

    for label in ["服务", "任务", "确认", "日志", "配置"]:
        assert label in html
    assert "/api/confirmations/batch-approve" in app
    assert "/api/service/scan-now" in app
    assert "confirmation_required" in app


def test_web_static_exposes_v4_config_editor():
    html = Path("src/live_clipper/web_static/index.html").read_text(encoding="utf-8")
    app = Path("src/live_clipper/web_static/app.js").read_text(encoding="utf-8")

    for label in ["基础路径", "录播源", "AI 与 ASR", "服务行为", "检查配置", "保存配置", "重启服务"]:
        assert label in html
    assert "/api/config" in app
    assert "/api/config/validate" in app
    assert "/api/config/restart-service" in app


def test_web_static_exposes_v5_scheduler_config_section():
    html = Path("src/live_clipper/web_static/index.html").read_text(encoding="utf-8")
    app = Path("src/live_clipper/web_static/app.js").read_text(encoding="utf-8")

    for label in ["定时任务", "每周录播扫描", "每周审阅检查", "立即执行", "暂停", "启用"]:
        assert label in html
    assert "/api/scheduler" in app
    assert "/api/scheduler/jobs" in app
    assert "/run-now" in app
    assert "AI 自动审阅将在后续版本配置" in html


def test_readme_documents_v3_web_console_confirmation_flow():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "V3 Web 控制台" in text
    assert "`确认`" in text
    assert "批量确认/拒绝" in text
    assert "work/service/confirmations.json" in text
    assert "NAS 原始录播不会被 Web 直接删除" in text


def test_readme_documents_v4_web_config_editor():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "V4 Web 配置页" in text
    assert "`配置`" in text
    assert "检查配置" in text
    assert "保存配置" in text
    assert "work/config_backups/" in text
    assert "不会显示明文 API key" in text


def test_readme_documents_v5_internal_scheduler_without_v6_ai_review():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "V5 内置定时调度" in text
    assert "不再依赖 Codex 定时任务、cron 或 launchd" in text
    assert "每周日 00:00" in text
    assert "每周日 12:00" in text
    assert "只标记和提醒待审阅任务" in text
    assert "不会自动生成 selected_clips.json" in text
