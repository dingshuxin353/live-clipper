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
