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
| 1 | 0.3.1-model-download-foundation | `specs/2026-07-24-0.3.1-model-download-foundation.md` | `master@89d49d1` | 设计完成，待用户确认派发 |
| 2 | 0.3.1-model-matrix-selection | `specs/2026-07-24-0.3.1-model-matrix-selection.md` | 下载基础设施验收并合入后回写精确基线 | 排队，禁止提前开工 |

## 当前发布操作

- **v9f2-local-async-notarization**：已完成。从不可变 `v0.3.0` 标签在本机完成正式构建、Developer ID 签名、异步公证、票据装订、制包和 GitHub Release 发布；原始 Apple 提交包与发布证据保存在 `release-work/v0.3.0/`，等待 v0.3.1 自动更新真机演练完成后再申请清理。

## 产品版本路线

- **0.3.1 · 本地模型与首次使用闭环**：Qwen3 不进入本版本；三下载源/续传/校验/修复与 Whisper 三档/当前模型闭环已拆成两条串行 Spec，待用户确认派发；其后再规划首启向导、tag 自动工作流停用、本地 `.[mlx]` 正式构建、真实中文转写与 `0.3.0 → 0.3.1` 自动更新真机演练。
- **0.3.2 · UI 组件系统统一**：盘点并统一输入框、选择框、按钮、卡片、状态、弹层和表单结构；保持原生 HTML/JS 技术栈，不在本版本迁移 React/Vue。
- **0.4.0 · Project 工作台**：原 V11 方向；原 V10c“切片偏好 + 提示词编辑器”并入 Project、场景模板和关注点预设。

版本与 Spec 统一规则见项目根：
`specs/plans/2026-07-24-product-version-roadmap.md`。

## 已合入

- readme-product-home-v2-1（README 产品首页 V2.1，3018633df792f7445a1200277e0207583e9e3634）
- readme-product-home（README 产品首页 V2，d7c715767bbcfdde4178bb9625cd7d7f18bfc825）
- v10a-local-asr-models（本地 ASR 模型管理器，20a0033894e1e95b3f1a3dd3e331cea23fac6485）
- v9f1-notary-recovery（Apple 公证恢复工作流，005fa90；因 GitHub 托管 job 的 6 小时硬上限，v0.3.0 实际恢复改走本地 V9f.2 操作）
- v9f-release（签名公证发版流水线 + 包内自动更新，d62accf）
- v10ui-settings（设置页信息架构简化，1fc1623 + c001c6b，看板制之前在主仓完成）
