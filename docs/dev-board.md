# 开发看板（多车道并行）

机制：一条车道 = 一份 Spec + 一个 git worktree + 一个 lane 分支 + 一个 Codex 开发会话 + 一个对应测试会话。
文件集合不相交的 Spec 才允许并行（以 Spec 头部「并行安全声明」为准）；测试会话只验收出报告；合并统一由合并 Agent（高思考度会话）按依赖顺序执行，合入后删 worktree 和分支并更新本看板。协作总纲见项目根 `CLAUDE.local.md`。

## 车道开工模板

项目根：`/Users/gouzi/dingshuxinRepo/live-clipper/`（主仓 `venus-master/` 跟踪 GitHub 远端；`lanes/` 放车道 worktree；`specs/` 放设计文档；`assets/` 放非代码素材）。

```bash
# 1. 建车道（<lane> 换成车道名）
git -C /Users/gouzi/dingshuxinRepo/live-clipper/venus-master worktree add ../lanes/<lane> -b lane/<lane> master
# 2. 车道内建独立环境（必须，否则测试会测到主仓代码；不装 mlx 是刻意的）
cd /Users/gouzi/dingshuxinRepo/live-clipper/lanes/<lane> && python3.11 -m venv .venv && .venv/bin/pip install -e . pytest ruff
# 3. 在该目录开 Codex 会话，丢对应 Spec（项目根 specs/ 目录下）
```

## 当前车道

| 车道 | Spec | 文件集合 | 依赖 | 状态 |
|---|---|---|---|---|

## 待派发队列

| 顺序 | 车道 | Spec | 依赖 | 状态 |
|---:|---|---|---|---|
| 1 | 0.3.1-local-mlx-release-operation · 阶段 B | `specs/2026-07-26-0.3.1-local-mlx-release-operation.md` | 版本与发布文档冻结已合入 | 等待用户授权阶段 B：本地 MLX candidate 构建 |

## 当前发布操作

- **v9f2-local-async-notarization**：已完成。从不可变 `v0.3.0` 标签在本机完成正式构建、Developer ID 签名、异步公证、票据装订、制包和 GitHub Release 发布；原始 Apple 提交包与发布证据保存在 `release-work/v0.3.0/`，等待 v0.3.1 自动更新真机演练完成后再申请清理。

## 产品版本路线

- **0.3.1 · 本地模型与首次使用闭环**：Qwen3 不进入本版本；下载基础设施、MiSans 默认字体、macOS 菜单栏图标修复、Whisper 三档/当前模型闭环、首启向导及版本与发布文档冻结均已合入，tag 自动发布工作流已停用；当前等待用户授权阶段 B：本地 MLX candidate 构建，尚未完成真实中文转写、Apple 公证或正式发布。
- **0.3.2 · UI 组件系统统一**：盘点并统一输入框、选择框、按钮、卡片、状态、弹层和表单结构；保持原生 HTML/JS 技术栈，不在本版本迁移 React/Vue。
- **0.4.0 · Project 工作台**：原 V11 方向；原 V10c“切片偏好 + 提示词编辑器”并入 Project、场景模板和关注点预设。

版本与 Spec 统一规则见项目根：
`specs/plans/2026-07-24-product-version-roadmap.md`。

## 已合入

- 0.3.1-release-metadata-freeze（冻结 0.3.1 版本与发布文档；lane `206fb5171c1e5191fd25be96d095f9c24e6fb257`；merge `5e8584cddecd227998ac0cd7ad7a60e87e9d90c1`；定向 31 passed；全量 387 passed；Ruff、四项 Node 语法、npm ci、七文件边界及 lockfile 依赖图一致性通过）
- 0.3.1-disable-tag-release-workflow（停用 tag 自动发布工作流；lane `786edf083783db80ad8d149a0a58deb7685ee7f6`；merge `53b778407f23f349f8562a4a24e85ad5a8c23897`；定向 5 passed；全量 386 passed；发布能力与副作用审计零命中）
- 0.3.1-onboarding-local-asr（首次启动本地/云端语音识别闭环；lane `1df20b8041992c50f8d792a23118d588835afa10`；merge `ba5a272df406acdc491f77a465d9448fe843a0fb`；定向 70 passed；全量 385 passed；Ruff、Node 语法、禁用词与六文件边界通过）
- 0.3.1-model-matrix-selection（Whisper 三档模型与当前模型闭环；lane `45fd9d814ae904427b8bd59bf0a469c9e5eb484a`；merge `42214c87745e9dfdce39eac7f837b6517eb9362f`；定向 50 passed；全量 367 passed；Ruff、Node 语法、禁用词与九文件边界通过）
- 0.3.1-macos-tray-icon-alpha（macOS 菜单栏图标透明蒙版修复，89317ce4fa09c9a49b3738d313782534b41d973a）
- 0.3.1-misans-default-font（MiSans 默认界面字体与打包接线，62fc0ebff0f5c27cd69205f52f76a7a90166133d）
- 0.3.1-model-download-foundation（ModelScope / Hugging Face 两源模型下载基础设施，2d1b2c2b5b8d8f12847f60311ffcd5cf4d8b93ba）
- readme-product-home-v2-1（README 产品首页 V2.1，3018633df792f7445a1200277e0207583e9e3634）
- readme-product-home（README 产品首页 V2，d7c715767bbcfdde4178bb9625cd7d7f18bfc825）
- v10a-local-asr-models（本地 ASR 模型管理器，20a0033894e1e95b3f1a3dd3e331cea23fac6485）
- v9f1-notary-recovery（Apple 公证恢复工作流，005fa90；因 GitHub 托管 job 的 6 小时硬上限，v0.3.0 实际恢复改走本地 V9f.2 操作）
- v9f-release（签名公证发版流水线 + 包内自动更新，d62accf）
- v10ui-settings（设置页信息架构简化，1fc1623 + c001c6b，看板制之前在主仓完成）
