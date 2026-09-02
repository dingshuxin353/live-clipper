# 配置说明

本文面向源码、CLI 和兼容 Web 流程。Venus 1.0.0 桌面客户端会通过首次设置与“设置”页面管理这些配置。

`live-clipper config init` 会生成 `live-clipper.toml`。推荐把非敏感配置写在 TOML 文件里，把 API key 留在 `.env` 或 shell 环境变量里。

配置分组：

- `paths`: 输入、输出、工作、缓存、日志、状态和术语表路径。
- `recording_source`: 定时任务查找录制文件的来源目录和时间窗口。
- `asr`: ASR 后端、模型、语言和云端 API 设置。
- `llm`: OpenAI-compatible LLM 服务地址、模型、重试和超时。
- `prompts`: 用户自定义提示词目录。
- `privacy`: 失败日志模式。
- `web`: 本地控制台 host 和端口。
- `review`: Codex/人工审阅相关文件名。
- `render`: 渲染输出设置。
