# 源码与命令行兼容工作流

本文记录高级 CLI 路径，不是 Venus 1.0.0 桌面客户端主流程。桌面端会按项目自动完成转写、分析、AI 审阅和渲染。

核心流程：

1. 准备长视频。
2. `doctor` 检查环境。
3. `smoke` 做本地烟测。
4. `scan` 生成转录、窗口和候选。
5. `brief` 生成审阅包。
6. Codex 或人工写入 `selected_clips.json`。
7. `render` 渲染成片。
8. `cleanup` 预演并清理本地中间大文件。

Codex 的介入点是 `codex_brief.json` 已存在但 `selected_clips.json` 不存在时。定时任务可通过 `automation check` 发现这个状态并生成 `codex_task.md`。
