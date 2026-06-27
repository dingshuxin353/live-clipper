# live-clipper 小白友好化改造 Spec

日期：2026-06-27

## 背景

当前项目已经具备开源基础：中文 README、统一配置文件、提示词导出、隐私日志、Web 本机默认、安全文档和 CI。但它仍然更像“工程师可用的工具”，不是“小白能顺着提示一步步跑起来的产品”。

主要问题：

- 首次使用仍需要理解 Python 虚拟环境、editable install、`.env`、`live-clipper.toml`、模型后端、ASR 后端、提示词目录等概念。
- 配置项虽然集中，但字段名称和说明仍偏工程化，例如 `llm.api_base`、`asr.backend`、`failure_log_mode`。
- README 的流程是正确的，但小白用户容易不知道“下一步该运行哪个命令”“失败后该看哪里”“哪些东西需要自己填”。
- 即使新增 `setup`、`start`、`next` 等友好命令，用户仍可能卡在“我该选哪个模型服务”“本地 ASR 模型怎么下”“当前 Agent 软件怎么配置定时任务”这些判断题上。
- Codex 定时任务提示词仍是英文，不符合中文用户心智，也不适合直接复制给小白用户。
- 项目目录里 `input/`、`output/`、`work/`、`prompts/`、`docs/` 的职责还需要更直观地呈现。

本 Spec 只定义设计，不直接实施代码。

## 目标用户

主要用户：

- 中文创作者、运营、剪辑协作者。
- 有直播录制文件，希望自动找出可发布片段。
- 能复制粘贴命令，但不熟悉 Python 包管理、配置文件结构、API endpoint 等工程概念。
- 愿意用 Codex 做定时自动化，但需要中文提示词和明确步骤。

非目标用户：

- 需要多人权限、云端托管、完整 SaaS 后台的团队。
- 完全不愿打开终端的用户。这个版本可以降低命令行负担，但不承诺一键桌面 App。

## 设计原则

1. **先跑通，再解释。** README 和 CLI 应先给用户一条最短成功路径，详细概念放到二级文档。
2. **配置用人话分组。** 小白看到的配置应是“视频放哪里”“用哪个模型”“要不要局域网访问”，而不是工程对象名称。
3. **危险操作默认保守。** Web 默认本机访问，清理默认预演，失败日志默认脱敏。
4. **Codex 提示词可直接复制。** 定时任务提示词必须中文、明确、可粘贴，不要求用户理解内部状态机。
5. **保留高级入口。** 工程用户仍可使用现有子命令和完整 TOML 配置。
6. **让 AI 陪用户做。** 对小白用户，优先提供一份可复制给 AI 的中文使用说明，让 AI 根据用户环境一步步生成配置、解释报错、安排 Codex 定时任务。

## 推荐方案

采用“面向用户 AI 的使用说明文档 + 引导式 CLI + 友好配置文件 + 中文提示词模板 + 目录重排说明”的增量方案。

不做完整 GUI 安装器，也不把所有命令藏起来。第一阶段新增小白入口，让现有专业命令继续可用：

- `docs/ai-assistant-guide.md`：给用户复制到 AI 聊天窗口的中文陪跑说明。
- `live-clipper guide ai`：在终端输出这份 AI 陪跑说明，方便用户复制。
- `live-clipper setup`：交互式初始化项目。
- `live-clipper start <视频文件>`：一条命令跑扫描、可选复评和审阅包生成。
- `live-clipper next`：查看当前最应该做什么。
- `live-clipper codex prompts`：输出两段中文 Codex 定时任务提示词。
- `live-clipper doctor --friendly`：用中文解释缺什么、怎么补。

取舍：

- 优点：实现成本可控；不破坏现有测试和工作流；能立刻改善小白体验。
- 缺点：仍需要终端；首次安装 Python/ffmpeg 还需要文档引导。

## 目标体验

### 小白首次使用路径

推荐路径不是让用户独自理解所有命令，而是让用户把 `docs/ai-assistant-guide.md` 发给自己的 AI 助手。AI 助手根据用户电脑环境、模型 key、视频目录和当前 Agent 软件的定时任务能力，逐步给出命令并解释每一步结果。

用户看到 README 后，有两条入口：

1. 不熟悉命令行：打开 `docs/ai-assistant-guide.md`，复制给自己的 AI 助手，让 AI 陪跑。
2. 熟悉命令行：按顺序执行：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev,mlx]'
.venv/bin/live-clipper setup
.venv/bin/live-clipper smoke
.venv/bin/live-clipper start input/my-live.mp4
```

`setup` 负责：

- 创建 `live-clipper.toml`。
- 创建 `.env` 模板。
- 创建 `input/`、`output/`、`work/`、`prompts.local/`。
- 导出默认提示词。
- 使用本机 ASR 默认配置，不询问云端 ASR。
- 询问用户是否已经有 OpenAI-compatible LLM API key。
- 用中文解释哪些配置现在可以先留空。

`start` 负责：

- 检查视频文件是否存在。
- 自动选择输出目录。
- 默认跳过复杂选项，跑一个“推荐流程”。
- 结束时明确告诉用户下一步：
  - 如果已经生成 `codex_brief.json`：提示“现在需要 Codex 或人工选片”。
  - 如果已经生成 `selected_clips.json`：提示“可以渲染”。
  - 如果失败：提示运行 `live-clipper next` 和查看哪份日志。

### 日常使用路径

用户每次只需要：

```bash
.venv/bin/live-clipper start input/本周直播.mp4
.venv/bin/live-clipper next
```

如果已经配置 Agent 定时任务，用户只需要查看 Web 控制台或 output 目录结果。

## 面向用户 AI 的使用说明文档

新增 `docs/ai-assistant-guide.md`，作为小白首选入口。它不是给开发者看的 API 文档，而是一份可以完整复制给 ChatGPT、Codex、Claude 或其他 AI 助手的中文操作说明。

### 文档定位

这份文档要让用户完成：

- 初始化项目目录。
- 创建或检查 `.env`。
- 创建或检查 `live-clipper.toml`。
- 使用本机 ASR 默认配置。
- 配置模型服务地址、模型名称和密钥环境变量。
- 下载或首次预热本地 ASR 模型。
- 跑通 `doctor`、`smoke` 和第一次 `start`。
- 生成并配置 Agent 定时任务提示词。
- 遇到错误时，把终端输出交给 AI 继续诊断。

### README 中的呈现方式

README 首屏增加“小白推荐方式”：

```text
如果你不熟悉命令行：
1. 打开 docs/ai-assistant-guide.md；
2. 把全文复制给你常用的 AI 助手；
3. 按 AI 的问题一步步回答；
4. AI 会帮你生成配置、检查环境、配置 Codex 定时任务。
```

这个入口应排在完整命令行教程之前。

### AI 助手工作规则

`docs/ai-assistant-guide.md` 应明确要求 AI 助手：

- 一次只问用户 1 到 2 个问题，避免把用户淹没。
- 先自动检测用户系统、Python、ffmpeg 和当前目录，不把可检测问题丢给用户。
- 如果缺少 Python 或 ffmpeg，安装前必须说明目的、下一步会做什么，并征得用户同意。
- 让用户复制终端输出回来，再判断下一步。
- 不要求用户把 API key 粘贴到聊天窗口。
- 只指导用户把 key 写入本机 `.env` 文件。
- 不删除原始视频。
- 不默认开启局域网访问。
- 当前只支持本机 ASR，不询问是否使用云端 ASR。
- 配置清理任务时，必须先预演再确认。
- 如果用户不知道怎么选，优先使用默认推荐配置。

### AI 助手先自动检测

AI 助手先让用户运行一小组环境检测命令，并根据输出判断系统、Python、ffmpeg 和项目目录：

```bash
uname -a
python3 --version || python --version
ffmpeg -version
pwd
```

Windows 使用 PowerShell 对应命令：

```powershell
$PSVersionTable.OS
py --version
ffmpeg -version
Get-Location
```

只有无法自动判断的问题才询问用户：

1. 你准备把直播录制视频放在哪个目录？
2. 你是否已经有 OpenAI-compatible 的模型 API key？只确认有无，不收集真实 key。
3. 你是否希望使用当前 Agent 软件的定时任务能力自动处理录制？
4. 如果要配置定时任务，你希望多久检查一次新录制，多久检查一次待选片任务？

### AI 助手应生成的配置

AI 助手根据用户回答生成两类文件内容：

```text
.env
live-clipper.toml
```

要求：

- `.env` 只写变量名和占位说明，不在聊天中收集真实密钥。
- `.env` 写模型服务密钥，例如 `CHEAP_MODEL_API_KEY=...`。
- `live-clipper.toml` 写非敏感配置，例如输入目录、输出目录、模型服务地址、模型名称、本机 ASR 模型、提示词目录、Web 端口。
- `prompts.local/` 写用户可修改的提示词模板。
- 如果用户不知道模型服务地址，先使用 README 推荐的默认值，并提示用户之后可修改。
- 当前只支持本机 ASR，默认使用 `mlx_whisper` 和 `mlx-community/whisper-large-v3-turbo`。
- 提示首次运行本机模型可能会下载模型，耗时较长。

### AI 助手应解释模型服务

在询问用户是否有模型 API key 前，AI 助手必须先解释：

- 本地 ASR 模型负责把音频转成带时间戳的文字稿。
- 用户提供的模型服务是用来做文字理解和判断，不是用来做语音识别。
- 这个模型参与 ASR 文字校对、候选片段打分、候选复评和生成给 Agent 审阅的材料。
- 模型服务需要兼容 OpenAI Chat Completions 风格接口，配置时通常需要 `api_base`、`model` 和 API key。

必须给出示例：

- 火山方舟：`api_base = "https://ark.cn-beijing.volces.com/api/v3"`，`model` 使用用户自己的方舟推理接入点 ID 或模型名。
- 阿里百炼：`api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"`，示例 `model = "qwen-plus"`。
- Agnes：`api_base = "https://apihub.agnes-ai.com/v1"`，示例 `model = "agnes-2.0-flash"`。

AI 助手不得要求用户把真实 API key 粘贴到聊天窗口，只能指导用户写入本机 `.env`。

### AI 助手应处理本地 ASR 模型下载

AI 助手必须补上本地 ASR 模型下载或首次预热步骤。

要求：

- 先说明下载目的：本地 ASR 模型用于把直播音频转成文字稿。
- 告诉用户首次下载可能较慢，并占用本机磁盘空间。
- 征得用户同意后，再让用户运行预热命令。
- 默认模型为 `mlx-community/whisper-large-v3-turbo`。
- 如果下载失败，解释这是本地 ASR 模型下载问题，不是用户提供的 LLM API key 问题。

### AI 助手应带用户运行的命令

AI 助手不一次性丢出全部命令，而是按阶段给用户执行：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev,mlx]'
.venv/bin/live-clipper setup
.venv/bin/live-clipper doctor --friendly
.venv/bin/live-clipper smoke
.venv/bin/live-clipper start input/示例视频.mp4
.venv/bin/live-clipper codex prompts
```

Windows 用户由 AI 助手替换为对应的 `.venv\Scripts\...` 命令。

### Agent 定时任务陪跑

AI 助手需要解释：Agent 的介入不是 live-clipper 内部自己召唤，而是用户在当前 Agent 软件里配置两个定时任务，让 Agent 定期打开项目、运行命令、读取任务文件并写回结果。

不要把定时任务限定为 Codex。AI 助手应根据当前运行的 Agent 软件判断是否支持定时任务、周期任务、自动化、计划任务或后台工作流。如果不支持，应指导用户使用系统 `cron`、macOS `launchd`、Windows 任务计划程序，或手动运行对应命令。

建议配置两个定时任务：

1. **录制检测任务**
   - 作用：定期检查录制目录是否有新视频。
   - 建议频率：直播结束后可能产生录制的时间段，每 10 到 30 分钟一次。
   - 使用提示词：由 `live-clipper codex prompts` 输出的“录制检测定时任务提示词”。
2. **选片与收尾任务**
   - 作用：定期检查是否有 `codex_task.md`，由当前 Agent 选片、渲染、诊断或清理。这里的 `codex_task.md` 是项目内部任务文件名，不代表只能使用 Codex。
   - 建议频率：录制检测任务之后的时间段，每 10 到 20 分钟一次。
   - 使用提示词：由 `live-clipper codex prompts` 输出的“选片与收尾定时任务提示词”。

AI 助手需要引导用户确认：

- Agent 定时任务运行目录是 live-clipper 项目根目录。
- 两个定时任务可以分开配置，避免一个任务既找新录制又做选片收尾导致提示词过长。
- 用户能在当前 Agent 的任务记录里看到执行记录、修改文件列表和失败原因。
- 当 Agent 写入 `selected_clips.json` 或渲染完成时，用户感知到的结果是 output 目录出现新文件、Web 控制台状态变化，以及 Agent 定时任务记录里的中文总结。

### 可复制给 AI 的核心提示词草案

`docs/ai-assistant-guide.md` 应包含以下提示词，并允许用户直接复制：

```text
你是 live-clipper 的安装与配置助手。请用中文一步步带我完成初始化、配置、第一次运行和 Agent 定时任务配置。

工作方式：
1. 一次只问我 1 到 2 个问题。
2. 每次只让我执行一小组命令。
3. 让我把终端输出贴回来后，你再判断下一步。
4. 不要让我把 API key 发给你；只指导我把 key 写入本机 .env 文件。
5. 不要删除我的原始视频。
6. 如果我不知道怎么选，请使用 live-clipper 推荐默认值。
7. 先自动检测能检测的事情，不要问我电脑系统、是否安装 Python、是否安装 ffmpeg。
8. 如果需要安装 Python 或 ffmpeg，先说明目的、下一步会做什么，并征得我同意。
9. 当前只支持本机 ASR，不要问我是否使用云端 ASR。
10. 不要把定时任务限定为 Codex；请根据你当前运行的 Agent 软件判断怎么配置定时任务。

你需要帮我完成：
- 检查 Python、ffmpeg 和项目目录；
- 解释用户提供的模型服务是用来做文字理解和判断，并给出火山方舟、阿里百炼、Agnes 示例；
- 创建 .env 和 live-clipper.toml；
- 使用本机 ASR 默认配置；
- 下载或首次预热本地 ASR 模型；
- 运行 doctor、smoke 和第一次 start；
- 生成 Agent 定时任务提示词；
- 指导我在当前 Agent 软件中分别配置“录制检测任务”和“选片与收尾任务”；
- 最后给我一份配置完成清单。

现在请先让我运行环境检测命令，并根据输出判断下一步。
```

### 配置完成清单

AI 助手最后应输出：

```text
已完成：
- Python 虚拟环境
- Python 依赖安装
- ffmpeg 检查
- .env
- live-clipper.toml
- input/output/work 目录
- smoke 测试
- 第一次 start
- Agent 录制检测定时任务
- Agent 选片与收尾定时任务

你以后只需要：
1. 把录制视频放进指定目录；
2. 等当前 Agent 定时任务处理；
3. 到 output 目录查看结果。
```

## 项目结构改造

当前目录保留，但对小白展示为四个核心目录：

```text
live-clipper/
  input/            放原始视频
  output/           看处理结果和成片
  prompts.local/    改提示词
  live-clipper.toml 改常用设置
```

内部目录继续存在，但 README 不放在首屏解释：

```text
work/               断点、日志、中间缓存
src/                开发源码
tests/              自动化测试
docs/               详细文档
```

新增建议：

- 创建 `examples/`，放合成示例和小白教程截图说明，不放真实直播素材。
- 创建 `docs/ai-assistant-guide.md`，作为 README 首屏推荐的小白 AI 陪跑入口。
- 创建 `docs/beginner/`，拆分小白文档：
  - `docs/beginner/installation.md`
  - `docs/beginner/first-run.md`
  - `docs/beginner/config-wizard.md`
  - `docs/beginner/codex-schedule.md`
  - `docs/beginner/common-errors.md`

## 配置友好化设计

### 双层配置

保留现有 `live-clipper.toml` 作为机器可读配置，但新增“小白视图”概念。

`setup` 生成的配置文件应按用户问题组织：

```toml
[基础]
输入目录 = "input"
输出目录 = "output"
界面端口 = 8765

[模型]
服务地址 = "https://apihub.agnes-ai.com/v1"
模型名称 = "agnes-2.0-flash"
密钥环境变量 = "CHEAP_MODEL_API_KEY"

[语音识别]
方式 = "本机"
语言 = "中文"
本机模型 = "mlx-community/whisper-large-v3-turbo"

[提示词]
目录 = "prompts.local"

[隐私]
失败日志 = "脱敏"
```

程序内部仍可映射到现有结构：

- `[基础].输入目录` -> `paths.input_dir`
- `[模型].服务地址` -> `llm.api_base`
- `[语音识别].方式 = "本机"` -> `asr.backend = "mlx_whisper"`
- `[隐私].失败日志 = "脱敏"` -> `privacy.failure_log_mode = "redacted"`

### 配置帮助命令

新增：

```bash
live-clipper config explain
live-clipper config check
live-clipper config set 模型.服务地址 https://example.com/v1
```

`config explain` 输出中文说明，不展示内部 dataclass。

`config check` 输出：

- 当前输入目录是否存在。
- 当前输出目录是否可写。
- 模型 key 是否存在。
- ASR 后端是否可用。
- 提示词目录是否存在。
- Web 是否只监听本机。

### 配置文件兼容策略

第一阶段支持两种格式：

- 现有英文 TOML 分组。
- 新增中文 TOML 分组。

如果两者同时存在，英文高级配置优先；`setup` 默认生成中文配置，并在文件顶部说明“高级用户可参考 docs/configuration.md 使用英文配置”。

## 命令行友好化设计

### 新增命令

#### `live-clipper setup`

用途：首次初始化。

交互问题：

1. 你的视频一般放在哪里？
2. 你想用本机语音识别还是云端语音识别？
3. 你是否已经有 LLM API key？
4. 是否导出提示词供你修改？
5. 是否启动本机 Web 控制台？

要求：

- 每个问题提供默认值。
- 用户一路回车也能生成可用模板。
- 不在终端里回显 API key。
- 如果 `.env` 已存在，不覆盖，提示用户手动检查。

#### `live-clipper start <video>`

用途：推荐的一条命令处理视频。

默认行为：

- 使用配置里的输入/输出路径。
- 自动生成输出目录。
- 使用 `scan`。
- 如果配置开启 `auto_refine = true`，继续运行 `refine`。
- 自动运行 `brief`。
- 最后运行 `status` 并输出“下一步”。

#### `live-clipper next`

用途：告诉小白下一步做什么。

输出示例：

```text
当前发现 1 个任务需要处理：

output/2026-06-27-live/
状态：候选包已生成，等待选片

下一步：
1. 打开 Codex，把 output/2026-06-27-live/codex_task.md 的内容发给它；
2. 或者配置 Codex 定时任务，让它自动写 selected_clips.json；
3. 写好 selected_clips.json 后运行：
   .venv/bin/live-clipper render output/2026-06-27-live/selected_clips.json
```

#### `live-clipper codex prompts`

用途：输出中文 Codex 定时任务提示词。

可选参数：

```bash
live-clipper codex prompts --source-dir /path/to/recordings
live-clipper codex prompts --output docs/codex-prompts.md
```

#### `live-clipper guide ai`

用途：输出 `docs/ai-assistant-guide.md` 的核心内容，让用户可以直接复制给自己的 AI 助手。

可选参数：

```bash
live-clipper guide ai
live-clipper guide ai --output docs/my-ai-guide.md
```

输出要求：

- 默认输出中文。
- 包含安装、配置、首次运行、Codex 定时任务、常见错误处理。
- 明确提醒不要把 API key 粘贴给 AI。
- 如果项目中存在用户配置，允许在说明里带入当前输入目录、输出目录、提示词目录等非敏感信息。

## Codex 中文提示词设计

### 录制检测定时任务提示词

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

### Codex 决策定时任务提示词

```text
你是 live-clipper 的选片与收尾助手。

你的任务是：检查 live-clipper 是否有需要 Codex 判断的任务，并按任务文件要求处理。

第一步，请运行：

  .venv/bin/live-clipper automation check --output-root output

然后阅读 JSON 结果。

如果 requires_codex 是 false：
- 告诉我“当前没有需要 Codex 处理的任务”。
- 不要修改文件。

如果 codex_tasks 里有任务：
- 对每个任务，先打开 codex_task_file。
- 严格按 codex_task.md 的要求执行。

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

## README 改造

README 首屏只保留三件事：

1. 这个工具能做什么。
2. 小白如何让自己的 AI 陪跑安装和配置。
3. 如果卡住怎么办。

建议结构：

```text
# live-clipper
## 30 秒理解
## 小白推荐：把 AI 使用说明发给你的 AI
## 第一次使用：照着做
## 每次剪直播：只用这两个命令
## 配置 API Key
## 配置 Codex 定时任务
## 常见问题
## 进阶文档
```

现有详细命令表移到 `docs/workflow.md`。

README 中应明确区分两种路径：

- **AI 陪跑路径**：推荐给小白用户，入口是 `docs/ai-assistant-guide.md`。
- **命令行路径**：推荐给熟悉终端的用户，入口是 `setup`、`doctor --friendly`、`smoke`、`start`。

## Web 控制台友好化

第一阶段不重做 UI，但文案要从工程状态改成用户动作：

- `needs_codex_selection` 显示为“等待选片”。
- `ready_to_render` 显示为“可以渲染”。
- `cleanup_ready` 显示为“可以清理本地中间文件”。
- `failed_needs_codex` 显示为“处理失败，需要诊断”。

按钮文案：

- `检查任务`
- `渲染成片`
- `预演清理`
- `删除本机视频副本`

页面需要提示：

- Web 默认只适合本机使用。
- 局域网访问需要用户明确开启。
- 清理不会删除录制源目录中的原始视频。

## 错误提示友好化

新增错误解释层，把常见错误转成中文建议。

示例：

- 缺少 `CHEAP_MODEL_API_KEY`：
  - “缺少模型 API Key。请打开 `.env`，填写 `CHEAP_MODEL_API_KEY=你的密钥`。”
- 缺少 `ffmpeg`：
  - “没有找到 ffmpeg。Mac 用户可以运行 `brew install ffmpeg`。”
- 缺少输入视频：
  - “`input/` 目录里没有视频。请把 `.mp4`、`.mov` 或 `.mkv` 文件放进去。”
- Web 局域网无法访问：
  - “请确认使用了 `live-clipper web --host 0.0.0.0`，并检查系统防火墙。”

## 测试要求

新增或更新测试：

- `tests/test_cli.py`
  - `setup` 能生成中文配置和 `.env` 模板。
  - `start` 能串联 scan/refine/brief。
  - `next` 能根据状态输出中文下一步。
  - `codex prompts` 输出中文提示词。
  - `guide ai` 能输出中文 AI 陪跑说明。
- `tests/test_config.py`
  - 中文配置字段能映射到内部设置。
  - 英文高级配置仍兼容。
- `tests/test_web.py`
  - Web 阶段文案改为用户动作。
- `tests/test_prompt_loader.py`
  - 中文 Codex 模板可导出。
- `tests/test_docs.py`
  - `docs/ai-assistant-guide.md` 存在。
  - 文档明确禁止用户把 API key 粘贴到聊天窗口。
  - 文档包含两个 Codex 定时任务的配置说明。

保留全量验证：

```bash
.venv/bin/python -m pytest -q
uv run --with build --with twine python -m build
uv run --with twine python -m twine check dist/*
```

## 分阶段交付

### 阶段 1：最短上手路径

- 新增 `docs/ai-assistant-guide.md`，作为 README 首屏推荐入口。
- 新增 `live-clipper guide ai`。
- 新增 `setup`。
- 新增 `start`。
- 新增 `next`。
- README 改为小白路径。

### 阶段 2：配置和提示词中文化

- 支持中文 TOML 配置。
- `config explain/check/set`。
- `codex prompts` 输出中文模板。

### 阶段 3：Web 和错误提示友好化

- Web 状态改为用户动作。
- 常见错误转中文建议。
- docs/beginner 文档拆分完成。

## 成功标准

一个没有项目背景的新用户，只看 README，应能做到：

1. 知道可以把 `docs/ai-assistant-guide.md` 复制给自己的 AI 助手。
2. 在 AI 助手陪跑下安装依赖。
3. 在 AI 助手陪跑下生成 `.env` 和 `live-clipper.toml`。
4. 运行 `setup`。
5. 跑通 `smoke`。
6. 对一个本地视频运行 `start`。
7. 知道 Codex 是通过两个定时任务介入流程。
8. 能复制中文 Codex 定时任务提示词。
9. 遇到缺 key、缺 ffmpeg、缺输入视频时知道把输出交给 AI 诊断。

## 暂不做

- 桌面安装器。
- 多用户权限系统。
- 云端托管服务。
- 自动购买或管理第三方模型 key。
- 面向所有操作系统的完整 ffmpeg 图形安装向导。
