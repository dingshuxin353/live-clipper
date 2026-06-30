# V4 Web 可视化配置设计

## 背景

当前 MCP 工作台版本已经具备：

- 本机常驻服务：扫描录播源、复制录播、启动 pipeline、跟踪 run lifecycle。
- MCP 工具面：让 AI 读取状态、审阅包、写入选片、触发渲染、创建删除确认请求。
- Web 控制台：查看服务、任务、确认队列、日志和只读设置。

但当前 `设置` 页只能只读展示少量配置。用户仍需要手动编辑 `live-clipper.toml`，这对小白用户不友好，尤其是录播源目录、输入/输出目录、模型服务、ASR、扫描频率这些高频必要配置。

V4 的目标是把“必要配置”做成 Web 可视化配置，不做完整高级配置编辑器。

## 产品目标

用户不需要打开 `live-clipper.toml`，就能在 Web 端完成第一次可运行配置。

具体目标：

1. 在 Web 控制台新增可编辑的 `配置` 页面。
2. 支持查看、修改、保存必要配置。
3. 保存前进行校验，保存后写回 `live-clipper.toml`。
4. 明确区分“立即生效”和“需要重启服务/控制台生效”的配置。
5. 不在 Web 页面保存明文 API key。
6. 保留高级用户继续手动编辑 `live-clipper.toml` 的能力。

## 非目标

V4 不做以下内容：

- 不做多录播源管理。仍只编辑 `[recording_source.default]`。
- 不做完整 TOML 任意字段编辑器。
- 不在 Web 页面写入 `.env` 中的明文密钥。
- 不做公网、多用户、权限系统。
- 不做云端配置同步。
- 不做复杂 prompt 编辑器。
- 不做 ASR 模型下载管理器。
- 不自动重启 Web 控制台自身。

## 用户画像

主要用户：

- 对 Python、TOML、命令行不熟悉。
- 有 NAS 或本地录播目录。
- 想通过 Web 页面完成项目配置。
- 会让 AI 帮忙选片，但希望基础配置自己能看懂、能改。

高级用户：

- 可以继续直接编辑 `live-clipper.toml`。
- 可以通过 Web 页面快速确认当前有效配置。

## 成功标准

V4 完成后，用户可以完成以下闭环：

1. 启动 Web 控制台。
2. 打开 `配置` 页面。
3. 填写录播源目录、输入目录、输出目录。
4. 填写 LLM 服务地址、模型名、API key 环境变量名。
5. 选择 ASR 后端、模型、语言。
6. 设置服务扫描间隔和自动渲染开关。
7. 点击 `检查配置`，看到明确的通过/失败结果。
8. 点击 `保存配置`，写回 `live-clipper.toml`。
9. 如果服务正在运行，页面提示需要重启服务才能让新配置稳定生效。
10. 用户可以在同一页面点击 `重启服务`，服务重新加载配置。

## 信息架构

Web 控制台继续保留五个主要页面，并将 `设置` 改造为 `配置`。

页面建议：

- `服务`
- `任务`
- `确认`
- `日志`
- `配置`

`配置` 页面分为四个分区：

1. `基础路径`
2. `录播源`
3. `AI 与 ASR`
4. `服务行为`

高级信息放在折叠区 `高级配置` 中，只读或低优先级编辑。

## 可编辑字段白名单

### 基础路径

这些字段影响项目内输入输出位置：

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| 输入目录 | `[paths].input_dir` | path | `input` | 是 |
| 输出目录 | `[paths].output_root` | path | `output` | 是 |
| 工作目录 | `[paths].work_dir` | path | `work` | 否 |
| 术语表路径 | `[paths].glossary_path` | path | `glossary/common_terms.json` | 否 |

说明：

- P0 页面默认只展示输入目录和输出目录。
- 工作目录、缓存目录、日志目录、状态目录可先只读展示或放到高级折叠区。
- 保存时，`[recording_source.default].input_dir` 和 `[recording_source.default].output_root` 默认跟随基础路径，除非用户在高级模式中显式覆盖。

### 录播源

这些字段是常驻服务自动扫描 NAS 录播的核心：

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| 录播源目录 | `[recording_source.default].source_dir` | path | 空 | 是 |
| 输入目录 | `[recording_source.default].input_dir` | path | `input` | 是 |
| 输出目录 | `[recording_source.default].output_root` | path | `output` | 是 |
| 回看时间窗口 | `[recording_source.default].since_hours` | integer | `168` | 是 |
| 最小文件年龄 | `[recording_source.default].min_age_minutes` | integer | `10` | 是 |
| 稳定性检查秒数 | `[recording_source.default].stable_check_seconds` | integer | `60` | 是 |

校验要求：

- `source_dir` 允许为空，但为空时服务扫描不可用，页面显示黄色提醒。
- 如果 `source_dir` 不为空，必须存在且是目录。
- `input_dir`、`output_root` 如果不存在，允许自动创建，保存前提示用户。
- `since_hours` 必须为 1 到 720。
- `min_age_minutes` 必须为 1 到 1440。
- `stable_check_seconds` 必须为 5 到 600。

### AI 与 ASR

#### LLM 配置

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| 模型服务名称 | `[llm].provider_label` | string | `OpenAI-compatible LLM` | 否 |
| API 地址 | `[llm].api_base` | url | `https://apihub.agnes-ai.com/v1` | 是 |
| API key 环境变量名 | `[llm].api_key_env` | string | `CHEAP_MODEL_API_KEY` | 是 |
| 模型名 | `[llm].model` | string | `agnes-2.0-flash` | 是 |
| 请求超时 | `[llm].timeout_seconds` | integer | `300` | 否 |
| 重试次数 | `[llm].request_attempts` | integer | `5` | 否 |
| 重试间隔 | `[llm].retry_delay_seconds` | float | `3.0` | 否 |

安全要求：

- 页面不显示、不写入明文 API key。
- 页面只显示环境变量名，例如 `CHEAP_MODEL_API_KEY`。
- 页面可显示“当前环境变量是否已配置”，只显示 `已配置` / `未配置`，不显示值。
- 如果未配置，提供说明：请在 `.env` 中写入 `CHEAP_MODEL_API_KEY=...`。

#### ASR 配置

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| ASR 后端 | `[asr].backend` | enum | `mlx_whisper` | 是 |
| ASR 模型 | `[asr].model` | string | `mlx-community/whisper-large-v3-turbo` | 是 |
| 语言 | `[asr].language` | string | `zh` | 是 |
| OpenAI ASR API 地址 | `[asr].api_base` | url | `https://api.openai.com/v1` | 条件 |
| ASR API key 环境变量名 | `[asr].api_key_env` | string | `ASR_API_KEY` | 条件 |
| Hugging Face token 环境变量名 | `[asr].hf_token_env` | string | `HF_TOKEN` | 否 |

ASR 后端选项：

- `mlx_whisper`：Apple Silicon 本地 ASR，默认推荐。
- `openai`：OpenAI-compatible 远程 ASR。

交互规则：

- 当后端是 `mlx_whisper` 时，隐藏或弱化 `api_base` 和 `api_key_env`。
- 当后端是 `openai` 时，显示 `api_base` 和 `api_key_env`，并检查对应环境变量是否已配置。
- `language` 默认 `zh`，允许用户填写 `auto` 或其他 ISO 语言码。

### 服务行为

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| 启用服务 | `[service].enabled` | boolean | `true` | 否 |
| 扫描间隔 | `[service].scan_interval_minutes` | integer | `30` | 是 |
| 选片后自动渲染 | `[service].auto_render_after_selection` | boolean | `true` | 是 |
| 清理模式 | `[service].cleanup_mode` | enum | `preview_only` | 是 |

校验要求：

- `scan_interval_minutes` 必须为 1 到 1440。
- V4 只允许 `cleanup_mode = "preview_only"`。
- 页面可以显示清理模式，但不提供其他可选项，避免误删风险。

### Web 控制台

| 页面字段 | TOML 字段 | 类型 | 默认值 | 是否 P0 |
| --- | --- | --- | --- | --- |
| Host | `[web].host` | string | `127.0.0.1` | 否 |
| Port | `[web].port` | integer | `8765` | 否 |
| Access token | `[web].access_token` | string | 空 | 否 |

V4 处理方式：

- 默认折叠在高级配置中。
- 页面提示：修改 Web host/port 后，需要重启 Web 控制台命令本身才会生效。
- `access_token` 暂不启用公网访问能力，仅保留只读或高级字段说明。

## 页面交互设计

### 顶部状态区

配置页顶部显示：

- 当前配置文件路径：`live-clipper.toml`
- 配置文件是否存在。
- 最后读取时间。
- 当前服务状态：运行中 / 已停止 / 异常。
- 是否存在未保存改动。

### 操作按钮

按钮：

- `检查配置`
- `保存配置`
- `重载配置`
- `恢复默认`
- `重启服务`

按钮行为：

- `检查配置`：只校验当前表单，不写文件。
- `保存配置`：校验通过后写入 `live-clipper.toml`。
- `重载配置`：丢弃未保存改动，从磁盘重新读取。
- `恢复默认`：只恢复表单默认值，不立即保存；需要二次确认。
- `重启服务`：如果服务运行中，先 stop 再 start；如果服务未运行，只提示无需重启。

### 保存后的反馈

保存成功后显示：

- `配置已保存`
- 哪些字段改变了。
- 哪些配置立即生效。
- 哪些配置需要重启服务。
- 如果 Web host/port 变化，提示需要手动重启 Web 控制台。

### 脏状态保护

如果表单有未保存改动：

- 切换页面时不阻止，但保留改动。
- 点击 `重载配置`、`恢复默认`、刷新页面前提示用户。
- 保存成功后清除脏状态。

## API 设计

### GET /api/config

读取当前配置。

返回：

```json
{
  "ok": true,
  "config_path": "live-clipper.toml",
  "exists": true,
  "config": {
    "paths": {},
    "recording_source_default": {},
    "service": {},
    "asr": {},
    "llm": {},
    "web": {}
  },
  "env_status": {
    "CHEAP_MODEL_API_KEY": true,
    "ASR_API_KEY": false,
    "HF_TOKEN": false
  },
  "warnings": []
}
```

要求：

- 不返回明文 secret。
- 只返回白名单字段。
- 如果 TOML 存在无法解析，返回 `ok: false` 和中文错误。

### POST /api/config/validate

校验前端提交的配置草稿。

请求：

```json
{
  "config": {
    "recording_source_default": {
      "source_dir": "/Volumes/homes/weixiaodan12/录播"
    }
  }
}
```

返回：

```json
{
  "ok": true,
  "errors": [],
  "warnings": [
    {
      "field": "recording_source_default.source_dir",
      "message": "目录存在，但当前没有发现可处理视频"
    }
  ],
  "changes": []
}
```

### POST /api/config

保存配置。

要求：

- 必须先执行同一套校验。
- 只写白名单字段。
- 写入前创建备份：`work/config_backups/live-clipper.<timestamp>.toml`。
- 写入使用原子替换，避免半写入。
- 写入后重新 `load_settings()` 验证。

返回：

```json
{
  "ok": true,
  "saved": true,
  "backup_path": "work/config_backups/live-clipper.20260630-120000.toml",
  "requires_service_restart": true,
  "requires_web_restart": false,
  "changes": [
    {
      "field": "service.scan_interval_minutes",
      "old": 30,
      "new": 15
    }
  ]
}
```

### POST /api/config/restart-service

重启常驻服务。

行为：

- 如果服务未运行，返回 `ok: true, restarted: false`。
- 如果服务运行中，调用现有 service stop/start。
- 不杀 pipeline 子进程，保持现有安全边界。

## 配置写入策略

V4 需要新增结构化配置写入层，不建议在 Web handler 中手写字符串拼接。

建议新增模块：

```text
src/live_clipper/config_editor.py
```

职责：

- 读取 `live-clipper.toml` 原始内容。
- 解析成 dict。
- 合并白名单字段。
- 校验字段类型和值域。
- 生成 TOML 文本。
- 备份旧文件。
- 原子写入新文件。
- 返回变更 diff。

实现建议：

- 优先使用项目已有依赖和标准库。
- 如果引入 TOML writer 依赖，需要选择小而稳定的库，并写入 `pyproject.toml`。
- 如果不引入依赖，可以实现一个只覆盖本项目配置结构的简单 TOML writer，但必须避免任意字符串拼接漏洞。
- 不要求保留用户手写注释；保存后可生成规范化 TOML，并在页面明确提示。

必须保留：

- 未纳入白名单的已知配置分组。
- 未纳入白名单的字段。

如果简单 writer 做不到可靠保留未知字段，则 V4 只能编辑完整已知配置模板，并在保存前明确提示“保存会规范化配置文件”。产品推荐：尽量保留未知字段。

## 校验规则

### 路径校验

- 路径允许相对路径和绝对路径。
- `source_dir` 如果填写，必须存在且为目录。
- `input_dir`、`output_root` 不存在时给出“将创建”的 warning，不算 error。
- 不允许 `input_dir` 或 `output_root` 指向 `source_dir` 内部，避免把 NAS 原始录播当成本地工作目录。
- 不允许 `input_dir` 和 `output_root` 相同。

### 数字校验

- 所有数字字段必须是数字，不接受空字符串。
- `scan_interval_minutes`: 1-1440。
- `since_hours`: 1-720。
- `min_age_minutes`: 1-1440。
- `stable_check_seconds`: 5-600。
- `timeout_seconds`: 30-3600。
- `request_attempts`: 1-10。
- `retry_delay_seconds`: 0-60。
- `web.port`: 1024-65535。

### 枚举校验

- `service.cleanup_mode`: 仅允许 `preview_only`。
- `asr.backend`: 允许 `mlx_whisper`、`openai`。
- `privacy.failure_log_mode`: 如果暴露高级字段，仅允许 `redacted`、`full`、`disabled`。

### Secret 校验

- 不校验 secret 值。
- 只校验 env var name 格式：`^[A-Z_][A-Z0-9_]*$`。
- 返回 env var 是否存在。

## 生效规则

立即生效：

- Web 页面展示类配置。
- 下一次 `load_settings()` 后读取的新配置。

需要重启 service：

- 录播源目录。
- 输入/输出目录。
- 扫描间隔。
- 自动渲染开关。
- 清理模式。
- ASR/LLM 配置。

需要重启 Web 控制台：

- `[web].host`
- `[web].port`

保存成功后，如果当前 service 正在运行，页面必须显示：

```text
配置已保存。为了让常驻服务使用新配置，请重启服务。
```

## UI 文案要求

延续当前产品原则：

- 所有页面文案能用中文就用中文。
- 特殊名词保留英文，例如 MCP、ASR、LLM、API key、Web、TOML、run。
- 错误信息必须用中文解释“怎么修”。

示例：

- `录播源目录不存在，请确认 NAS 已挂载，或重新选择目录。`
- `API key 环境变量 CHEAP_MODEL_API_KEY 未配置，请在 .env 中添加。`
- `配置已保存，但当前服务仍在使用旧配置，请点击重启服务。`

## 安全要求

- Web 默认仍只绑定 `127.0.0.1`。
- 不在 API 返回明文 secret。
- 不在日志中写入 secret。
- 不允许通过配置页面删除文件。
- 不允许配置页面修改确认队列。
- 保存配置前创建备份。
- 写入失败时保留原配置。
- 配置文件解析失败时，不允许覆盖，除非用户明确选择“用默认模板重建配置”，且有二次确认。

## 数据流

读取：

```text
浏览器配置页
  -> GET /api/config
  -> config_editor.load_editable_config()
  -> live-clipper.toml + .env 状态
  -> 白名单 JSON
```

保存：

```text
浏览器配置页
  -> POST /api/config/validate
  -> 展示错误/警告/diff
  -> POST /api/config
  -> 备份 live-clipper.toml
  -> 原子写入新 TOML
  -> load_settings() 验证
  -> 返回 restart 提示
```

重启服务：

```text
浏览器配置页
  -> POST /api/config/restart-service
  -> service.stop_service()
  -> service.start_service(load_settings())
  -> 返回新 service status
```

## 兼容性

- 现有 CLI `live-clipper config init` 保持不变。
- 现有 `load_settings()` 保持配置读取入口。
- 现有 `GET /api/settings` 可保留兼容，但新页面应使用 `GET /api/config`。
- 现有 Web `设置` 页可改名为 `配置`；旧前端路由和 DOM id 可以保留内部命名，不作为用户可见文案。
- 手动编辑 `live-clipper.toml` 后，点击 `重载配置` 应能读取最新值。

## 边界与取舍

### 推荐方案：白名单表单编辑

只开放必要字段，保存时只写白名单。

优点：

- 对小白用户清晰。
- 安全边界清楚。
- 测试范围可控。
- 不会让 Web 页面变成危险的任意配置编辑器。

缺点：

- 高级字段仍需要手动编辑 TOML。

### 备选方案：完整 TOML 编辑器

Web 页面提供文本编辑器，用户直接编辑 TOML。

优点：

- 实现较快。
- 高级字段全覆盖。

缺点：

- 对小白不友好。
- 容易写坏配置。
- 难以做字段级校验和中文解释。
- 不符合本需求的“可视化配置”目标。

### 备选方案：首次启动向导

只做 onboarding wizard，不做长期配置页。

优点：

- 初次配置体验好。

缺点：

- 后续修改配置仍不方便。
- 当前已有 Web 控制台，独立向导会增加入口复杂度。

产品选择：采用“白名单表单编辑”，后续可在此基础上增加首次配置向导。

## 验收标准

### 功能验收

- Web 端存在 `配置` 页面。
- 页面可读取当前 `live-clipper.toml` 的必要配置。
- 页面可编辑 P0 字段并保存。
- 保存后 `live-clipper.toml` 被更新。
- 保存前会生成配置备份。
- 保存后重新读取配置，页面展示新值。
- 如果服务正在运行，保存后显示重启提示。
- 点击 `重启服务` 后，service 使用新配置启动。
- Secret 值不会出现在 API、页面或日志中。
- 无效配置不能保存，并显示中文错误。

### 测试验收

需要新增或更新测试：

- `tests/test_config_editor.py`
  - 读取默认配置。
  - 校验合法配置。
  - 拒绝非法路径、非法数字、非法枚举。
  - 保存前创建备份。
  - 保存后可被 `load_settings()` 读取。
  - 不返回明文 secret。
- `tests/test_web_v4_config.py`
  - `GET /api/config`
  - `POST /api/config/validate`
  - `POST /api/config`
  - `POST /api/config/restart-service`
  - 解析失败时不覆盖配置。
- `tests/test_docs.py`
  - README 或用户指南包含配置页说明。

全量测试仍需通过：

```bash
.venv/bin/python -m pytest -q
```

触达文件 lint 通过：

```bash
uv run --with ruff ruff check src/live_clipper/config.py src/live_clipper/config_editor.py src/live_clipper/web.py tests/test_config_editor.py tests/test_web_v4_config.py
```

### 手工验收

1. 启动 Web 控制台。
2. 打开 `配置` 页面。
3. 修改 `scan_interval_minutes` 为 15。
4. 点击 `检查配置`，应通过。
5. 点击 `保存配置`，应成功。
6. 检查 `live-clipper.toml` 已更新。
7. 检查 `work/config_backups/` 有备份。
8. 如果 service 正在运行，页面显示需要重启。
9. 点击 `重启服务`，服务状态变为运行中。
10. 修改 `source_dir` 为不存在目录，保存应失败，并提示中文错误。

## 开发拆分建议

### V4.1 配置 API 与写入层

- 新增 `config_editor.py`。
- 新增 `GET /api/config`。
- 新增 `POST /api/config/validate`。
- 新增 `POST /api/config`。
- 完成测试覆盖。

### V4.2 Web 配置页面

- `设置` 改为 `配置`。
- 新增四个配置分区。
- 支持检查、保存、重载、恢复默认。
- 展示 env var 配置状态。
- 保存后展示重启提示。

### V4.3 服务重启与体验完善

- 新增 `POST /api/config/restart-service`。
- 页面支持一键重启服务。
- 补充用户指南和 README。
- 做一次真实页面 smoke。

## 产品风险

- TOML 写回如果处理不好，可能破坏用户手写配置。
  - 缓解：白名单、备份、原子写入、保存后 `load_settings()` 验证。
- Web 修改 host/port 后无法自动改变当前 Web 进程监听地址。
  - 缓解：明确提示需要手动重启 Web 控制台。
- 用户以为 API key 可以在页面里配置。
  - 缓解：页面只配置环境变量名，并明确说明密钥放 `.env`。
- 服务运行中保存配置后，用户以为已经生效。
  - 缓解：保存结果明确显示是否需要重启服务，并提供按钮。

## 已定产品判断

本 spec 做出以下产品判断：

1. V4 只支持单录播源 `[recording_source.default]`。
2. 明文 API key 不进入 Web 配置页。
3. `cleanup_mode` 只允许 `preview_only`。
4. 保存配置会创建备份。
5. 修改 Web host/port 后只提示，不自动重启 Web 控制台。

如后续需要支持多录播源、页面编辑 `.env`、公网访问权限或完整高级配置编辑器，应作为 V5 独立需求设计。
