# V6 AI 自动审阅设计

## 背景

V1 到 V5 的系统边界是：

- service 负责扫描录播、启动 pipeline、推进 run 状态。
- pipeline 生成 `codex_brief.json`、`codex_review.md` 和 `selected_clips.template.json`。
- AI 或人工读取审阅包，写入 `selected_clips.json`。
- service 发现 `selected_clips.json` 后自动渲染。
- V5 Scheduler 可以在周日 12:00 检查待审阅 run，但不负责自动选片。

用户认为当前第二个时间节点仍过度依赖手工，希望系统在到点后可以自动调用 AI 完成审阅。用户明确希望分成两类配置：

1. 支持调用本地 Claude Code 或 Codex CLI 自动执行。
2. 支持调用配置接入的模型。

V6 目标是新增一个 AI 自动审阅执行器，让 `needs_review` run 可以自动生成经过校验的 `selected_clips.json`。

## 产品目标

1. 用户可以在 Web `配置` 页配置 AI 自动审阅。
2. 用户可以选择本地 Agent 模式：Codex CLI 或 Claude Code。
3. 用户可以选择直连模型模式：复用 `[llm]` 的 OpenAI-compatible 模型配置。
4. Scheduler 的周日 12:00 任务可以触发自动 AI 审阅。
5. Web `任务` 页可以手动触发某个 run 的 AI 审阅。
6. AI 审阅只负责生成选片结果，最终写入必须经过系统校验。
7. 写入 `selected_clips.json` 后继续复用现有自动渲染链路。

## 已定产品判断

1. V6 不把 AI 审阅配置做成独立主页面，统一放在 Web `配置` 页。
2. `配置` 页新增 `AI 审阅` 分区，和 `定时任务` 分区放在一起。
3. 定时任务的“12 点审阅动作”引用 `AI 审阅` 配置，不重复配置模型或 Agent。
4. 默认不静默开启自动 AI 审阅，必须用户明确启用。
5. 本地 Agent 模式优先推荐 `codex_cli`，但同时支持 `claude_code`。
6. 无论使用本地 Agent 还是直连模型，默认都让 AI 返回选片 JSON，由 `live-clipper` 校验并写入文件。
7. V6 不允许 AI 直接删除、清理、移动 NAS 文件或 approve confirmation。

## 非目标

V6 不做以下内容：

- 不做完整视觉剪辑器。
- 不自动发布视频到平台。
- 不做多轮人机协同剪辑工作台。
- 不做公网队列或云端审阅服务。
- 不让 AI 绕过 `validate_selected_clips_file()`。
- 不让 AI 执行 destructive action。
- 不让 AI 自动确认 cleanup 或删除请求。
- 不强依赖某一个 AI 厂商。

## 用户故事

### 用户故事 1：使用本地 Codex CLI 自动审阅

作为用户，我希望在配置页选择 `Codex CLI`，让系统到周日 12:00 自动调用本机 Codex 完成选片。

验收：

- Web `配置` 页可以选择 `本地 Agent` / `Codex CLI`。
- 页面可以检测 `codex` 命令是否存在。
- 点击 `测试 AI 审阅环境` 能看到可用或失败原因。
- 到点后自动处理 `needs_review` run。
- 处理成功后写入 `selected_clips.json`。
- 选片结果通过现有校验。

### 用户故事 2：使用本地 Claude Code 自动审阅

作为用户，我希望在配置页选择 `Claude Code`，让系统调用本机 Claude Code 完成选片。

验收：

- Web `配置` 页可以选择 `本地 Agent` / `Claude Code`。
- 页面可以检测 `claude` 命令是否存在。
- 执行过程有超时和错误日志。
- 输出无法解析或校验失败时，不进入渲染。

### 用户故事 3：使用配置模型自动审阅

作为用户，我希望不依赖本地 Agent，而是复用配置里的模型服务直接完成选片。

验收：

- Web `配置` 页可以选择 `配置模型直连`。
- 模型配置复用 `[llm]` 的 API 地址、模型名和 API key 环境变量。
- 页面不展示明文 API key。
- 模型返回严格 JSON。
- 系统校验通过后写入 `selected_clips.json`。

## 信息架构

Web 顶部导航保持：

- `服务`
- `任务`
- `确认`
- `日志`
- `配置`

`配置` 页分区包含：

1. `基础路径`
2. `录播源`
3. `AI 与 ASR`
4. `服务行为`
5. `定时任务`
6. `AI 审阅`
7. `高级配置`

`定时任务` 分区负责“什么时候触发”。

`AI 审阅` 分区负责“用谁审阅、怎么审阅、失败怎么办”。

## 配置格式

V6 扩展 `live-clipper.toml`：

```toml
[review_automation]
enabled = false
mode = "local_agent"
max_runs_per_tick = 1
auto_render_after_selection = true
on_failure = "keep_needs_review"
timeout_minutes = 60
prompt_template = "default_clip_review"

[review_automation.local_agent]
provider = "codex_cli"
command_timeout_minutes = 60
include_review_package_inline = true
allow_agent_file_writes = false

[review_automation.model]
provider = "openai_compatible"
use_llm_config = true
model = ""
max_candidates = 40
temperature = 0.2
max_tokens = 4096
retry_attempts = 2
```

### review_automation 字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | 是否启用自动 AI 审阅 |
| `mode` | enum | `local_agent` | `local_agent` 或 `model` |
| `max_runs_per_tick` | integer | `1` | 单次触发最多处理几个 run |
| `auto_render_after_selection` | boolean | `true` | 写入选片后是否沿用自动渲染 |
| `on_failure` | enum | `keep_needs_review` | 失败后保留待审阅或标记失败 |
| `timeout_minutes` | integer | `60` | 单个 run 最大执行时间 |
| `prompt_template` | string | `default_clip_review` | 审阅提示词模板 |

### local_agent 字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | enum | `codex_cli` | `codex_cli` 或 `claude_code` |
| `command_timeout_minutes` | integer | `60` | 本地命令超时 |
| `include_review_package_inline` | boolean | `true` | 是否把审阅包作为 prompt 输入 |
| `allow_agent_file_writes` | boolean | `false` | 是否允许 Agent 直接写文件，P0 必须固定为 false |

### model 字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | enum | `openai_compatible` | P0 仅支持 OpenAI-compatible |
| `use_llm_config` | boolean | `true` | 是否复用 `[llm]` 配置 |
| `model` | string | 空 | 为空时复用 `[llm].model` |
| `max_candidates` | integer | `40` | 每次最多给模型的候选数量 |
| `temperature` | float | `0.2` | 模型温度 |
| `max_tokens` | integer | `4096` | 最大输出 token |
| `retry_attempts` | integer | `2` | JSON 解析或请求失败时重试次数 |

## Web 配置页设计

### AI 审阅分区

字段：

- `启用自动 AI 审阅`：开关。
- `审阅方式`：`本地 Agent` / `配置模型直连`。
- `本地 Agent`：`Codex CLI` / `Claude Code`。
- `每次最多处理任务数`。
- `单任务超时时间`。
- `失败后处理`：保留待审阅 / 标记失败。
- `选片后自动渲染`。
- `测试 AI 审阅环境`。

显示状态：

- Codex CLI 是否可用。
- Claude Code 是否可用。
- LLM API key 环境变量是否已配置。
- 最近一次 AI 审阅结果。

安全提示：

```text
AI 审阅只会生成选片结果。删除、清理和确认仍需要你在确认页处理。
```

### 定时任务分区联动

V6 后，`定时任务` 分区中的周日 12:00 任务可以选择动作：

- `审阅检查`：只标记待审阅。
- `AI 自动审阅`：按 `AI 审阅` 分区配置执行自动选片。

默认升级策略：

- 新用户默认创建 `AI 自动审阅` 任务，但 `review_automation.enabled = false`，页面提示需要手动启用。
- 老用户已有 `review_due_check` job 时，不自动改成 `AI 自动审阅`，只提示可升级。

## 执行模型

V6 新增 `review_automation` 模块，负责把 run 审阅包转换成选片结果。

数据流：

```text
Scheduler 或 Web 手动触发
  -> find needs_review run
  -> build review payload
  -> local_agent adapter 或 model adapter
  -> parse selected clips JSON
  -> write selected_clips.tmp.json
  -> validate_selected_clips_file()
  -> replace selected_clips.json
  -> append events
  -> existing service auto render
```

关键原则：

- AI 只负责判断和生成候选 JSON。
- `live-clipper` 负责写文件、校验和状态推进。
- `selected_clips.json` 只能在校验通过后出现。
- 校验失败时不得触发渲染。

## 审阅输入包

系统为每个 run 构建结构化输入：

```json
{
  "run_id": "run_...",
  "run_dir": "output/...",
  "source_name": "recording.mp4",
  "brief": {
    "path": "codex_brief.json",
    "content": {}
  },
  "review_markdown": {
    "path": "codex_review.md",
    "text": "..."
  },
  "selection_template": {
    "path": "selected_clips.template.json",
    "content": []
  },
  "refined_candidates": {
    "path": "refined_candidates.json",
    "content": []
  },
  "output_contract": {
    "type": "array",
    "path": "selected_clips.json",
    "must_reference_existing_clip_ids": true
  }
}
```

如果 `codex_brief.json` 太大：

- 本地 Agent 模式可以仍传完整内容，但要记录 payload 大小。
- 模型直连模式受 `max_candidates` 限制，超出时优先取评分靠前候选，并在事件中记录 truncated。

## 输出契约

AI 必须返回 JSON 数组：

```json
[
  {
    "clip_id": "w0001-c001",
    "source_start": 12.5,
    "source_end": 58.0,
    "title": "一句适合发布的标题",
    "remove_ranges": []
  }
]
```

允许 AI 在最终文本中包含解释，但系统必须能提取 JSON 数组。

写入前必须调用：

```python
validate_selected_clips_file(temp_path, candidates_path)
```

校验失败：

- 删除临时文件。
- 不写 `selected_clips.json`。
- run 保持 `needs_review`，除非用户配置 `on_failure = "mark_failed"`。
- 写入 `ai_review_failed` 事件。
- Web 显示中文错误。

## 本地 Agent 模式

### Codex CLI

适配器行为：

1. 检测 `codex` 命令是否存在。
2. 构建审阅 prompt 和 review payload。
3. 使用非交互方式调用 `codex exec`。
4. 捕获最终输出。
5. 解析 JSON。
6. 交给系统校验和写入。

产品要求：

- P0 不允许 Codex CLI 直接改文件。
- P0 不要求 Codex CLI 使用 MCP。
- P0 不让 Codex CLI 执行删除或 cleanup。
- 命令超时必须可配置。
- stdout/stderr 摘要写入日志，不能写入 secret。

建议命令形态由开发按当前 Codex CLI 版本确定，但必须满足：

- 非交互。
- 可设置工作目录。
- 可设置超时。
- 输出可捕获。
- 不需要人工确认才能完成。

### Claude Code

适配器行为：

1. 检测 `claude` 命令是否存在。
2. 使用 `claude -p` 或等价非交互方式。
3. 输入同一份 review payload。
4. 捕获输出、解析 JSON、交给系统校验。

产品要求：

- P0 不允许 Claude Code 直接改文件。
- P0 不依赖 Claude Code session 续接。
- P0 不要求 Claude Code 打开 IDE 或浏览器。

## 直连模型模式

直连模型模式复用现有 OpenAI-compatible `[llm]` 配置和 `CheapModelClient` 模式。

执行：

1. 读取 `[llm].api_base`、`[llm].api_key_env`、`[llm].model`。
2. 构建系统 prompt。
3. 发送 review payload。
4. 要求模型返回 JSON 数组。
5. 解析并校验。
6. 写入 `selected_clips.json`。

错误处理：

- API key 未配置：返回 `ai_review_model_not_configured`。
- 请求失败：按 retry_attempts 重试。
- 非 JSON：重试一次修复提示。
- 校验失败：不渲染，记录失败。

## 状态与事件

新增状态文件：

```text
work/service/review_automation.json
work/service/review_automation_events.jsonl
```

`review_automation.json` 保存摘要：

```json
{
  "enabled": true,
  "mode": "local_agent",
  "provider": "codex_cli",
  "last_run_at": "2026-07-05T12:00:00+08:00",
  "last_status": "success",
  "last_run_id": "run_...",
  "last_error": null
}
```

事件：

- `ai_review_started`
- `ai_review_completed`
- `ai_review_failed`
- `ai_review_skipped`
- `ai_review_selection_written`
- `ai_review_validation_failed`
- `ai_review_environment_check`

关键事件也要写入 Service Core `events.jsonl`。

## Web API

新增 API：

### GET /api/review-automation

返回配置摘要、环境检测状态、最近执行结果。

### POST /api/review-automation/check

检测当前配置是否可用。

返回：

- Codex CLI 是否存在。
- Claude Code 是否存在。
- LLM API key 环境变量是否已配置。
- 当前模式是否可以执行。

### POST /api/runs/<run_id>/ai-review

对单个 run 立即执行 AI 审阅。

要求：

- run 必须存在。
- run 必须是 `needs_review`。
- `selected_clips.json` 不存在时才执行。
- 返回执行结果和 selection path。

### POST /api/review-automation/run-due

处理当前 due 的 `needs_review` runs，受 `max_runs_per_tick` 限制。

Scheduler 的 `AI 自动审阅` job 调用同一套动作。

## MCP 工具扩展

V6 可选但推荐新增：

- `get_review_automation_status`
- `run_ai_review_for_run`
- `run_due_ai_reviews`

这些工具仍然必须复用 Service Core 和 review automation 模块，不直接写文件。

## 安全边界

- AI 不直接删除文件。
- AI 不直接执行 cleanup confirm。
- AI 不 approve/reject confirmation。
- AI 不移动 NAS 原始录播。
- AI 默认不直接写 `selected_clips.json`，只返回 JSON。
- `live-clipper` 校验通过后才写入正式文件。
- 所有失败都可观察。
- 日志不得写入 API key、完整 Authorization header 或其他 secret。

## UI 文案要求

页面文案能用中文就用中文，特殊名词保留英文。

建议文案：

- `AI 审阅`
- `启用自动 AI 审阅`
- `审阅方式`
- `本地 Agent`
- `Codex CLI`
- `Claude Code`
- `配置模型直连`
- `测试 AI 审阅环境`
- `立即 AI 审阅`
- `AI 已生成选片，等待系统校验`
- `选片校验失败，未进入渲染`
- `AI 审阅只会生成选片结果。删除和清理仍需要你确认。`

## 测试验收

需要新增：

- `tests/test_review_automation.py`
  - 配置默认值。
  - local_agent provider 校验。
  - model provider 校验。
  - 从 review package 构建 payload。
  - 解析 JSON 数组。
  - 校验失败不写 `selected_clips.json`。
  - 校验成功写入 `selected_clips.json`。
  - max_runs_per_tick 生效。
- `tests/test_web_review_automation.py`
  - `GET /api/review-automation`。
  - `POST /api/review-automation/check`。
  - `POST /api/runs/<run_id>/ai-review`。
  - invalid phase 返回中文错误。
  - 已存在 `selected_clips.json` 时跳过或拒绝。
- `tests/test_config.py`
  - review_automation 配置读取。
- `tests/test_config_editor.py`
  - 配置页白名单字段读写。
- `tests/test_docs.py`
  - README 或用户指南包含 AI 自动审阅说明。

全量测试：

```bash
.venv/bin/python -m pytest -q
```

触达文件 lint：

```bash
uv run --with ruff ruff check src/live_clipper/review_automation.py src/live_clipper/config.py src/live_clipper/config_editor.py src/live_clipper/web.py src/live_clipper/web_static tests/test_review_automation.py tests/test_web_review_automation.py tests/test_config.py tests/test_config_editor.py tests/test_docs.py
```

## 手工验收

1. 启动 Web 控制台。
2. 打开 `配置` 页面。
3. 找到 `AI 审阅` 分区。
4. 选择 `本地 Agent` / `Codex CLI`。
5. 点击 `测试 AI 审阅环境`，确认 Codex CLI 可用。
6. 准备一个 `needs_review` run。
7. 在 `任务` 页点击 `立即 AI 审阅`。
8. 确认生成 `selected_clips.json`。
9. 确认 Service Core 自动进入渲染或允许用户手动渲染。
10. 切换为 `Claude Code`，用 mock 或真实命令验证环境检测。
11. 切换为 `配置模型直连`，确认缺少 API key 时显示中文错误。
12. 模拟 AI 返回非法 JSON，确认不会写入正式选片文件。

## 开发拆分建议

### V6.1 Review Automation Core

- 新增 `src/live_clipper/review_automation.py`。
- 实现配置 dataclass。
- 实现 review payload 构建。
- 实现 JSON 提取、临时写入、校验、正式替换。
- 实现状态和事件文件。

### V6.2 Local Agent Adapter

- 实现 Codex CLI adapter。
- 实现 Claude Code adapter。
- 环境检测。
- 超时、stdout/stderr 摘要、错误记录。
- 测试中使用 fake runner，不依赖真实 CLI。

### V6.3 Model Adapter

- 复用 `[llm]` 配置。
- 复用 OpenAI-compatible client。
- 实现 JSON 修复重试。
- 实现 max_candidates 截断。

### V6.4 Web 配置与任务入口

- `配置` 页新增 `AI 审阅` 分区。
- `任务` 页对 `needs_review` run 新增 `立即 AI 审阅`。
- Scheduler job type 新增 `AI 自动审阅`。
- README 和小白指南更新。

## 验收标准

功能通过：

- 用户能在 Web `配置` 页配置 AI 自动审阅。
- 支持 Codex CLI。
- 支持 Claude Code。
- 支持配置模型直连。
- Scheduler 可以触发 AI 自动审阅。
- 任务页可以手动触发单个 run 的 AI 审阅。
- 选片结果必须校验通过才写入 `selected_clips.json`。
- 校验失败不会渲染。
- 删除和清理仍需要 confirmation。

工程通过：

- Review Automation Core 有独立测试。
- Local Agent adapter 有 fake runner 测试。
- Model adapter 有 mock client 测试。
- Web API 和配置页有测试。
- 全量测试通过。
- 触达文件 lint 通过。
