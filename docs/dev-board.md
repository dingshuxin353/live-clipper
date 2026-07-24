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
| v10a-local-asr-models | `specs/2026-07-23-v10a-local-asr-models.md` | ASR 模型管理器、设置 UI、相关测试与发版依赖 | v9f-release | 实施完成（a136c35），待独立验收与合入 |
| readme-product-home | `specs/2026-07-24-readme-product-home-v2.md` | README、中英文/高级文档、3 张真实截图、文档测试 | V10a 先合入；不得并行 | V2 规划完成，等待前置 |

## 当前发布操作

- **v9f2-local-async-notarization**：已完成。从不可变 `v0.3.0` 标签在本机完成正式构建、Developer ID 签名、异步公证、票据装订、制包和 GitHub Release 发布；原始 Apple 提交包与发布证据保存在 `release-work/v0.3.0/`，等待 v0.3.1 自动更新真机演练完成后再申请清理。

## 已合入

- v9f1-notary-recovery（Apple 公证恢复工作流，005fa90；因 GitHub 托管 job 的 6 小时硬上限，v0.3.0 实际恢复改走本地 V9f.2 操作）
- v9f-release（签名公证发版流水线 + 包内自动更新，d62accf）
- v10ui-settings（设置页信息架构简化，1fc1623 + c001c6b，看板制之前在主仓完成）
