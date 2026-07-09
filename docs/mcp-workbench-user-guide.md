# MCP 工作台小白使用指南

这份指南面向第一次使用 `live-clipper` 的用户，目标是把一场长直播录播变成可发布的短视频切片。

当前版本的核心能力是：

1. 常驻服务自动扫描录播源目录。
2. 发现稳定录播后，复制到本地项目库并开始切片流水线。
3. 流水线生成候选片段和审阅包。
4. AI 或人工审阅候选片段，写入最终选择文件。
5. 服务发现选择文件后自动渲染，或由你手动点击渲染。
6. 删除和清理动作进入确认队列，由你在 Web 控制台确认。

## 一句话理解

`live-clipper` 负责确定性的脏活累活：扫描录播、转文字、找候选、渲染视频。

AI 负责判断性的工作：从候选里挑出最值得发布的片段，并写入 `selected_clips.json`。

Web 控制台负责给人看：现在服务是否运行、哪些任务待审阅、哪些删除动作需要确认。

## 你需要先知道的几个词

- 项目目录：本项目所在目录，例如 `/path/to/live-clipper`。
- 录播源目录：你的 NAS 或本地录播目录，例如 `/Volumes/your-nas/recordings`。
- `input/`：本项目本地输入库。服务会把录播复制到这里处理。
- `output/`：每次处理任务的输出目录。
- run：一次录播处理任务。
- run 目录：某一次任务的文件夹，例如 `output/2026-06-27-21-00-16`。
- `codex_brief.json`：给 AI 看的候选片段审阅包。
- `selected_clips.json`：AI 或人工最终写入的选片结果。
- MCP 工具面：给 AI 调用的工具接口。当前版本提供 Python adapter，后续可以接 stdio/SSE MCP server wrapper。

## 第 0 步：进入项目目录

每次操作前，先打开终端并进入项目目录：

```bash
cd /path/to/live-clipper
```

确认当前分支是 MCP 工作台版本：

```bash
git branch --show-current
```

应该看到：

```text
codex/mcp-workbench
```

如果不是，可以切换：

```bash
git switch codex/mcp-workbench
```

## 第 1 步：做一次环境检查

先运行：

```bash
.venv/bin/live-clipper doctor
```

你主要看这几类信息：

- `ffmpeg` 是否可用。
- ASR 配置是否可用。
- LLM API key 是否已配置。
- `input/`、`output/` 等目录是否存在。

如果你不确定怎么修，把 `doctor` 的输出贴给 AI，让 AI 帮你逐项排查。

不要把 API key、Token、Cookie 之类密钥贴给 AI。密钥只放在本机 `.env` 文件里。

## 第 2 步：确认配置文件

推荐配置文件是 `live-clipper.toml`。如果没有，先创建：

```bash
.venv/bin/live-clipper config init
```

常驻服务至少需要这些配置：

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

这些配置的意思是：

- `scan_interval_minutes = 30`：服务每 30 分钟扫描一次录播源。
- `auto_render_after_selection = true`：AI 写好 `selected_clips.json` 后，服务会自动渲染。
- `cleanup_mode = "preview_only"`：渲染后只做清理预览，不直接删除文件。
- `source_dir`：你的 NAS 录播目录。
- `input_dir`：复制到本机后的输入目录。
- `output_root`：任务输出目录。
- `since_hours = 168`：只看最近 168 小时内修改过的录播。
- `min_age_minutes = 10`：至少 10 分钟没变化的文件才认为录制结束。
- `stable_check_seconds = 60`：再检查 60 秒，确认文件大小稳定。

## 第 3 步：启动 Web 控制台

Web 控制台是你观察整个系统的地方。

运行：

```bash
.venv/bin/live-clipper web
```

然后打开浏览器：

```text
http://127.0.0.1:8765
```

页面里会看到五个页签：

- `服务`：看服务状态，也可以启动、停止、立即扫描。
- `任务`：看每个录播任务的状态和详情。
- `确认`：处理删除、清理这类需要人工确认的动作。
- `日志`：看事件流和任务日志。
- `设置`：只读查看当前关键配置。

如果 8765 端口被占用，可以换端口：

```bash
.venv/bin/live-clipper web --port 8766
```

不要把 Web 控制台暴露到公网。

## 第 4 步：启动常驻服务

常驻服务负责扫描录播源目录、复制录播、启动切片流水线。

后台启动：

```bash
.venv/bin/live-clipper service start
```

查看状态：

```bash
.venv/bin/live-clipper service status --json
```

查看日志：

```bash
.venv/bin/live-clipper service logs
```

停止服务：

```bash
.venv/bin/live-clipper service stop
```

注意：`service stop` 只停止服务主进程，不会主动杀掉已经启动的 pipeline 子进程。这样是为了避免误中断正在处理的大视频。

## 第 5 步：让服务发现录播

如果配置好了录播源目录，服务会定时扫描。

你也可以在 Web 控制台的 `服务` 页点击 `立即扫描`。

或者在终端运行一次单轮扫描：

```bash
.venv/bin/live-clipper service start --once
```

发现录播后，服务会做这些事：

1. 检查录播文件是否在允许的时间窗口内。
2. 检查录播文件是否足够老，避免还在录制中。
3. 检查文件大小是否稳定。
4. 复制到 `input/`。
5. 创建一个 run id 和 run 目录。
6. 后台启动切片流水线。

你可以在 Web 控制台的 `任务` 页看到新任务。

## 第 6 步：理解任务状态

常见状态如下：

- `处理中`：系统正在跑 ASR、候选扫描、复评或生成审阅包。
- `待审阅`：审阅包已经生成，正在等 AI 或人工选片。
- `渲染中`：已经有 `selected_clips.json`，正在生成视频。
- `已成片`：切片视频已经生成。
- `失败`：某一步失败，需要看日志排查。

对你最重要的是 `待审阅`。

当任务进入 `待审阅`，就说明可以让 AI 选片了。

## 第 7 步：AI 选片到底是怎么发生的

当前版本不会让服务自己自动决定发布哪些片段。

正确流程是：

1. 服务生成 `codex_brief.json`。
2. AI 读取 `codex_brief.json`、`codex_review.md`，必要时参考 `refined_candidates.json`。
3. AI 从候选片段中挑选适合发布的片段。
4. AI 写入 `selected_clips.json`。
5. 服务看到 `selected_clips.json` 后自动渲染，或你手动点击 `渲染`。

所以，“让 AI 选片”的本质就是：

让 AI 读取审阅包，然后写一个合法的 `selected_clips.json`。

## 第 8 步：最推荐的 AI 选片方法

如果你在 Codex 里操作本项目，可以直接对 Codex 说：

```text
请帮我检查 live-clipper 当前是否有待审阅任务。

如果有，请读取对应 run 目录里的 codex_brief.json、codex_review.md 和 refined_candidates.json。
请像短视频剪辑师一样挑选最值得发布的片段，写入 selected_clips.json。

选片要求：
- 优先选择开头快、信息密度高、能独立成立的片段。
- 避免只适合直播上下文、单独看不懂的片段。
- 避免长时间沉默、寒暄、等待、重复解释。
- 如果片段里只有一小段废话，可以用 remove_ranges 去掉。
- 不要编造 clip_id，只能从候选包里选。
- 写完后运行验证或渲染命令。

完成后请告诉我：
- 处理的是哪个 run。
- 选择了几个片段。
- selected_clips.json 写在哪里。
- 是否已经开始或完成渲染。
```

AI 收到后，应该做这些动作：

1. 查找 `needs_review` 或 `待审阅` 任务。
2. 打开 run 目录。
3. 阅读 `codex_brief.json`。
4. 参考 `codex_review.md`。
5. 生成 `selected_clips.json`。
6. 触发渲染或等待服务自动渲染。

## 第 9 步：用 MCP 工具方式让 AI 选片

当前版本提供的是 MCP 工具函数层，也就是 `live_clipper.mcp_tools`。

如果 AI 可以运行 Python 或被接入到 MCP wrapper，它可以按这个工具流程选片：

1. 调用 `get_service_status` 查看服务状态。
2. 调用 `list_runs`，筛选 `phase = "needs_review"`。
3. 调用 `get_review_package` 读取审阅包。
4. 根据候选内容决定片段。
5. 调用 `write_selected_clips` 写入选择。
6. 调用 `render_run` 触发渲染，或等待服务自动渲染。
7. 调用 `preview_cleanup` 查看清理预览。

你也可以用终端模拟这个流程。

查看待审阅任务：

```bash
.venv/bin/python - <<'PY'
from live_clipper import mcp_tools
import json

result = mcp_tools.call_tool("list_runs", {"phase": "needs_review"})
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
```

读取某个 run 的审阅包，把 `RUN_ID` 换成实际 run id：

```bash
.venv/bin/python - <<'PY'
from live_clipper import mcp_tools
import json

result = mcp_tools.call_tool("get_review_package", {"run_id": "RUN_ID"})
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
```

AI 写选择时，工具调用格式是：

```python
from live_clipper import mcp_tools

mcp_tools.call_tool("write_selected_clips", {
    "run_id": "RUN_ID",
    "selected_clips": [
        {
            "clip_id": "w0001-c001",
            "source_start": 12.5,
            "source_end": 58.0,
            "title": "这里写中文标题",
            "remove_ranges": [[25.0, 28.0]]
        }
    ]
})
```

工具会先验证选择是否合法，再写入 `selected_clips.json`。如果不合法，会返回错误，不会覆盖正式文件。

## 第 10 步：人工检查 AI 的选片结果

AI 写完后，你可以在 run 目录里看到：

```text
selected_clips.json
```

一个合法示例：

```json
[
  {
    "clip_id": "w0001-c001",
    "source_start": 12.5,
    "source_end": 58.0,
    "title": "这里写中文标题",
    "remove_ranges": [[25.0, 28.0]]
  }
]
```

字段解释：

- `clip_id`：候选片段 id，必须来自候选包，不能乱编。
- `source_start`：在原始视频里的开始秒数。
- `source_end`：在原始视频里的结束秒数。
- `title`：导出片段的标题。
- `remove_ranges`：可选，用来剪掉片段中间的废话或等待。

`remove_ranges` 的规则：

- 必须在 `source_start` 和 `source_end` 之间。
- 不能互相重叠。
- 不能把整个片段删空。
- 不需要时可以写成空数组 `[]`，也可以省略。

## 第 11 步：渲染成片

如果配置里是：

```toml
[service]
auto_render_after_selection = true
```

那么 AI 写入 `selected_clips.json` 后，服务下一轮 reconcile 会自动渲染。

你也可以手动渲染。

方式一：Web 控制台

1. 打开 `任务` 页。
2. 选择对应任务。
3. 点击 `渲染`。

方式二：终端命令

```bash
.venv/bin/live-clipper render output/<run_id>/selected_clips.json
```

把 `<run_id>` 换成实际目录名。

渲染完成后，成片通常在：

```text
output/<run_id>/clips/
```

## 第 12 步：查看成片和日志

在 Web 控制台：

1. 打开 `任务` 页。
2. 选择对应任务。
3. 看详情里的文件状态、成片数和日志。

在终端：

```bash
.venv/bin/live-clipper service status --json
.venv/bin/live-clipper service logs
```

如果你知道 run 目录，也可以直接查看：

```bash
ls output/<run_id>/clips
```

## 第 13 步：删除和清理机制

当前版本的删除机制是安全优先：

- AI/MCP 不会直接删除文件。
- Web 旧删除入口对 Service Core run 也会创建确认请求。
- 删除请求会进入 `work/service/confirmations.json`。
- 你需要在 Web 控制台 `确认` 页里确认或拒绝。
- 批量确认/拒绝已经支持。

可以请求确认的动作包括：

- 删除某个已渲染 clip。
- 执行 cleanup。
- 删除本地 `input/` 源文件副本。

这些动作不会直接删除 NAS 原始录播。

如果你只是想看哪些文件可以清理，先点 `预览清理`，或者运行：

```bash
.venv/bin/live-clipper cleanup output/<run_id>
```

只有你确认预览结果没问题，才执行真正清理：

```bash
.venv/bin/live-clipper cleanup output/<run_id> --confirm
```

## 第 14 步：定时任务推荐

建议拆成两个定时任务。

### 定时任务 1：周日 00:00，拉取录播并处理到待审阅

你之前的需求是：

周日零点，从 NAS 的 `/Volumes/your-nas/recordings` 读取录播，拷贝到项目库，然后执行切片工作，直到待审阅阶段。这个阶段只需要触发开始，不需要 AI 一直盯着进度。

如果使用当前服务能力，推荐让服务常驻运行，然后定时任务只负责启动或扫描：

```bash
cd /path/to/live-clipper
.venv/bin/live-clipper service start
.venv/bin/live-clipper service start --once
```

如果使用 automation helper，也可以用：

```bash
cd /path/to/live-clipper
.venv/bin/live-clipper automation start-latest \
  --source-dir /Volumes/your-nas/recordings \
  --input-dir input \
  --output-root output \
  --since-hours 168 \
  --min-age-minutes 10 \
  --top-n 25
```

### 定时任务 2：周日 12:00，让 AI 审阅并渲染

中午 12 点，让 AI 执行：

```text
请进入 /path/to/live-clipper。

请检查 live-clipper 是否有待审阅任务。
优先通过 MCP 工具或本地文件读取审阅包。

如果有待审阅任务：
1. 读取 codex_brief.json、codex_review.md 和 refined_candidates.json。
2. 选择适合发布的片段。
3. 写入 selected_clips.json。
4. 触发渲染，或确认服务会自动渲染。
5. 输出本次处理的 run id、选中片段数量、成片目录。

如果没有待审阅任务：
- 查看服务状态和日志。
- 告诉我当前没有可选片任务，以及原因。

不要删除 NAS 原始录播。
不要直接删除本地 input 源文件。
需要删除或清理时，只创建确认请求或让我在 Web 控制台确认。
```

## 第 15 步：完整的一次使用流程

下面是一条最常见的完整路径。

### 1. 启动 Web 控制台

```bash
cd /path/to/live-clipper
.venv/bin/live-clipper web
```

打开：

```text
http://127.0.0.1:8765
```

### 2. 启动服务

```bash
.venv/bin/live-clipper service start
```

### 3. 触发扫描

Web 控制台点击 `立即扫描`，或者终端运行：

```bash
.venv/bin/live-clipper service start --once
```

### 4. 等任务进入待审阅

在 Web 控制台 `任务` 页看状态。

看到 `待审阅` 后，就可以叫 AI 选片。

### 5. 叫 AI 选片

直接把这段发给 AI：

```text
请帮我处理 live-clipper 的待审阅任务。

项目目录是 /path/to/live-clipper。
请先查看服务状态和待审阅任务。
如果有待审阅任务，请读取 run 目录里的 codex_brief.json、codex_review.md、refined_candidates.json。
请挑选适合发布的短视频片段，并写入 selected_clips.json。
请不要编造 clip_id。
写完后请触发渲染或确认服务会自动渲染。
最后用中文告诉我处理结果。
```

### 6. 等渲染完成

Web 控制台看 `任务` 页。

如果状态变成 `已成片`，去这里找视频：

```text
output/<run_id>/clips/
```

### 7. 清理

先预览：

```bash
.venv/bin/live-clipper cleanup output/<run_id>
```

确认没问题后再清理：

```bash
.venv/bin/live-clipper cleanup output/<run_id> --confirm
```

或者让 AI 创建清理确认请求，你在 Web 控制台 `确认` 页批量确认。

## 第 16 步：排错

### Web 控制台打不开

先确认命令还在运行：

```bash
.venv/bin/live-clipper web
```

如果端口占用：

```bash
.venv/bin/live-clipper web --port 8766
```

然后打开：

```text
http://127.0.0.1:8766
```

### 服务启动不了

查看：

```bash
.venv/bin/live-clipper service status --json
.venv/bin/live-clipper service logs
```

常见原因：

- `live-clipper.toml` 配置错误。
- NAS 目录没有挂载。
- ASR 模型第一次下载失败。
- LLM API key 没有配置。
- `ffmpeg` 不可用。

### 没有发现录播

检查：

```bash
ls /Volumes/your-nas/recordings
```

再检查配置：

```toml
[recording_source.default]
source_dir = "/Volumes/your-nas/recordings"
since_hours = 168
min_age_minutes = 10
```

如果录播刚刚结束，可能还没超过 `min_age_minutes`，等一会儿再扫描。

### 一直没有进入待审阅

看日志：

```bash
.venv/bin/live-clipper service logs
```

也可以看 run 日志。Web 控制台 `任务` 页选择任务后，`日志` 页会显示任务日志尾部。

可能原因：

- ASR 失败。
- LLM API 请求失败。
- 网络异常。
- 视频文件损坏。
- 本机磁盘空间不够。

### AI 写了 selected_clips.json 但不能渲染

通常是选择文件不合法。

检查：

- `clip_id` 是否来自候选包。
- `source_start` 和 `source_end` 是否在候选范围内。
- `remove_ranges` 是否越界或重叠。
- JSON 是否格式正确。

如果通过 MCP 工具 `write_selected_clips` 写入，它会自动验证，能减少这类问题。

### 渲染完成但看不到视频

检查：

```bash
ls output/<run_id>/clips
```

如果没有文件，看日志：

```bash
.venv/bin/live-clipper service logs
```

也可以手动渲染一次：

```bash
.venv/bin/live-clipper render output/<run_id>/selected_clips.json
```

## 第 17 步：给 AI 的标准选片提示词

你可以把下面这段保存起来，每次有待审阅任务时直接发给 AI：

```text
你是 live-clipper 的短视频选片助手。

项目目录：
/path/to/live-clipper

目标：
从当前待审阅任务中选择适合发布的直播切片，并生成 selected_clips.json。

请按顺序执行：
1. 进入项目目录。
2. 查看服务状态和待审阅任务。
3. 找到 phase 为 needs_review 或页面显示为待审阅的 run。
4. 读取该 run 目录下的 codex_brief.json。
5. 读取 codex_review.md。
6. 如果 refined_candidates.json 存在，也一起读取。
7. 从候选中选择最适合发布的片段。
8. 写入 selected_clips.json。
9. 验证 selected_clips.json 合法。
10. 触发渲染，或确认 auto_render_after_selection 会自动渲染。

选片标准：
- 片段开头要快，最好 3 秒内进入重点。
- 单独观看也要能懂，不依赖直播前后文。
- 优先选择有观点、有结论、有冲突、有方法、有情绪价值的片段。
- 避免只是过渡、寒暄、铺垫、重复、等待、沉默。
- 标题用中文，简短具体。
- 不要编造 clip_id。
- 不要选择候选包之外的片段。
- 如果一段里只有几秒废话，可以用 remove_ranges 去掉。

安全要求：
- 不要删除 NAS 原始录播。
- 不要直接删除 input 源文件。
- 不要执行 cleanup --confirm，除非我明确确认。
- 删除或清理请走 Web 控制台确认队列。

完成后请用中文汇报：
- run id。
- 选择了几个片段。
- 每个片段的标题和时间范围。
- selected_clips.json 路径。
- 渲染是否开始或完成。
- 成片目录在哪里。
```

## 最后记住

最小闭环只有四步：

1. 启动服务：`.venv/bin/live-clipper service start`
2. 等任务变成 `待审阅`
3. 让 AI 写 `selected_clips.json`
4. 等服务自动渲染，或手动点 `渲染`

真正需要你人工确认的，主要是删除和清理。
