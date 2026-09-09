# 项目与审阅工作流

桌面端从项目发起处理：明确分配资源，手动或定时发现录像，再转写、分析、审阅和渲染。处理记录在入队时冻结资源修订与参数。换资源只影响新记录。

源码调用 `scan`、`pipeline`、`refine` 时，也必须通过 `--project-run` 和 `--service-dir` 指定已入队或正在处理的项目记录。子进程只接收记录身份，凭据由本机数据库对应的资源绑定解析。缺失身份的命令会在媒体处理前失败。

`brief` 生成 `review_brief.json`、`review_notes.md` 和 `selected_clips.template.json`。通用外部 Agent 或人工可阅读材料，写入 `selected_clips.json` 后交给 `render` 校验并渲染。旧命名的材料仍可读取，新记录只生成中性文件名。

`automation check` 可生成 `review_task.md`。返回字段 `requires_review`、`review_tasks` 和 `review_task_file` 描述需要当前 Agent 判断的工作。阶段为 `needs_review_selection` 时先审阅候选；`failed_needs_review` 时先诊断失败。清理先预演，原录像不删除。

配置方法见[资源与配置](configuration.md)，命令边界见[高级使用](advanced-usage.md)。
