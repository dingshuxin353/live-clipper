# Venus 高级使用

本文面向当前开发分支的源码、CLI 和 MCP 用户。模型配置与执行边界见[资源与配置](configuration.md)；真实供应商模型和完整短片仍需验收。

## 从源码启动

桌面开发环境使用 Apple Silicon、macOS 14 或更高版本、Python 3.11、Node.js 24、ffmpeg 和 ffprobe。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,mlx]'
cd desktop
npm ci
npm start
```

开发和测试应使用隔离的应用数据目录，避免读取已安装应用的真实配置。`LIVE_CLIPPER_HOME` 指定应用目录，`live-clipper app` 启动本机后端和首次设置。资源凭据由应用保存，不通过命令行参数传递。

```bash
.venv/bin/live-clipper --help
.venv/bin/live-clipper doctor
.venv/bin/live-clipper smoke --output-dir work/smoke
.venv/bin/live-clipper guide ai
```

`smoke` 使用合成视频和受控结果，不调用真实远程 ASR 或 LLM。这只检查本地链路，不能作为供应商能力或实际短片验收。

## 资源与项目记录

本机模型在资源页准备，支持 ModelScope 中国大陆推荐或 Hugging Face 国际官方来源，包括 `mlx-community/whisper-large-v3-turbo`。模型完整性通过后还需验证识别用途，已有完整模型可复用。开始处理不会隐式下载。

项目分配语音识别、分析、审阅资源。记录入队时固定资源修订与处理参数；子进程从记录身份解析凭据，不读取全局默认模型来替换原配置。

源码 `scan`、`pipeline`、`refine` 必须携带 `--project-run` 和 `--service-dir`，指向实际入队或正在处理的记录。参数含义可用对应命令的 `--help` 查看；不得伪造记录或修改冻结快照来绕过资源检查。使用项目 UI 发起新处理更便于追踪。

旧 Service 全局扫描和缺少冻结身份的重试不能启动新处理。定时扫描在具体项目内配置。内置审阅支持模型直连和 Claude Code；Codex CLI 内置接入已移除，外部当前 Agent 可继续通过 MCP 或文件材料协作。

## 检查与审阅产物

每条记录保留转录、候选、审阅证据、成片与发布物料。`status` 检查目录中的产物，`next` 和 `automation check` 汇总需要人工或 Agent 判断的工作。

`brief` 从现有候选生成 `review_brief.json`、`review_notes.md` 和 `selected_clips.template.json`。提交的 `selected_clips.json` 先经过候选身份、时间范围及字段校验，再交给 `render`。旧材料名称仍能读取，新记录不再使用专属 Agent 的文件名。

## 故障和清理

问题详情指向失败记录的原资源修订。同一账号、端点、模型、地域与业务空间的凭据可以验证后修复；不同身份须显式新建重跑。检查通过不会自动将其他失败记录批量入队。

清理先 preview。资源默认只删除配置，模型文件需单独勾选且满足独占、无占用和应用管理目录条件。录像原件和成片不属于模型清理范围。模型或凭据清理部分失败会保留结果并允许重试。

[MCP 工作台](mcp-workbench-user-guide.md)介绍外部 Agent 工具边界；[隐私说明](privacy.md)说明媒体和转录的去向。
