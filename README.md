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

## 小白推荐：让 AI 陪你配置

如果你不熟悉 Python、终端或配置文件，建议先走这条路径：

1. 打开 [docs/ai-assistant-guide.md](docs/ai-assistant-guide.md)。
2. 把全文复制给你常用的 AI 助手。
3. 按 AI 的问题一步步回答，把终端输出贴回去。
4. 让 AI 帮你完成 `.env`、`live-clipper.toml`、首次运行和 Agent 定时任务配置。

也可以在终端里输出这份说明：

```bash
.venv/bin/live-clipper guide ai
```

安全提醒：不要把 API key、Token、Cookie 或任何密钥粘贴到聊天窗口。只把它们写进你本机的 `.env` 文件。

## 命令行快速开始

准备 Python 3.11+ 和 `ffmpeg`。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/live-clipper setup
.venv/bin/live-clipper smoke
```

如果使用 Apple Silicon 本地 ASR，安装时可使用 MLX extra：

```bash
.venv/bin/python -m pip install -e '.[dev,mlx]'
```

`smoke` 会生成合成视频并跑完整本地链路，不调用远程 ASR 或 LLM。

## 本地 ASR 安装

当前版本默认使用本地 ASR，把直播音频转成带时间戳的文字稿。本地 ASR 不使用你的 LLM API key；LLM 只负责后续文字校对、候选片段判断和复评。

Apple Silicon 推荐安装 MLX extra：

```bash
.venv/bin/python -m pip install -e '.[dev,mlx]'
```

默认本地 ASR 模型是：

```toml
[asr]
backend = "mlx_whisper"
model = "mlx-community/whisper-large-v3-turbo"
language = "zh"
```

首次运行会下载本地 ASR 模型，可能比较慢，也会占用本机磁盘空间。想先确认模型能下载和运行，可以做一次预热：

```bash
mkdir -p work/asr_model_check
ffmpeg -y -f lavfi -i sine=frequency=440:duration=1 -ar 16000 -ac 1 work/asr_model_check/tone.wav
.venv/bin/python - <<'PY'
import mlx_whisper

mlx_whisper.transcribe(
    "work/asr_model_check/tone.wav",
    path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
    language="zh",
)
print("本地 ASR 模型可用")
PY
```

如果提示没有 `mlx_whisper`，请确认安装命令里包含 `mlx` extra。如果模型下载失败，优先检查网络或 Hugging Face 访问；这通常不是 `CHEAP_MODEL_API_KEY` 的问题。

## 常用命令

```bash
.venv/bin/live-clipper doctor
.venv/bin/live-clipper smoke
.venv/bin/live-clipper guide ai
.venv/bin/live-clipper setup
.venv/bin/live-clipper next
.venv/bin/live-clipper service start
.venv/bin/live-clipper service status --json
.venv/bin/live-clipper service logs
.venv/bin/live-clipper service stop
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
- `guide ai`: 输出可复制给 AI 助手的中文陪跑说明。
- `setup`: 创建 `.env`、`live-clipper.toml`、常用目录和提示词模板。
- `next`: 告诉你当前 output 里的任务下一步该做什么。
- `service start/stop/status/logs`: 管理本机常驻服务。
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

## 本机常驻服务

V1 提供一个本机常驻服务，用来替代手写的定时提示词编排。服务会按配置扫描录播目录，把稳定录制复制到本地 `input/`，启动现有 pipeline，等 `selected_clips.json` 出现后自动渲染。

常用命令：

```bash
.venv/bin/live-clipper service start
.venv/bin/live-clipper service start --foreground
.venv/bin/live-clipper service start --once
.venv/bin/live-clipper service status --json
.venv/bin/live-clipper service logs
.venv/bin/live-clipper service stop
```

服务状态写在：

```text
work/service/
  service.pid
  service.json
  service.log
  runs.json
  events.jsonl
```

配置示例：

```toml
[service]
enabled = true
scan_interval_minutes = 30
auto_render_after_selection = true
cleanup_mode = "preview_only"

[recording_source.default]
source_dir = "/Volumes/homes/weixiaodan12/录播"
input_dir = "input"
output_root = "output"
since_hours = 168
min_age_minutes = 10
stable_check_seconds = 60
```

安全边界：

- 服务不会自动删除 NAS 原始录制。
- 服务不会自动删除本地输入副本。
- 服务不会自动执行 `cleanup --confirm`。
- `service stop` 只停止服务主进程，不会主动终止已经启动的 pipeline 子进程。
- V1 只会在渲染完成后做 cleanup preview，并把状态记录到本地文件。

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

## Agent 定时自动化

无人值守流程建议用当前 Agent 软件的定时任务能力做编排，例如 Codex 的定时任务、其他 Agent 的周期任务，或系统 `cron`、macOS `launchd`、Windows 任务计划程序。`live-clipper` 负责确定性的本地工作，并在需要判断时写出明确文件。

建议拆成两个定时任务：

1. **录制检测任务**：寻找新的稳定录制文件，并启动后台流水线。
2. **Agent 决策任务**：检查已完成或失败的 run，读取 `codex_task.md`，执行选片、渲染、失败诊断或清理预演。这里的 `codex_task.md` 是项目内部文件名，不代表只能使用 Codex。

不要把两件事塞进一个大提示词。录制检测可以围绕直播结束后的时间段运行；Agent 决策可以在处理窗口内更频繁运行。

推荐时间：

- 录制检测任务：直播结束后几个小时内，每 30-60 分钟运行一次。
- Agent 决策任务：预计录制被拾取后，每 15-30 分钟运行一次。
- 非发布日可以暂停两个定时任务。

### 定时任务 1：录制检测

在你的 Agent 软件中创建一个定时任务，工作目录选择本项目目录。如果当前 Agent 不支持定时任务，可以把这段命令交给系统计划任务执行。

提示词模板：

```text
你是 live-clipper 的录制检测助手。

你的任务是：检查是否有新的直播录制文件。如果有，就启动后台处理流程；如果没有，就简短说明当前没有可处理录制。

请在项目目录中运行：

  .venv/bin/live-clipper automation start-latest \
    --source-dir /path/to/recordings \
    --input-dir input \
    --output-root output \
    --since-hours 36 \
    --min-age-minutes 10 \
    --top-n 25

执行规则：
- 不要重复启动已经在运行的任务。
- 不要删除或修改录制源目录里的原始视频。
- 如果命令显示任务已启动，请告诉我 run 目录、日志文件、状态文件和进程 PID。
- 如果没有发现录制文件，请简短说明，不要创建无关文件。
- 如果失败，请解释失败原因，并给出下一步我应该怎么处理。
```

把 `/path/to/recordings` 替换成你的录制目录。如果希望先做转录校对，追加 `--correct-transcript`。如果想跳过复评，追加 `--no-refine`。

### 定时任务 2：Agent 决策

创建第二个 Agent 定时任务，工作目录同样选择本项目目录。

提示词模板：

```text
你是 live-clipper 的选片与收尾助手。

你的任务是：检查 live-clipper 是否有需要 Agent 判断的任务，并按任务文件要求处理。

第一步，请运行：

  .venv/bin/live-clipper automation check --output-root output

然后阅读 JSON 结果。

如果 requires_codex 是 false：
- 告诉我“当前没有需要 Agent 处理的任务”。
- 不要修改文件。

如果 codex_tasks 里有任务：
- 对每个任务，先打开 codex_task_file。
- 严格按 codex_task.md 的要求执行。
- 注意：requires_codex、codex_tasks、codex_task_file 是当前项目里的字段名，不代表只能用 Codex。

当 phase 是 needs_codex_selection：
- 阅读 run 目录里的 codex_brief.json。
- 如果 refined_candidates.json 存在，也一起参考。
- 选择适合发布的片段。
- 写入 selected_clips.json。
- 写完后运行：

  .venv/bin/live-clipper render <run_dir>/selected_clips.json

当 phase 是 failed_needs_codex：
- 查看日志尾部和 run 目录已有文件。
- 优先判断能否安全 resume。
- 不要删除原始视频。
- 给出明确的恢复命令或人工处理建议。

当 phase 是 cleanup_ready：
- 先运行：

  .venv/bin/live-clipper cleanup <run_dir>

- 只有确认预演结果只会删除本地副本和中间文件、不会删除原始录制时，才可以运行带 --confirm 的清理命令。

输出要求：
- 用中文总结处理了哪些 run。
- 列出创建或修改的文件。
- 如果有需要我人工确认的地方，明确写出来。
```

Agent 的介入信号是文件状态：

- `codex_brief.json` 已存在；
- `selected_clips.json` 不存在；
- `automation check` 标记 run 为 `needs_codex_selection`；
- run 目录中出现 `codex_task.md`。

用户可以通过三个地方感知：

- 当前 Agent 定时任务输出中的 `requires_codex: true`；
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
