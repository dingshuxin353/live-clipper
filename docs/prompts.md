# 提示词自定义

导出默认提示词：

```bash
.venv/bin/live-clipper prompts export --output prompts.local
```

编辑 `prompts.local/*.md` 后，可以通过配置文件启用：

```toml
[prompts]
directory = "prompts.local"
```

也可以对单次命令使用：

```bash
.venv/bin/live-clipper scan input/example.mp4 --prompt-dir prompts.local
```

如果没有配置 `prompt_dir`，程序会使用包内默认提示词。
