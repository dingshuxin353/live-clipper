from __future__ import annotations

import re
from pathlib import Path


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


def test_shipped_ai_guide_matches_documentation_and_links_resolve():
    from live_clipper.ai_guide import AI_ASSISTANT_GUIDE

    assert Path("docs/ai-assistant-guide.md").read_text() == AI_ASSISTANT_GUIDE
    for name in ("configuration", "workflow", "advanced-usage", "mcp-workbench-user-guide"):
        path = Path(f"docs/{name}.md")
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            relative = target.split("#", 1)[0]
            if relative and not relative.startswith(("https://", "http://")):
                assert (path.parent / relative).exists(), (path, target)
