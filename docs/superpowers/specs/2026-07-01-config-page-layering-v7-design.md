# V7 配置页分层与高级配置收纳设计

## 背景

V4 到 V6 已经把本机服务、Web 配置、内置定时任务和 AI 自动审阅串起来，产品能力基本闭环。但当前 Web `配置` 页把所有字段按技术模块平铺展示，导致用户第一次打开时会同时看到路径、NAS、LLM、ASR、服务、定时、AI 审阅、模型参数、调度参数和 Web host/port。配置项本身没有错，问题是它们的视觉权重相同，用户很难判断哪些是必须填、哪些是偶尔改、哪些几乎不该碰。

V7 的目标是优化配置页的信息架构：把高频、必要、低风险配置放在默认视图，把低频、高风险或专家参数收进高级配置里。底层 TOML schema、保存 API、安全校验和备份机制保持兼容。

## 目标

1. 让新用户在默认视图中只看到“跑起来必须关心”的配置。
2. 让已经跑通的用户能清楚管理自动化：服务、定时任务、AI 审阅。
3. 把不常用参数收进高级区域，但不移除能力。
4. 让配置页先展示状态和风险，再展示字段，减少用户靠猜。
5. 保持现有 `GET /api/config`、`POST /api/config/validate`、`POST /api/config` 的字段兼容。

## 非目标

- 不新增新的配置字段。
- 不改变 TOML 文件结构。
- 不实现多录播源。
- 不实现密钥明文编辑。
- 不重写 Settings/config_editor 的核心保存逻辑。
- 不改变 V6 本地 Agent 的安全边界。

## 用户分层

### 新用户

只需要完成三件事：

1. 录播从哪里来。
2. 处理结果放哪里。
3. AI/ASR 是否可用。

新用户不应该在第一屏看到 timeout、retry、tick、missed_policy、temperature、max_tokens 等工程参数。

### 日常用户

日常用户主要关心：

1. 服务是否在跑。
2. 周日自动扫描和 AI 审阅是否启用。
3. 下一次定时任务什么时候执行。
4. AI 审阅环境是否可用。

### 高级用户

高级用户可以展开高级区域，调整扫描窗口、稳定性检查、模型参数、调度策略、Web host/port 等。

## 页面结构

`配置` 页改为四个主要区域，按阅读顺序排列。

### 1. 配置体检

默认显示在配置页顶部，作为只读状态摘要。

展示项：

- 录播源：`正常` / `未配置` / `不可访问`
- 本地项目库：`正常` / `将自动创建` / `路径冲突`
- LLM：`已配置` / `未配置`
- ASR：`已配置` / `未配置`
- 服务：`运行中` / `未运行`
- 定时任务：`已启用` / `未启用`，并显示下一次任务时间
- AI 审阅：`可用` / `不可用` / `未启用`

体检区不直接保存配置，只提供跳转或提示。它可以复用现有 config validation、service status、scheduler status、review automation status 数据。

### 2. 快速开始

默认展开。只放第一次配置必须看到的字段。

字段：

- `recording_source_default.source_dir`：录播源目录
- `recording_source_default.input_dir`：本地输入目录
- `recording_source_default.output_root`：本地输出目录
- `llm.api_base`
- `llm.api_key_env`
- `llm.model`
- `asr.backend`
- `asr.model`
- `asr.language`
- `asr.api_base`
- `asr.api_key_env`
- `service.enabled`
- `service.auto_render_after_selection`

说明：

- `paths.input_dir`、`paths.output_root` 与 recording source 的输入/输出目录如果语义重复，默认视图优先展示 `recording_source_default.*`。全局 `paths.*` 放到高级配置。
- `llm.provider_label` 放到高级配置，普通用户只需要知道 API 地址、模型名和 key 环境变量。
- `asr.hf_token_env` 放到高级配置。
- `service.scan_interval_minutes` 放到高级配置；有内置 Scheduler 后，普通用户不需要先理解轮询间隔。
- `service.cleanup_mode` 不作为可编辑主字段，只在安全说明里展示“清理默认预览，不直接删除”。

### 3. 自动化

默认展开。聚焦“什么时候自动做事”和“做到哪一步”。

字段与控件：

- `scheduler.enabled`
- 定时任务摘要卡片：每个 job 显示名称、动作、频率、下一次执行、启用状态
- 默认任务快捷编辑：
  - 每周录播扫描：星期、时间、启用
  - 每周 AI 审阅：星期、时间、启用
- `review_automation.enabled`
- `review_automation.mode`
- `review_automation_local_agent.provider`
- `review_automation_model.model`
- `review_automation.max_runs_per_tick`
- `review_automation.auto_render_after_selection`
- `测试 AI 审阅环境`
- `立即处理待审阅`

高级任务编辑器默认收起，仅在用户点击“编辑高级定时任务”时展示完整 job 表单。

说明：

- 老用户已有 `review_due_check` 不自动强改为 `ai_review`，但页面应给出明确提示和一键引导：“把每周审阅检查升级为 AI 自动审阅”。该动作必须仍经过保存配置。
- 定时任务仍和配置页放在一起，不新增独立主页面。
- 自动化区要明确提示：服务未运行时 Scheduler 不执行。

### 4. 高级设置

默认收起。分组展示，不再把所有高级字段放进一个长列表。

高级分组：

#### 路径与文件

- `paths.input_dir`
- `paths.output_root`
- `paths.work_dir`
- `paths.glossary_path`
- `recording_source_default.since_hours`
- `recording_source_default.min_age_minutes`
- `recording_source_default.stable_check_seconds`

#### 模型请求

- `llm.provider_label`
- `llm.timeout_seconds`
- `llm.request_attempts`
- `llm.retry_delay_seconds`
- `asr.hf_token_env`

#### 服务与调度

- `service.scan_interval_minutes`
- `scheduler.timezone`
- `scheduler.tick_seconds`
- `scheduler.missed_policy`
- 高级 job 字段：`id`、`type`、`schedule`、`interval_minutes`、`skip_if_running`

#### AI 审阅参数

- `review_automation.timeout_minutes`
- `review_automation.on_failure`
- `review_automation.prompt_template`
- `review_automation_local_agent.command_timeout_minutes`
- `review_automation_local_agent.include_review_package_inline`
- `review_automation_model.max_candidates`
- `review_automation_model.temperature`
- `review_automation_model.max_tokens`
- `review_automation_model.retry_attempts`
- `review_automation_local_agent.allow_agent_file_writes`：只读禁用展示，文案说明“安全边界固定关闭”

#### Web 控制台

- `web.host`
- `web.port`
- `web.access_token_configured`：只读状态

## 交互规则

### 保存与校验

- 继续使用现有“检查配置 / 保存配置 / 重载配置 / 恢复默认 / 重启服务”按钮。
- 保存仍先备份，再原子写入，再用 `load_settings()` 复验。
- 配置分层只改变字段展示位置，不改变提交 payload 的结构。
- 高级字段即使收起，也必须随当前 config payload 一起保留，避免保存默认视图时丢失高级配置。

### 展开状态

- `快速开始` 和 `自动化` 默认展开。
- `高级设置` 默认收起。
- 高级分组的展开状态保存在浏览器本地状态即可，不写入 TOML。

### 文案

- 页面文案优先中文。
- 特殊名词保留英文：`Codex CLI`、`Claude Code`、`LLM`、`ASR`、`API key`、`Scheduler`、`TOML`。
- 参数名不直接作为主要文案出现，必要时作为辅助说明。

### 错误呈现

- 普通用户区域的错误要使用用户语言，例如“录播源目录不存在，请确认 NAS 已挂载”。
- 高级字段错误可以带字段路径，但必须同时有中文解释。
- 如果错误来自收起的高级字段，保存/校验结果要提示所在高级分组，并提供“展开查看”动作。

## 实现边界

### 前端

重点修改：

- `src/live_clipper/web_static/index.html`
- `src/live_clipper/web_static/app.js`
- `src/live_clipper/web_static/styles.css`

建议新增轻量级 field metadata，统一描述字段层级：

- `essential`
- `automation`
- `advanced`

也可以直接通过 DOM 结构重排，前提是测试覆盖字段仍能读写。

### 后端

原则上不需要改变配置 API shape。只在必要时补充只读状态聚合：

- 如果现有前端已能分别调用 service/scheduler/review/config API 组合出体检区，则不新增 API。
- 如果组合成本过高，可新增一个轻量 `GET /api/config/health`，但它只聚合现有状态，不引入新状态源。

### 测试

必须覆盖：

1. 静态页面包含 `配置体检`、`快速开始`、`自动化`、`高级设置`。
2. 快速开始默认区域不展示高级字段文案，例如 `tick 秒数`、`Temperature`、`Max tokens`。
3. 高级设置中仍能找到所有被收纳字段。
4. 保存配置 payload 不丢失高级字段。
5. AI 审阅、定时任务的现有 API 和测试保持通过。
6. README 更新配置页说明，强调“常用配置默认展示，高级配置默认收起”。

验收命令建议：

```bash
.venv/bin/python -m pytest tests/test_config_editor.py tests/test_web_v4_config.py tests/test_web_scheduler.py tests/test_web_review_automation.py tests/test_docs.py -q
.venv/bin/python -m pytest -q
uv run --with ruff ruff check src/live_clipper/web.py src/live_clipper/config_editor.py src/live_clipper/web_static tests/test_config_editor.py tests/test_web_v4_config.py tests/test_web_scheduler.py tests/test_web_review_automation.py tests/test_docs.py
node --check src/live_clipper/web_static/app.js
git diff --check
```

## 验收标准

1. 用户打开配置页时，第一屏能看见配置体检和快速开始入口。
2. 默认展开区域不再出现大批低频参数。
3. 定时任务和 AI 审阅保留在同一个配置页中，且作为“自动化”分组。
4. 高级配置默认收起，但所有旧字段仍可编辑或查看。
5. 检查、保存、重载、恢复默认、重启服务能力保持可用。
6. 保存配置不会丢失高级字段。
7. 全页面中文文案比例提升，特殊名词保留英文。
8. 现有安全边界不回退：密钥不明文展示，删除/清理仍走 confirmation，AI 不直接写文件。

## 后续可选优化

- 给常用配置提供“安装向导”模式，但 V7 不做。
- 增加目录选择器，但 V7 不做。
- 支持多录播源配置，但 V7 不做。
- 为高级配置增加搜索框，但 V7 不做。
