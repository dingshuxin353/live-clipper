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
| v9f-release | specs/2026-07-17-v9f-release-pipeline.md | desktop/**、.github/workflows/、pyproject.toml(version)、tests/test_project_metadata.py | 无 | Spec 就绪，待指派 |
| v10a-volcano-asr | specs/2026-07-22-v10a-volcano-asr.md | src/live_clipper/{volcano_asr(新),transcribe,config}.py、tests/test_volcano_asr.py(新) | 无 | Spec 就绪，待指派 |

## 已合入

- v10ui-settings（设置页信息架构简化，1fc1623 + c001c6b，看板制之前在主仓完成）
