# V5 内置定时调度设计

## 背景

当前 MCP 工作台版本已经具备：

- V1 本机常驻服务：扫描录播源、复制录播、启动 pipeline、推进 run 状态。
- V2 MCP 工具面：让 AI 能读取服务状态、审阅包、写入选片、触发渲染、创建删除确认。
- V3 Web 控制台：展示服务、任务、确认队列、日志和设置。
- V4 Web 配置页：把必要配置从手写 `live-clipper.toml` 推进到 Web 可视化配置。

但“什么时候扫描录播、什么时候检查待审阅任务”仍依赖 Codex 定时任务、系统 cron 或人工触发。用户希望 `live-clipper` 自己具备定时能力，并且定时相关配置要和其他配置放在同一个 Web `配置` 页里。

V5 目标是新增一个跟随 `live-clipper service` 运行的轻量 Scheduler，让用户可以在 Web `配置` 页配置本机定时任务，不依赖 Codex 定时任务、cron 或 launchd。

## 产品目标

1. 用户可以在 Web `配置` 页配置内置定时任务。
2. 默认支持每周日 00:00 扫描录播源并启动切片 pipeline。
3. 默认支持每周日 12:00 检查待审阅任务，并写入可观察提醒状态。
4. 用户可以查看每个定时任务的启用状态、下次执行时间、上次执行结果和最近日志。
5. 用户可以手动立即执行、暂停、启用定时任务。
6. Scheduler 跟随 `live-clipper service` 运行，不安装系统级计划任务。

## 已定产品判断

1. V5 的定时配置不做独立主页面，统一放在 Web `配置` 页。
2. Web 顶部导航保持：`服务`、`任务`、`确认`、`日志`、`配置`。
3. `配置` 页内新增 `定时任务` 分区，与 `基础路径`、`录播源`、`AI 与 ASR`、`服务行为`、`高级配置` 并列。
4. V5 不自动执行 AI 选片，只做待审阅检查和提醒。
5. 自动 AI 审阅执行器作为 V6 独立能力实现。
6. V5 Scheduler 不执行删除、cleanup confirm 或任何 destructive action。

## 非目标

V5 不做以下内容：

- 不新增独立 `定时` 主页面。
- 不安装 cron、launchd 或开机自启动项。
- 不做云端调度。
- 不做多用户权限。
- 不做复杂 cron 表达式编辑器作为默认入口。
- 不调用 Codex、Claude Code 或外部模型自动选片。
- 不直接生成 `selected_clips.json`。
- 不绕过 confirmation 机制。

## 信息架构

Web 顶部导航保持五个页面：

- `服务`
- `任务`
- `确认`
- `日志`
- `配置`

`配置` 页分区调整为：

1. `基础路径`
2. `录播源`
3. `AI 与 ASR`
4. `服务行为`
5. `定时任务`
6. `高级配置`

如果 V6 自动 AI 审阅已经存在或后续接入，`配置` 页再增加 `AI 审阅` 分区。V5 只需要在 `定时任务` 分区中保留“审阅检查”动作，不展示 V6 的自动审阅配置。

## 定时任务分区设计

### 顶部摘要

`定时任务` 分区顶部显示：

- Scheduler 状态：运行中 / 已暂停 / 未启用 / 异常。
- 当前调度时区。
- 当前系统时间。
- 下一次即将执行的任务。
- 最近一次执行结果。
- 提示文案：`服务未运行时不会执行定时任务。`

### Job 列表

每个任务显示：

- 名称。
- 动作类型。
- 计划时间。
- 启用状态。
- 下次执行时间。
- 上次执行时间。
- 上次结果。
- 操作：`立即执行`、`暂停` / `启用`、`编辑`。

### Job 编辑表单

P0 只支持小白表单，不暴露 cron 表达式。

字段：

- 任务名称。
- 启用开关。
- 动作类型。
- 频率：每周 / 每天 / 每隔 N 分钟。
- 星期：周一到周日，仅每周显示。
- 时间：`HH:MM`，每周/每天显示。
- 间隔分钟数，仅每隔 N 分钟显示。
- 跳过策略：如果上次还在运行，跳过本次。

动作类型：

- `扫描录播`：扫描录播源，发现稳定录播后启动 pipeline。
- `审阅检查`：检查 `needs_review` run，写入提醒事件。
- `维护检查`：推进 run 状态、写心跳、汇总异常。

## 配置格式

V5 扩展 `live-clipper.toml`：

```toml
[scheduler]
enabled = true
timezone = "Asia/Shanghai"
tick_seconds = 30
missed_policy = "run_once"
state_dir = "work/service"

[[scheduler.jobs]]
id = "weekly_recording_scan"
name = "每周录播扫描"
enabled = true
type = "scan_recordings"
schedule = "weekly"
day_of_week = "sun"
time = "00:00"
skip_if_running = true

[[scheduler.jobs]]
id = "weekly_review_due"
name = "每周审阅检查"
enabled = true
type = "review_due_check"
schedule = "weekly"
day_of_week = "sun"
time = "12:00"
skip_if_running = true
```

### scheduler 字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否启用内置 Scheduler |
| `timezone` | string | `Asia/Shanghai` | IANA 时区 |
| `tick_seconds` | integer | `30` | service loop 检查 schedule 的最小间隔 |
| `missed_policy` | enum | `run_once` | 错过执行时间后的处理策略 |
| `state_dir` | path | `work/service` | scheduler 状态存储目录 |

### scheduler.jobs 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 稳定 job id，只允许小写字母、数字、下划线、短横线 |
| `name` | string | 中文显示名 |
| `enabled` | boolean | 是否启用 |
| `type` | enum | `scan_recordings`、`review_due_check`、`maintenance_check` |
| `schedule` | enum | `weekly`、`daily`、`interval_minutes` |
| `day_of_week` | enum | `mon` 到 `sun`，weekly 必填 |
| `time` | string | `HH:MM`，weekly/daily 必填 |
| `interval_minutes` | integer | interval_minutes 必填 |
| `skip_if_running` | boolean | 如果同一 job 上次执行仍在运行，是否跳过 |

## 时间校准

Scheduler 基于配置时区和本机系统时间计算到期时间。

默认：

```toml
timezone = "Asia/Shanghai"
```

规则：

- 周日 00:00 和周日 12:00 都按 `[scheduler].timezone` 计算。
- 不按 UTC 直接触发。
- 不按浏览器时区触发。
- V5 不做网络授时，默认信任当前机器系统时间。
- Web `配置` 页必须显示当前系统时间和调度时区。
- 如果系统时间无法读取或时区非法，Scheduler 不执行 job，并在页面显示中文错误。

## 错过执行策略

`missed_policy` 支持：

- `run_once`：service 恢复后发现错过窗口，只补跑最近一次。
- `skip`：错过就跳过。

默认 `run_once`。

约束：

- 不补跑所有错过次数，避免恢复后连续启动大量任务。
- 如果错过超过 7 天，不补跑。
- 补跑时写入 `scheduler_job_missed_run_once` 事件。

## Job 行为

### scan_recordings

执行：

1. 调用现有录播扫描逻辑。
2. 对稳定录播创建 run。
3. 启动 pipeline。
4. 记录 started_runs、known_runs、skipped duplicates。

幂等要求：

- 同一个 source fingerprint 不重复创建 run。
- 如果同一 job 仍在运行且 `skip_if_running = true`，跳过本次。

### review_due_check

执行：

1. 读取 Service Core runs。
2. 找出 `phase = "needs_review"` 且还没有被本轮标记过的 run。
3. 对每个 run 写入 `review_due_at` 或 scheduler due metadata。
4. 写入 `review_due` 事件。
5. 如果 run 目录缺少 `codex_task.md` 或 `ai_review_task.md`，可以生成或刷新任务文件。

返回示例：

```json
{
  "ok": true,
  "due_runs": ["run_a", "run_b"],
  "message": "发现 2 个待审阅任务"
}
```

如果没有待审阅：

```json
{
  "ok": true,
  "due_runs": [],
  "message": "当前没有待审阅任务"
}
```

### maintenance_check

执行：

1. reconcile runs。
2. 写 scheduler heartbeat。
3. 汇总 failed / needs_review / rendered counts。

P0 可以内置为系统 job，不一定暴露给用户编辑。

## 状态文件

Scheduler 状态写入 `work/service/`：

```text
work/service/
  scheduler.json
  scheduler_runs.json
  scheduler_events.jsonl
```

### scheduler.json

```json
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "last_tick_at": "2026-06-30T09:00:00+08:00",
  "next_due_job_id": "weekly_review_due",
  "next_due_at": "2026-07-05T12:00:00+08:00",
  "last_error": null
}
```

### scheduler_runs.json

```json
{
  "jobs": {
    "weekly_recording_scan": {
      "status": "success",
      "last_run_at": "2026-07-05T00:00:00+08:00",
      "next_run_at": "2026-07-12T00:00:00+08:00",
      "last_result": {
        "started_runs": 1,
        "known_runs": 3
      }
    }
  }
}
```

### scheduler_events.jsonl

P0 至少写：

- `scheduler_started`
- `scheduler_tick`
- `scheduler_job_due`
- `scheduler_job_started`
- `scheduler_job_completed`
- `scheduler_job_failed`
- `scheduler_job_skipped`
- `scheduler_job_missed_run_once`

关键事件也要写入 Service Core `events.jsonl`，方便现有 Web `日志` 页面统一观察。

## Service Core 集成

当前 service loop 不应继续只依赖长 sleep。V5 建议改为短 tick loop：

```text
while running:
  reload or use current settings
  reconcile existing runs
  scheduler.tick(now)
  update service heartbeat and scheduler summary
  sleep(tick_seconds)
```

关键原则：

- 只有 `scan_recordings` 到期时才执行录播扫描。
- 非扫描 tick 不应每次执行录播源稳定性检查。
- run 状态 reconcile 可以在短 tick 中执行。
- `service start --once` 继续保留，用于手动单轮扫描和测试。

## Web API

V5 新增 API：

### GET /api/scheduler

返回 scheduler 状态、job 列表、每个 job 的下次执行和最近结果。

### POST /api/scheduler/jobs

创建或替换一个 job。

要求：

- 校验字段。
- 写回 `live-clipper.toml` 或复用 V4 config editor 的写回能力。
- 不写 secret。
- 返回更新后的 scheduler 状态。

### POST /api/scheduler/jobs/<job_id>/run-now

立即执行指定 job。

### POST /api/scheduler/jobs/<job_id>/pause

暂停 job。

### POST /api/scheduler/jobs/<job_id>/resume

启用 job。

### GET /api/scheduler/events

返回最近 scheduler events。

## 与 V4 配置页的关系

V5 必须基于 V4 的 `配置` 页面继续扩展。

要求：

- 使用 V4 config editor 的白名单字段模式。
- 保存前备份 `live-clipper.toml`。
- 保存后复验 `load_settings(config_path)`。
- 修改 scheduler 配置后提示需要重启 service 或点击 `重启服务`。
- 修改 Web host/port 仍只提示手动重启 Web 控制台。

## 与 V6 AI 审阅的关系

V5 的 `review_due_check` 只负责标记待审阅，不自动选片。

V6 会新增 `review_automation` 能力。届时：

- V6 可以新增 job type `ai_review`。
- V6 可以把默认周日 12:00 job 从 `review_due_check` 升级为 `ai_review`。
- V6 的 UI 配置仍放在同一个 Web `配置` 页。

V5 开发时只需要保留扩展点，不要提前实现 V6 自动审阅逻辑。

## 校验规则

### scheduler

- `enabled`: boolean。
- `timezone`: 必须是有效 IANA timezone。
- `tick_seconds`: 5 到 300。
- `missed_policy`: `run_once` 或 `skip`。

### jobs

- `id`: `^[a-z0-9_-]{1,64}$`。
- `name`: 1 到 40 个字符。
- `type`: `scan_recordings`、`review_due_check`、`maintenance_check`。
- `schedule`: `weekly`、`daily`、`interval_minutes`。
- `day_of_week`: weekly 必填，值为 `mon/tue/wed/thu/fri/sat/sun`。
- `time`: weekly/daily 必填，格式 `HH:MM`。
- `interval_minutes`: interval 必填，范围 5 到 1440。
- `skip_if_running`: boolean。

## 安全边界

- Scheduler 不删除文件。
- Scheduler 不执行 `cleanup --confirm`。
- Scheduler 不直接 approve/reject confirmation。
- Scheduler 不杀 pipeline 子进程。
- Scheduler 不访问公网，除非被触发的 pipeline 根据现有 ASR/LLM 配置请求服务。
- Scheduler 不存储或展示 secret。
- Web 默认仍绑定 `127.0.0.1`。

## UI 文案要求

页面文案能用中文就用中文，特殊名词保留英文。

建议文案：

- `定时任务`
- `内置定时调度`
- `每周录播扫描`
- `每周审阅检查`
- `立即执行`
- `暂停`
- `启用`
- `下次执行`
- `上次结果`
- `服务未运行时不会执行定时任务。`
- `AI 自动审阅将在后续版本配置；当前任务只负责提醒和标记待审阅。`

## 测试验收

需要新增：

- `tests/test_scheduler.py`
  - weekly next run 计算。
  - daily next run 计算。
  - interval next run 计算。
  - missed_policy run_once。
  - skip_if_running。
  - job validation。
  - scan_recordings 调用现有 service action。
  - review_due_check 标记 needs_review runs。
- `tests/test_web_scheduler.py`
  - `GET /api/scheduler`。
  - `POST /api/scheduler/jobs`。
  - `POST /api/scheduler/jobs/<id>/run-now`。
  - pause/resume。
  - invalid job 返回中文错误。
- `tests/test_config.py`
  - scheduler config 默认值。
  - scheduler jobs 从 TOML 读取。
- `tests/test_docs.py`
  - README 或用户指南包含内置定时说明。

全量测试：

```bash
.venv/bin/python -m pytest -q
```

触达文件 lint：

```bash
uv run --with ruff ruff check src/live_clipper/scheduler.py src/live_clipper/service.py src/live_clipper/config.py src/live_clipper/config_editor.py src/live_clipper/web.py tests/test_scheduler.py tests/test_web_scheduler.py tests/test_config.py tests/test_docs.py
```

## 手工验收

1. 启动 Web 控制台。
2. 打开 `配置` 页面。
3. 找到 `定时任务` 分区。
4. 创建 `每周录播扫描`，设置为周日 00:00。
5. 点击 `立即执行`，如果录播源有稳定录播，应创建 run。
6. 创建 `每周审阅检查`，设置为周日 12:00。
7. 准备一个 `needs_review` run。
8. 点击 `立即执行`，Web 显示发现待审阅任务。
9. 检查 `work/service/scheduler.json`、`scheduler_runs.json`、`scheduler_events.jsonl`。
10. 检查 Service Core `events.jsonl` 有对应 scheduler event。
11. 暂停一个 job，确认到期不会执行。

## 开发拆分建议

### V5.1 Scheduler Core

- 新增 `src/live_clipper/scheduler.py`。
- 增加 scheduler config dataclass。
- 实现 next run 计算、job validation、state load/save。
- 实现 `scan_recordings`、`review_due_check`、`maintenance_check` action。
- 接入 service loop 短 tick。

### V5.2 Scheduler Web API

- 新增 `/api/scheduler` 系列 API。
- 支持 run-now、pause、resume。
- 返回 scheduler 状态和事件。

### V5.3 配置页定时任务分区

- 在 `配置` 页新增 `定时任务` 分区。
- 展示 job 列表、状态、最近结果。
- 支持创建/编辑基础 job。
- 支持立即执行、暂停、启用。

### V5.4 文档与小白指南

- README 增加内置定时说明。
- 小白指南增加“无需 Codex 定时任务”的配置路径。
- 给出周日 00:00 / 周日 12:00 示例。

## 验收标准

功能通过：

- 用户能在 Web `配置` 页配置内置定时任务。
- `live-clipper service` 运行时会按计划触发 job。
- 不依赖 Codex 定时任务、cron 或 launchd。
- 周日 00:00 扫描录播的默认模板可用。
- 周日 12:00 审阅检查的默认模板可用。
- Job 执行状态可在 Web 和状态文件中观察。
- Job 可暂停、启用、立即执行。
- Scheduler 不删除文件，不绕过 confirmation。

工程通过：

- Scheduler Core 有独立测试。
- Web API 有测试。
- 配置读取有测试。
- 全量测试通过。
- 触达文件 lint 通过。
