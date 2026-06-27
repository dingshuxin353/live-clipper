# live-clipper

本地直播切片流水线：把长直播录制转成可审阅的候选片段，再渲染成短视频高光。

English readers can start from [docs/README.en.md](docs/README.en.md). 当前主文档以中文为准。

## 它解决什么问题

`live-clipper` 适合创作者、运营或内容团队在本机处理长视频：

1. 从直播录制提取音频。
2. 生成带时间戳的转录。
3. 用 OpenAI-compatible LLM 做 ASR 校对、候选扫描和可选复评。
4. 生成候选审阅包。
5. 由 Codex 或人工写入最终选择。
6. 用 `ffmpeg` 渲染成片和字幕。

默认产物都写在本地目录。只有当你配置云端 ASR 或 LLM 服务时，音频或转录文本才会发送到对应服务。详见 [docs/privacy.md](docs/privacy.md)。

## 快速开始

准备 Python 3.11+ 和 `ffmpeg`。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/live-clipper config init
.venv/bin/live-clipper prompts export --output prompts.local
.venv/bin/live-clipper smoke
```

如果使用 Apple Silicon 本地 ASR，安装时可使用 MLX extra：

```bash
.venv/bin/python -m pip install -e '.[dev,mlx]'
```

`smoke` 会生成合成视频并跑完整本地链路，不调用远程 ASR 或 LLM。

## 常用命令

```bash
.venv/bin/live-clipper doctor
.venv/bin/live-clipper smoke
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023 --resume
.venv/bin/live-clipper refine output/week_023
.venv/bin/live-clipper brief output/week_023 --source refined
.venv/bin/live-clipper render output/week_023/selected_clips.json
.venv/bin/live-clipper cleanup output/week_023
```

命令说明：

- `doctor`: 检查 ffmpeg、输入视频、ASR 和 LLM 配置。
- `smoke`: 本地烟测。
- `scan`: 生成音频、转录、窗口和候选。
- `scan --resume`: 复用已有中间文件，从断点继续。
- `refine`: 用 LLM 对候选做二次复评。
- `brief`: 生成 `codex_brief.json`、`codex_review.md` 和 `selected_clips.template.json`。
- `render`: 根据 `selected_clips.json` 渲染成片。
- `cleanup`: 预演或清理本地中间大文件。

## 配置

推荐用统一配置文件管理非敏感项：

```bash
.venv/bin/live-clipper config init
```

这会生成 `live-clipper.toml`。API key 仍建议放在 `.env` 或 shell 环境变量里。

最常用配置：

```toml
[paths]
input_dir = "input"
output_root = "output"

[asr]
backend = "mlx_whisper"
model = "mlx-community/whisper-large-v3-turbo"
language = "zh"

[llm]
api_base = "https://apihub.agnes-ai.com/v1"
api_key_env = "CHEAP_MODEL_API_KEY"
model = "agnes-2.0-flash"

[prompts]
directory = "prompts.local"

[privacy]
failure_log_mode = "redacted"
```

更多说明见 [docs/configuration.md](docs/configuration.md)。

## 提示词自定义

导出默认提示词：

```bash
.venv/bin/live-clipper prompts export --output prompts.local
```

编辑 `prompts.local/*.md` 后，在 `live-clipper.toml` 中启用：

```toml
[prompts]
directory = "prompts.local"
```

也可以对单次命令指定：

```bash
.venv/bin/live-clipper scan input/week_023_live.mp4 --prompt-dir prompts.local
```

当前运行会在 `run_metadata.json` 中记录提示词来源。详见 [docs/prompts.md](docs/prompts.md)。

## 完整流程

运行扫描：

```bash
.venv/bin/live-clipper scan input/week_023_live.mp4 --output-dir output/week_023
```

典型输出：

```text
output/week_023/
  run_metadata.json
  audio.wav
  transcript_raw.json
  transcript.json
  windows.json
  cheap_candidates.json
  merged_candidates.json
```

可选复评：

```bash
.venv/bin/live-clipper refine output/week_023
```

生成审阅包：

```bash
.venv/bin/live-clipper brief output/week_023 --source refined
```

这会写入：

```text
output/week_023/
  codex_brief.json
  codex_review.md
  selected_clips.template.json
```

当 Codex 或人工写入 `selected_clips.json` 后，渲染：

```bash
.venv/bin/live-clipper render output/week_023/selected_clips.json
```

`selected_clips.json` 示例：

```json
[
  {
    "clip_id": "w0001-c001",
    "source_start": 12.5,
    "source_end": 58.0,
    "title": "A concise clip title",
    "remove_ranges": [[25.0, 28.0]]
  }
]
```

`remove_ranges` 会在渲染时移除片段内部的小段内容。范围必须在 `source_start` 和 `source_end` 内，不能重叠，并且不能删空整个片段。

## Codex 定时自动化

无人值守流程建议用 Codex 定时任务做编排。`live-clipper` 负责确定性的本地工作，并在需要判断时写出明确文件。

建议拆成两个定时任务：

1. **录制检测任务**：寻找新的稳定录制文件，并启动后台流水线。
2. **Codex 决策任务**：检查已完成或失败的 run，读取 `codex_task.md`，执行选片、渲染、失败诊断或清理预演。

不要把两件事塞进一个大提示词。录制检测可以围绕直播结束后的时间段运行；Codex 决策可以在处理窗口内更频繁运行。

推荐时间：

- 录制检测任务：直播结束后几个小时内，每 30-60 分钟运行一次。
- Codex 决策任务：预计录制被拾取后，每 15-30 分钟运行一次。
- 非发布日可以暂停两个定时任务。

### 定时任务 1：录制检测

在 Codex 中创建一个定时任务，工作目录选择本项目目录。

提示词模板：

```text
You are maintaining the live-clipper workspace.

Check whether there is a new stable recording and start the local pipeline if appropriate.

Run:

  .venv/bin/live-clipper automation start-latest \
    --source-dir /path/to/recordings \
    --input-dir input \
    --output-root output \
    --since-hours 36 \
    --min-age-minutes 10 \
    --top-n 25

Rules:
- Do not start a duplicate job if the command reports that a matching run is already running or already has a candidate package.
- Do not delete or modify original recordings in the source directory.
- If a job starts, report the run directory, state file, log file, and PID.
- If no recording is ready, report the command result briefly.
- If the command fails, inspect the error and summarize the next manual action.
```

把 `/path/to/recordings` 替换成你的录制目录。如果希望先做转录校对，追加 `--correct-transcript`。如果想跳过复评，追加 `--no-refine`。

### 定时任务 2：Codex 决策

创建第二个 Codex 定时任务，工作目录同样选择本项目目录。

提示词模板：

```text
You are the Codex decision worker for live-clipper.

First run:

  .venv/bin/live-clipper automation check --output-root output

Read the JSON response carefully.

If `requires_codex` is false:
- Report that there is currently no Codex action needed.
- Do not modify files.

For each item in `codex_tasks`:
- Open `codex_task_file` and follow its instructions.
- If the phase is `needs_codex_selection`, read the referenced `codex_brief.json` and `refined_candidates.json` when present, choose publishable clips, and write `selected_clips.json` in the run directory.
- After writing `selected_clips.json`, run:

  .venv/bin/live-clipper render <run_dir>/selected_clips.json

- If the phase is `failed_needs_codex`, inspect the log tail and run directory. Prefer a safe resume command when the failure is retryable. Do not delete source recordings.
- If the phase is `cleanup_ready`, run cleanup preview first:

  .venv/bin/live-clipper cleanup <run_dir>

  Only run cleanup with `--confirm` if the preview says it will delete local input copies or intermediate files and preserve the original recording.

Output:
- Summarize every run you touched.
- List files created or modified.
- Include any commands that should be retried manually.
```

Codex 的介入信号是文件状态：

- `codex_brief.json` 已存在；
- `selected_clips.json` 不存在；
- `automation check` 标记 run 为 `needs_codex_selection`；
- run 目录中出现 `codex_task.md`。

用户可以通过三个地方感知：

- Codex 定时任务输出中的 `requires_codex: true`；
- run 目录里的 `codex_task.md`；
- Web 控制台里的 `Codex 选择` 等待状态。

## Web 控制台

默认只允许本机访问：

```bash
.venv/bin/live-clipper web
```

打开 `http://127.0.0.1:8765`。

如果确实需要局域网访问：

```bash
.venv/bin/live-clipper web --host 0.0.0.0
```

不要把 Web 控制台暴露到公网。它包含渲染、清理和删除本地文件的操作。

## 术语表

ASR 校对会优先读取 `glossary/common_terms.json`。首次使用可以复制示例：

```bash
cp glossary/common_terms.example.json glossary/common_terms.json
```

示例：

```json
{
  "canonical": "Codex",
  "common_mistakes": ["code x", "扣得克斯", "codec"],
  "notes": "OpenAI coding agent product name"
}
```

## 开发与贡献

- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 安全策略：[SECURITY.md](SECURITY.md)
- 更新记录：[CHANGELOG.md](CHANGELOG.md)
- 许可证：[LICENSE](LICENSE)

本地验证：

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
```

