# 隐私说明

live-clipper 默认在本地读写输入视频、音频中间文件、转录文本、候选片段 JSON 和渲染结果。

以下配置可能把数据发送到第三方服务：

- `ASR_BACKEND=openai`: 上传音频到配置的 ASR API。
- LLM 扫描、校对或复评阶段：发送转录文本、候选窗口和上下文到配置的 OpenAI-compatible LLM API。

失败日志默认使用 `redacted` 模式，隐藏 prompt、payload 和模型响应正文。调试时可以在 `live-clipper.toml` 中改为：

```toml
[privacy]
failure_log_mode = "full"
```

不要公开分享包含真实转录或 API 响应的 `work/logs` 文件。
