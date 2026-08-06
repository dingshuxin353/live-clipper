<p align="center">
  <img src="desktop/assets/icon.png" width="112" alt="Venus 图标" />
</p>

<h1 align="center">Venus</h1>

<p align="center"><strong>美神直播剪辑工作台</strong></p>

<p align="center">把一场长直播，让 AI 自动帮你剪辑成可发布的短视频。</p>

<p align="center">面向主播和内容团队的 macOS 客户端：自动发现录播、AI 选片、AI剪辑并渲染成片。</p>

<p align="center">
  <a href="https://github.com/dingshuxin353/live-clipper/releases/latest"><strong>下载最新版</strong></a>
  ·
  <a href="#快速开始">快速开始</a>
  ·
  <a href="docs/README.en.md">English</a>
</p>

<p align="center">
  <a href="https://github.com/dingshuxin353/live-clipper/releases/latest"><img alt="Latest Release" src="https://img.shields.io/github/v/release/dingshuxin353/live-clipper?label=Latest"></a>
  <img alt="macOS 14+" src="https://img.shields.io/badge/macOS-14%2B-111111">
  <img alt="Apple Silicon" src="https://img.shields.io/badge/Apple-Silicon-111111">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-2f855a"></a>
</p>

## 为什么选择 Venus

人工回看数小时直播很耗时；找到片段后，还要分别处理转录、字幕和导出。录播可能散落在本机或 NAS，AI 服务与成片目录也各自独立，而删除和清理更需要清楚的安全边界。Venus 把这些工作收进一个可见、可控的客户端。

- **从长直播到短视频**：在一个工作台里完成发现录播、转写、选片、字幕和渲染。
- **先审阅，再发布**：AI 给出候选，你可以查看处理状态、审阅结果和最终成片。
- **本地模型可选**：直接在设置页下载本地语音模型，下载后可离线完成转写。
- **适合持续录播**：按计划扫描本机或 NAS 录播目录，不必每次手动寻找新文件。
- **本地文件优先**：原视频、中间产物和成片默认保存在你选择的本地目录。

## 界面预览

在“切片结果”中集中查看处理中、待审阅、已成片和失败任务。

0.3.3 提升了录播处理可靠性：缺少 AI Key 时会在创建任务前明确阻断，失败任务可人工重试；稳定录像不再受日期范围限制，会按完整内容身份去重并进入单并发队列，扫描反馈会区分发现、排队、重复、过新和写入中状态。配置体检卡也使用 Stone 语义颜色区分正常、待配置和中性状态。

![Venus 切片结果界面](docs/assets/readme/venus-overview.png)

| 自动化中心 | 本地语音模型 |
| :---: | :---: |
| ![Venus 自动化中心](docs/assets/readme/venus-automation.png) | ![Venus 本地语音模型管理](docs/assets/readme/venus-local-models.png) |

## 一场直播如何变成短视频

### 1. 发现录播

选择本机或 NAS 上的录播文件夹。Venus 可以手动扫描，也可以按计划寻找稳定的新录播。

### 2. 语音转写

使用已下载的本地模型，或你配置的识别服务，把音频变成带时间戳的文字。

### 3. AI 选片

根据转录内容找出值得发布的片段。你可以选择本机 Agent，也可以配置兼容的模型服务。

### 4. 生成成片

渲染字幕和短视频，在客户端里查看结果，再决定如何发布。

> 本地语音转写不等于 AI 选片也完全离线。AI 审阅是否联网，取决于你选择的 Agent 或模型服务。

## 功能特性

### 切片与审阅

- 查看处理中、待审阅、已成片和失败任务；
- 对待审阅任务启动 AI 审阅，并查看最近结果或失败原因；
- 查看最终生成的短视频、字幕和任务详情；
- 在人工确认后继续渲染或清理。

### 自动化处理

- 按计划扫描新的本机或 NAS 录播；
- 查看自动化引擎、定时任务、自动审阅状态和运行日志；
- 创建、暂停、启用或立即执行定时任务；
- AI 审阅不会静默开启，必须由你明确配置。

### 本地模型与 AI 服务

- 在客户端内下载、查看和删除本地语音模型；
- 可选择 ModelScope（中国大陆推荐）或 Hugging Face（国际官方）；
- 分别配置语音识别与 AI 审阅服务；
- 本地转写可以离线运行，但不代表所有 AI 功能都离线。

### 设置与安全

- 配置录播目录、成片目录、模型服务和自动化；
- 普通设置与高级设置分层，常用选项保持清晰；
- 删除和清理操作进入确认流程；
- 高风险操作受确认机制和路径边界保护。

## 快速开始

1. 从 [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases/latest) 安装并打开 Venus。
2. 在首次向导或设置页选择录播文件夹和成片目录。
3. 选择并下载本地语音模型，或配置云端语音识别服务。
4. 选择并配置 AI 审阅方式。
5. 返回“切片结果”，点击“立即扫描录播”。

本地语音模型提供 Small（约 187 MB）、Medium（约 489 MB）和 Large（约 1.6 GB）三档。首次向导初始选择 Small 是为了降低首次下载成本，不代表质量推荐。首次下载需要网络；下载完成后，可以离线完成语音转写，AI 选片是否联网仍取决于你选择的审阅方式。

## 常见问题

<details>
<summary><strong>Venus 支持哪些系统？</strong></summary>

当前正式客户端面向 Apple Silicon Mac，要求 macOS 14 或更高版本。暂不承诺 Intel Mac、Windows 或 Linux 客户端。
</details>

<details>
<summary><strong>Venus 是否完全离线？</strong></summary>

取决于配置。本地语音模型下载完成后可以离线转写；如果 AI 审阅选择云端模型服务，审阅仍需要网络。
</details>

<details>
<summary><strong>原始视频会上传到云端吗？</strong></summary>

本地 ASR 不会向云端 ASR 发送音频。选择云端 ASR 时，音频会发送给你配置的识别服务；AI 审阅则可能发送完成判断所需的转录文本。
</details>

<details>
<summary><strong>本地模型多大，首次需要多久？</strong></summary>

Small 约 187 MB，Medium 约 489 MB，Large 约 1.6 GB。首次下载时间取决于网络和所选下载源，客户端会显示实际下载进度；首次向导初始选择 Small 只是为了降低首次下载成本，不代表质量推荐。
</details>

<details>
<summary><strong>可以处理 NAS 上的录播吗？</strong></summary>

可以。选择已挂载到 macOS 的 NAS 录播目录即可；Venus 会把工作副本和成片放到你配置的位置，不直接删除 NAS 原始录播。
</details>

<details>
<summary><strong>AI 选片需要什么服务？</strong></summary>

你可以使用本机的 Codex CLI、Claude Code，或配置 OpenAI-compatible 模型服务。可用性、联网行为和费用取决于你的选择。
</details>

<details>
<summary><strong>删除操作会不会误删原始视频？</strong></summary>

删除与清理意图先进入确认流程，并受路径边界保护。Web 界面不会直接删除 NAS 原始录播，清理默认先给出预演结果。
</details>

<details>
<summary><strong>如何更新到新版本？</strong></summary>

Venus 支持应用内自动更新。你也可以随时到 [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases/latest) 下载最新安装包和查看历史版本。
</details>

## 下载安装

1. 打开 [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases/latest)。
2. 下载最新的 arm64 `.dmg`。
3. 打开 DMG，把 Venus 拖入“应用程序”。
4. 启动 Venus，按首次向导完成设置。

正式安装包经过 Apple Developer ID 筗名与公证，并支持应用内自动更新。系统要求是 Apple Silicon Mac 与 macOS 14 或更高版本；当前不承诺 Intel Mac、Windows 或 Linux 客户端。

版本变化与历史安装包统一见 [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases)。

需要 CLI、源码安装、MCP 或自动化高级配置，请阅读 [高级使用](docs/advanced-usage.md)。

## 本地处理与隐私

- 原始视频、中间文件、转录和成片默认保存在本机；
- 本地 ASR 不向云端 ASR 发送音频；
- 云端 ASR 会向你配置的服务发送音频；
- 云端 AI 审阅会发送完成判断所需的转录文本；
- 日志默认隐藏模型请求正文，失败信息按脱敏策略记录；
- 具体行为取决于你的配置，完整说明见 [隐私说明](docs/privacy.md)。

## 开发者部署

普通用户建议直接下载经过签名和公证的 DMG；以下内容面向希望从源码运行、调试或自行构建 Venus 的开发者。

<details>
<summary><strong>从源码运行与本地构建</strong></summary>

### 环境要求

- Apple Silicon Mac；
- macOS 14 或更高版本；
- Python 3.11；
- Node.js 24；
- Git；
- 可从终端调用的 `ffmpeg`。

### 获取源码并安装依赖

```bash
git clone https://github.com/dingshuxin353/live-clipper.git
cd live-clipper
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,mlx]'
cd desktop
npm ci
```

### 运行桌面开发版

```bash
npm start
```

Electron 开发模式会调用仓库根目录的 `.venv/bin/live-clipper` 启动本地后端。

### 验证与本地构建

```bash
cd ..
.venv/bin/python -m pytest
cd desktop
npm run dist
```

`npm run dist` 用于在本机构建 macOS 安装包。自行构建不等于 Venus 官方 Release，也不自动保证 Developer ID 签名或 Apple 公证。

完整 CLI、配置、Web 控制台、MCP 与自动化说明见 [高级使用](docs/advanced-usage.md)；参与贡献前请阅读 [贡献指南](CONTRIBUTING.md)。

</details>

## 文档、贡献与许可证

- [高级使用](docs/advanced-usage.md) · [配置说明](docs/configuration.md) · [故障排查](docs/troubleshooting.md)
- [隐私说明](docs/privacy.md) · [MCP 工作台](docs/mcp-workbench-user-guide.md)
- [GitHub Issues](https://github.com/dingshuxin353/live-clipper/issues) · [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases)
- [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [MIT License](LICENSE)

Venus 是开源项目，客户端由 Electron 与本地 Python 服务组成。欢迎通过 Issues 报告问题，并按贡献指南提交改进；架构、源码安装和开发入口集中在高级使用文档。
