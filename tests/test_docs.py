from __future__ import annotations

import re
from pathlib import Path


def _frontend_source(*names: str) -> str:
    return "\n".join(Path("frontend/src", name).read_text(encoding="utf-8") for name in names)


def test_ai_assistant_guide_exists_and_covers_beginner_safety():
    guide = Path("docs/ai-assistant-guide.md")

    text = guide.read_text(encoding="utf-8")

    assert "# live-clipper 源码与命令行兼容流程 AI 使用说明" in text
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
    assert "桌面客户端在首次设置中选择语音识别方式" in text
    assert "源码/CLI 默认使用本地 MLX" in text
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


def test_advanced_usage_guides_local_asr_install_and_first_download():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "## 本地 ASR 安装" in text
    assert "mlx-community/whisper-large-v3-turbo" in text
    assert ".venv/bin/python -m pip install -e '.[dev,mlx]'" in text
    assert "首次运行会下载本地 ASR 模型" in text
    assert "本地 ASR 模型可用" in text


def test_advanced_usage_documents_service_core_commands_and_safety():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "## 本机常驻服务" in text
    assert ".venv/bin/live-clipper service start" in text
    assert ".venv/bin/live-clipper service status --json" in text
    assert "work/service/" in text
    assert "cleanup_mode = \"preview_only\"" in text
    assert "不会自动删除" in text
    assert "不会主动终止已经启动的 pipeline 子进程" in text


def test_advanced_usage_documents_mcp_tools_and_confirmation_safety():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "## MCP 工具面" in text
    assert "live_clipper.mcp_tools" in text
    assert "get_service_status" in text
    assert "write_selected_clips" in text
    assert "confirmation_required" in text
    assert "work/service/confirmations.json" in text
    assert "不会直接删除任何文件" in text


def test_web_static_exposes_v3_console_sections():
    app = _frontend_source("App.tsx")
    compatibility = _frontend_source("CompatibilityPages.tsx")
    settings = _frontend_source("Settings.tsx")

    for label in ["工作室", "项目", "成片", "资源", "设置", "＋ 新建项目"]:
        assert label in app
    for path in ["/studio", "/projects", "/clips", "/review", "/resources", "/settings"]:
        assert f'<Route path="{path}"' in app
    assert '<NavLink to="/clips"' in app
    assert '<NavLink to="/review"' not in app
    for label in ["资源", "待审", "修改处理资源"]:
        assert label in compatibility
    for label in ["配置状态", "基础设置", "自动化", "高级设置"]:
        assert label in settings
    for endpoint in ["/api/config", "/api/service", "/api/asr/models"]:
        assert endpoint in compatibility


def test_web_static_exposes_v4_config_editor():
    settings = _frontend_source("Settings.tsx")

    for label in ["基础设置", "录像目录", "AI 服务地址", "语音识别方式", "AI 判断完成后自动生成成片", "检查配置", "保存配置", "重启服务"]:
        assert label in settings
    assert "/api/config" in settings
    assert "/api/config/validate" in settings
    assert "/api/config/restart-service" in settings


def test_web_static_exposes_v5_scheduler_config_section():
    settings = _frontend_source("Settings.tsx")
    compatibility = _frontend_source("CompatibilityPages.tsx")

    for label in ["自动化", "每周录播扫描", "每周审阅检查", "按时间表自动扫描和检查（默认每周日）"]:
        assert label in settings
    for label in ["保存定时任务", "启用任务", "重启服务"]:
        assert label in settings
    assert "/api/scheduler" in compatibility
    assert "/api/scheduler/jobs" in settings
    assert "编辑高级定时任务" in settings
    assert "自动处理随 Venus 运行" in settings


def test_advanced_usage_documents_web_console_confirmation_flow():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")
    workbench = Path("docs/mcp-workbench-user-guide.md").read_text(encoding="utf-8")

    assert "高级 Web 兼容控制台" in text
    assert "`文件清理`" in text
    assert "Web 控制台 `文件清理` 页" in workbench
    assert "批量确认/拒绝" in text
    assert "work/service/confirmations.json" in text
    assert "NAS 原始录播不会被 Web 直接删除" in text


def test_advanced_usage_documents_web_config_editor():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "Web 配置页（兼容）" in text
    assert "`配置`" in text
    assert "检查配置" in text
    assert "保存配置" in text
    assert "work/config_backups/" in text
    assert "不会显示明文 API key" in text


def test_advanced_usage_documents_internal_scheduler_without_automatic_selection():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "内置定时调度（兼容）" in text
    assert "不再依赖 Codex 定时任务、cron 或 launchd" in text
    assert "每周日 00:00" in text
    assert "每周日 12:00" in text
    assert "只标记和提醒待审阅任务" in text
    assert "不会自动生成 selected_clips.json" in text


def test_web_static_exposes_v6_ai_review_automation_controls():
    app = _frontend_source("App.tsx")
    compatibility = _frontend_source("CompatibilityPages.tsx")
    settings = _frontend_source("Settings.tsx")

    assert '<Route path="/review"' in app
    assert "待审" in compatibility
    for label in ["AI 审阅", "让 AI 自动选片（不用人工挑）", "启用 AI 自动判断前", "AI 判断完成后自动生成成片"]:
        assert label in settings
    assert "/api/review-automation" in compatibility
    assert 'review_automation.enabled' in settings
    assert '"ai_review"' in settings


def test_web_static_exposes_v7_layered_config_page():
    html = _frontend_source("Settings.tsx")

    for label in ["配置状态", "基础设置", "自动化", "高级设置"]:
        assert label in html

    quick_start = html.split('data-config-layer="quick-start"', 1)[1].split('data-config-layer="automation"', 1)[0]
    for advanced_label in ["tick 秒数", "Temperature", "Max tokens"]:
        assert advanced_label not in quick_start

    automation = html.split('data-config-layer="automation"', 1)[1].split('data-config-layer="advanced"', 1)[0]
    for label in ["按时间表自动扫描和检查（默认每周日）", "每周录播扫描", "让 AI 自动选片（不用人工挑）", "启用 AI 自动判断前", "使用下方开关和定时任务管理自动处理"]:
        assert label in automation

    advanced = html.split('data-config-layer="advanced"', 1)[1]
    for label in ["存储与扫描", "模型请求", "服务与调度", "AI 审阅参数", "Web 控制台"]:
        assert label in advanced
    for field in [
        "scheduler.tick_seconds",
        "review_automation_model.temperature",
        "review_automation_model.max_tokens",
        "web.host",
    ]:
        assert field in advanced


def test_advanced_usage_documents_layered_config_page():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "配置页分层（兼容）" in text
    assert "配置体检" in text
    assert "快速开始" in text
    assert "高级设置默认收起" in text


def test_advanced_usage_documents_ai_review_safety_and_modes():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "AI 自动审阅（兼容）" in text
    assert "默认不会静默启用" in text
    assert "Codex CLI" in text
    assert "Claude Code" in text
    assert "配置模型直连" in text
    assert "validate_selected_clips_file" in text
    assert "AI 不会直接删除文件" in text
    assert "不会执行 cleanup confirm" in text
    assert "不会 approve/reject confirmation" in text


def test_web_static_exposes_local_asr_model_manager():
    app = _frontend_source("Settings.tsx")

    for label in [
        "本地语音模型",
        "模型下载源",
        "ModelScope（中国大陆推荐）",
        "Hugging Face（国际官方）",
    ]:
        assert label in app
    assert "/api/asr/models" in app


def test_readme_explains_product_level_local_model_capability():
    text = Path("README.md").read_text(encoding="utf-8")

    for expected in [
        "ModelScope（中国大陆推荐）",
        "Hugging Face（国际官方）",
        "约 187 MB",
        "约 489 MB",
        "约 1.6 GB",
    ]:
        assert expected in text
    assert "本地语音识别可以离线运行" in text
    assert "AI 审阅是否联网" in text
    assert "镜像下载源" not in text


def test_english_readme_explains_current_local_model_choices():
    text = Path("docs/README.en.md").read_text(encoding="utf-8")

    for expected in [
        "ModelScope",
        "Hugging Face",
        "about 187 MB",
        "about 489 MB",
        "about 1.6 GB",
    ]:
        assert expected in text
    assert "recommended local speech model" not in text
    assert "Hugging Face or a mirror" not in text


def test_public_readmes_describe_the_1_0_0_desktop_flow():
    chinese = Path("README.md").read_text(encoding="utf-8")
    english = Path("docs/README.en.md").read_text(encoding="utf-8")

    for expected in [
        "首次设置或安全升级",
        "手动或定时发现录像",
        "自动转写、分析、AI 审阅和渲染",
        "修复问题或重新处理",
    ]:
        assert expected in chinese
    for expected in [
        "First-time setup or a safe upgrade",
        "Find recordings manually or on a schedule",
        "Transcribe, analyze, review with AI, and render automatically",
        "Fix issues or reprocess a recording",
    ]:
        assert expected in english
    for obsolete in ["待审阅", "启动 AI 审阅", "自动化中心", "切片结果"]:
        assert obsolete not in chinese
    for obsolete in ["needs-review", "Start AI review", "Automation center", "Clip Results"]:
        assert obsolete not in english


def test_advanced_documents_mark_non_desktop_paths_explicitly():
    advanced = Path("docs/advanced-usage.md").read_text(encoding="utf-8")
    assistant = Path("docs/ai-assistant-guide.md").read_text(encoding="utf-8")
    mcp = Path("docs/mcp-workbench-user-guide.md").read_text(encoding="utf-8")
    workflow = Path("docs/workflow.md").read_text(encoding="utf-8")
    web = Path("docs/web-console.md").read_text(encoding="utf-8")

    assert "高级与兼容流程" in advanced
    assert "源码与命令行兼容流程" in assistant
    assert "高级 MCP 兼容流程" in mcp
    assert "源码与命令行兼容工作流" in workflow
    assert "高级 Web 兼容控制台" in web


def test_changelog_documents_only_verified_0_3_2_changes():
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    section = text.split("## 0.3.2 - 2026-08-04", 1)[1].split("## 0.3.1", 1)[0]

    for expected in [
        "Migrated the desktop renderer to React 19, TypeScript, and Vite while preserving the existing local APIs and workflows.",
        "Unified navigation, forms, dialogs, lists, model controls, and status feedback on Astryx Stone with Venus brand tokens and MiSans.",
        "Added responsive navigation and layout behavior for minimum window sizes and increased zoom.",
        "Fixed onboarding validation so blocked actions explain the problem and focus the relevant field.",
        "Fixed inconsistent disabled and busy states, oversized notice banners, narrow-layout clipping, and the ambiguous file-cleanup navigation.",
        "Localized built-in accessibility labels and restored the MiSans heading theme tokens.",
        "Prevented onboarding API keys from being serialized into page HTML during React rerenders.",
    ]:
        assert f"- {expected}" in section

    for forbidden in [
        "Python backend migrated to Node.js",
        "ASR Simplified/Traditional Chinese",
        "automatic update verified",
        "Apple notarization completed",
        "0.3.2 has been released",
    ]:
        assert forbidden not in section


def test_changelog_documents_only_implemented_0_3_3_changes():
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    section = text.split("## 0.3.3 - 2026-08-06", 1)[1].split("## 0.3.2", 1)[0]

    for expected in [
        "complete streaming SHA-256",
        "single-concurrency queue",
        "newly discovered, queued, duplicate, too-new, and still-changing recordings",
        "before creating a run, copying a recording, or starting the pipeline",
        "Failed runs can now be retried manually",
        "concurrent scans serialize run-state mutations",
        "Stone semantic colors",
    ]:
        assert expected in section

    for forbidden in [
        "has been released",
        "long recording verified",
        "zero collisions",
        "collision-free",
    ]:
        assert forbidden not in section


def test_readmes_document_1_0_0_project_processing():
    chinese = Path("README.md").read_text(encoding="utf-8")
    english = Path("docs/README.en.md").read_text(encoding="utf-8")

    for expected in [
        "项目制工作台",
        "自动完成转写、分析、AI 审阅、字幕和成片渲染",
        "成片页查看视频、AI 判断、标题、简介和其他发布物料",
        "从原录像建立新版本",
    ]:
        assert expected in chinese
    assert "Node.js 24" in chinese
    assert "Node.js 20" not in chinese
    for expected in [
        "Project workbench",
        "transcribe, analyze, review with AI, subtitle, and render",
        "Clips and publishing material",
        "Reprocessing",
    ]:
        assert expected in english


def test_advanced_usage_documents_model_source_details():
    text = Path("docs/advanced-usage.md").read_text(encoding="utf-8")

    assert "设置页可直接下载本地语音模型" in text
    assert "ModelScope 中国大陆推荐" in text
    assert "Hugging Face 国际官方" in text
    assert "mlx-community/whisper-large-v3-turbo" in text
    assert ".venv/bin/python -m pip install -e '.[dev,mlx]'" in text


def test_readme_is_a_product_homepage():
    text = Path("README.md").read_text(encoding="utf-8")

    for expected in [
        '<h1 align="center">Venus</h1>',
        "美神直播剪辑工作台",
        "把长直播整理成可查看、可恢复、可继续处理的短视频成片。",
        "面向主播和内容团队的 macOS 客户端：按项目发现录像，自动完成转写、AI 审阅和渲染。",
        "下载最新版",
        "## 1.0.0 主流程",
        "## 从 0.3.x 升级",
        "## 界面预览",
        "## 主要能力",
        "## 快速开始",
        "## 常见问题",
        "Apple Silicon",
        "macOS 14",
        "https://github.com/dingshuxin353/live-clipper/releases/latest",
        "docs/privacy.md",
    ]:
        assert expected in text

    assert text.count("<h1") == 1
    assert "把一场长直播，变成一组可审阅、可发布的短视频。" not in text
    assert "发现录播、语音转写、AI 选片、生成字幕并渲染成片。" not in text


def test_readme_documents_collapsed_developer_setup():
    text = Path("README.md").read_text(encoding="utf-8")

    for expected in [
        "## 开发者部署",
        "<details>",
        "<summary><strong>从源码运行与本地构建</strong></summary>",
        "python3.11 -m venv .venv",
        ".venv/bin/python -m pip install -e '.[dev,mlx]'",
        "npm ci",
        "npm start",
        "npm run dist",
        "docs/advanced-usage.md",
        "CONTRIBUTING.md",
    ]:
        assert expected in text


def test_readme_relative_links_exist():
    readme = Path("README.md")
    text = readme.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)]+)\)", text)
    targets.extend(re.findall(r'(?:href|src)="([^"]+)"', text))

    for target in targets:
        path = target.split("#", 1)[0]
        if not path or path.startswith(("http://", "https://", "mailto:")):
            continue
        assert (readme.parent / path).exists(), target


def test_readme_screenshots_exist_and_are_substantial():
    text = Path("README.md").read_text(encoding="utf-8")
    screenshots = [
        Path("docs/assets/readme/venus-studio.png"),
        Path("docs/assets/readme/venus-project.png"),
        Path("docs/assets/readme/venus-results.png"),
    ]

    for screenshot in screenshots:
        assert screenshot.as_posix() in text
        assert screenshot.is_file()
        assert screenshot.stat().st_size > 50 * 1024


def test_readme_length_and_forbidden_content():
    text = Path("README.md").read_text(encoding="utf-8")
    line_count = len(text.splitlines())

    assert 180 <= line_count <= 280
    for forbidden in [
        "## 更新记录",
        "### 0.2.0",
        "### 0.3.0",
        "CHANGELOG.md",
        "V3 Web 控制台",
        "V4 Web 配置页",
        "V5 内置定时调度",
        "V6 AI 自动审阅",
        "V7 配置页分层",
        "赞助商",
        "优惠码",
        "Star History",
    ]:
        assert forbidden not in text
