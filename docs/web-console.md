# Web 控制台

启动：

```bash
.venv/bin/live-clipper web
```

默认只监听 `127.0.0.1:8765`。如果需要局域网访问，显式运行：

```bash
.venv/bin/live-clipper web --host 0.0.0.0
```

不要把 Web 控制台暴露到公网。控制台包含渲染、清理和删除本地文件的操作。
