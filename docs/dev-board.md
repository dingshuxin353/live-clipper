# 开发看板（多车道并行）

机制：一条车道 = 一份 Spec + 一个 git worktree + 一个 lane 分支 + 一个 Codex 会话。
文件集合不相交的 Spec 才允许并行（由 Claude 写 Spec 时保证）；验收在车道分支上做；合并回 master 由 Claude 按依赖顺序执行；合入后立即删 worktree 和分支。

## 车道开工模板

```bash
# 1. 建车道（在主仓执行；<lane> 换成车道名）
git -C /Users/gouzi/dingshuxinRepo/live-clipper worktree add ../venus-lanes/<lane> -b lane/<lane> master
# 2. 车道内建独立环境（必须，否则测试会测到主仓代码）
cd /Users/gouzi/dingshuxinRepo/venus-lanes/<lane> && python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev,mlx]"
# 3. 在该目录开 Codex 会话，丢对应 Spec
```

## 当前车道

| 车道 | Spec | 文件集合 | 依赖 | 状态 |
|---|---|---|---|---|
| v9f-release | specs/2026-07-17-v9f-release-pipeline.md | desktop/**、.github/workflows/、pyproject.toml(version)、tests/test_project_metadata.py | 无 | 待开工 |
| v10ui-settings | specs/2026-07-17-v10ui-settings-simplification.md | web_static/{index.html,styles.css}、tests/test_docs.py、README.md(设置段落) | 无 | 开发中（Codex 已在主仓进行，本单收尾后再迁看板制） |
| v10a-asr | （Spec 待 Claude 输出） | src/live_clipper/{transcribe,config,onboarding,web}.py 等后端 | 无 | 等 Spec |

## 已合入

（暂无）
