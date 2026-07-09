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

如果你想使用当前 MCP 工作台版本，尤其是想知道“如何让 AI 选片”，请看 [docs/mcp-workbench-user-guide.md](docs/mcp-workbench-user-guide.md)。

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
source_dir = "/Volumes/your-nas/recordings"
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

## MCP 工具面

V2 提供 MCP 工具函数层，供 Agent 或后续 MCP server wrapper 调用。它是本机常驻服务的 thin adapter：不另建状态库，不重新推断 `output/` 状态，所有有意义动作都会复用 Service Core，并写入同一套 `work/service/` 状态与事件。

工具入口在 `live_clipper.mcp_tools`：

```python
from live_clipper import mcp_tools

status = mcp_tools.call_tool("get_service_status", {})
runs = mcp_tools.call_tool("list_runs", {"phase": "needs_review"})
```

Read tools：

- `get_service_status`
- `list_runs`
- `get_run_detail`
- `get_run_log`
- `get_review_package`

Safe action tools：

- `scan_now`
- `start_run_for_source`
- `write_selected_clips`
- `render_run`
- `preview_cleanup`

Confirmation-required tools：

- `delete_clip`
- `cleanup_confirm`
- `delete_local_source`

删除意图工具不会直接删除任何文件，只会创建：

```text
work/service/confirmations.json
```

并返回 `confirmation_required`。真正的 approve/reject 和批量确认留给后续 Web 控制台处理。MCP 工具不得直接删除 NAS 原始录播、本地 `input/` 副本、audio 或 clips。

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
- Web 控制台里的 `待选片` 等待状态。

## Web 控制台

V3 Web 控制台是 Service Core 的统一控制面，默认只允许本机访问：

```bash
.venv/bin/live-clipper web
```

打开 `http://127.0.0.1:8765`。

页面包含：

- `服务`：查看服务状态、PID、心跳、下次扫描、录播源摘要，并可启动、停止、立即扫描。
- `任务`：按 `processing`、`needs_review`、`rendering`、`rendered`、`failed` 查看任务和详情。
- `确认`：查看 MCP/Web 创建的删除确认请求，支持单条确认/拒绝和批量确认/拒绝。
- `日志`：查看事件流和任务日志尾部。
- `配置`：在 Web 端检查、保存必要配置，并查看 API key 环境变量是否已配置。

### V4 Web 配置页

V4 Web 配置页把原来的只读 `设置` 升级为可编辑 `配置`，适合不熟悉 TOML 的用户完成第一次可运行配置。

在 `配置` 页面可以做这些事：

- `基础路径`：编辑输入目录、输出目录、工作目录和术语表路径。
- `录播源`：编辑录播源目录、回看时间窗口、最小文件年龄和稳定性检查秒数。
- `AI 与 ASR`：编辑 LLM API 地址、模型名、API key 环境变量名、ASR 后端、ASR 模型和语言。
- `服务行为`：编辑扫描间隔、选片后自动渲染，以及查看固定的安全清理模式 `preview_only`。
- `检查配置`：只校验当前表单，不写入文件。
- `保存配置`：校验通过后写回 `live-clipper.toml`，并在保存前备份到 `work/config_backups/`。
- `重启服务`：让本机常驻服务重新读取配置；不会主动终止已经启动的 pipeline 子进程。

安全边界：

- 页面不会显示明文 API key，也不会把密钥写进 `live-clipper.toml`。
- 页面只编辑 API key 的环境变量名，例如 `CHEAP_MODEL_API_KEY`，并显示 `已配置` / `未配置`。
- 如果环境变量未配置，请在 `.env` 中添加，例如 `CHEAP_MODEL_API_KEY=...`。
- 修改 Web host/port 后，需要手动重启 Web 控制台命令本身才会生效。
- 如果 TOML 解析失败，Web 配置页不会覆盖旧配置；请先修复 `live-clipper.toml`。

### V5 内置定时调度

V5 增加内置 Scheduler，跟随 `live-clipper service` 运行，不再依赖 Codex 定时任务、cron 或 launchd。定时配置统一放在 Web `配置` 页的 `定时任务` 分区。

默认定时任务：

- 每周日 00:00：`每周录播扫描`，执行 `scan_recordings`，扫描录播源并为稳定录播创建 run。
- 每周日 12:00：`每周审阅检查`，执行 `review_due_check`，只标记和提醒待审阅任务。

你可以在 `配置` 页完成这些操作：

- 查看 Scheduler 状态、调度时区、当前系统时间、下一次执行任务和最近结果。
- 创建或编辑任务，支持每周、每天、每隔 N 分钟。
- 对任务执行 `立即执行`、`暂停`、`启用`。
- 修改 `timezone`、`tick_seconds`、`missed_policy` 后保存配置，并重启 service 让新设置生效。

安全边界：

- Scheduler 不会删除文件，不会执行 cleanup confirm，不会 approve/reject confirmation。
- Scheduler 不会主动终止已经启动的 pipeline 子进程。
- V5 的 `review_due_check` 不会自动生成 selected_clips.json，也不会调用 Codex CLI、Claude Code 或模型自动选片。
- AI 自动审阅将在后续版本配置；当前 V5 只做定时提醒和状态标记。

### V6 AI 自动审阅

V6 增加 AI 自动审阅执行器，用来把 `needs_review` 任务的审阅包转换成经过系统校验的 `selected_clips.json`。默认不会静默启用，必须在 Web `配置` 页的 `AI 审阅` 分区明确打开。

支持三种方式：

- `本地 Agent / Codex CLI`：检测本机 `codex` 命令，使用非交互方式让 Codex 返回选片 JSON。
- `本地 Agent / Claude Code`：检测本机 `claude` 命令，使用非交互方式让 Claude Code 返回选片 JSON。
- `配置模型直连`：复用 `[llm]` 的 OpenAI-compatible API 地址、模型名和 API key 环境变量。

执行流程：

1. Scheduler 的任务类型可以选择 `AI 自动审阅`，也可以在 `任务` 页对单个 `needs_review` run 点击 `立即 AI 审阅`。
2. AI 只返回 JSON 数组，不直接写文件。
3. `live-clipper` 先写入 `selected_clips.tmp.json`。
4. 系统调用 `validate_selected_clips_file()` 校验 clip id、时间范围和 remove ranges。
5. 校验通过后才替换成正式 `selected_clips.json`。
6. `service` 继续复用已有自动渲染链路。

安全边界：

- AI 不会直接删除文件。
- AI 不会移动 NAS 原始录播。
- AI 不会执行 cleanup confirm。
- AI 不会 approve/reject confirmation。
- 选片校验失败时会删除临时文件，不会写入 `selected_clips.json`，也不会进入渲染。
- 页面只展示 API key 环境变量是否已配置，不展示明文 secret。

老用户提示：已有的周日 12:00 `review_due_check` 不会被自动强改成 `AI 自动审阅`。如果你确认要自动选片，请在 `配置` 页编辑该定时任务，把动作类型改为 `AI 自动审阅`，并先用 `测试 AI 审阅环境` 检查本机 Agent 或模型配置。

### V7 配置页分层

V7 把 Web `配置` 页重组为更适合新用户的四层结构，常用配置默认展示，高级设置默认收起：

- `配置体检`：只读查看录播源、本地项目库、LLM、ASR、服务、定时任务和 AI 审阅状态。
- `快速开始`：只保留首次跑通必须关心的录播源、本地输入/输出、LLM、ASR、服务开关和自动渲染。
- `自动化`：集中管理 Scheduler、默认定时任务、AI 审阅开关、审阅方式、环境测试和立即处理待审阅。
- `高级设置默认收起`：路径与文件、模型请求、服务与调度、AI 审阅参数、Web 控制台等低频参数仍可展开编辑。

保存、检查、重载、恢复默认和重启服务仍使用同一套配置 API；收起的高级字段也会随当前表单一起保留，不会因为只看快速开始而丢失。页面仍不会展示明文 API key，删除和清理动作继续走 confirmation，AI 审阅也不会直接写文件。

如果确实需要局域网访问：

```bash
.venv/bin/live-clipper web --host 0.0.0.0
```

不要把 Web 控制台暴露到公网。删除相关动作默认先进入 `work/service/confirmations.json`，approve 时会重新校验路径、run 状态和安全边界；NAS 原始录播不会被 Web 直接删除。

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
