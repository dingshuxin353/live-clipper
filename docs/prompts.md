# 提示词自定义

这是源码与 CLI 的高级配置。Venus 1.0.0 桌面主流程不要求手工导出或编辑提示词。

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
