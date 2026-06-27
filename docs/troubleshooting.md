# 故障排查

## doctor 失败

先确认 `ffmpeg` 在 `PATH` 中，`input/` 下有支持的视频文件，并且必要的 API key 已配置。

## 模型请求失败

查看 `work/logs/`。默认日志会脱敏；需要完整 payload 时，把 `failure_log_mode` 改为 `full` 后重试。

## 长任务中断

对同一输出目录重新运行 `scan --resume` 或原来的 `pipeline` 命令。

## Web 无法局域网访问

确认启动时使用了 `--host 0.0.0.0`，并检查本机防火墙和同网段访问。
