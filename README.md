<p align="center">
  <img src="desktop/assets/icon.png" width="112" alt="Venus 图标" />
</p>

<h1 align="center">Venus</h1>

<p align="center"><strong>美神直播剪辑工作台</strong></p>

<p align="center">把长直播整理成可查看、可恢复、可继续处理的短视频成片。</p>

<p align="center">面向主播和内容团队的 macOS 客户端：按项目发现录像，自动完成转写、AI 审阅和渲染。</p>

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

## 1.0.0 主流程

Venus 1.0.0 使用项目管理录像、处理记录和成片。一次完整流程是：

```text
首次设置或安全升级
  → 创建 / 进入项目
  → 手动或定时发现录像
  → 自动转写、分析、AI 审阅和渲染
  → 查看成片、AI 判断与发布物料
  → 修复问题或重新处理
```

每个项目都有独立的录像来源、成片目录和处理历史。相同录像按内容识别，重命名或复制不会重复处理；同一录像重新处理时会保留旧版本，方便查看设置差异和结果。

## 从 0.3.x 升级

Venus 首次启动会先检查旧数据，并在迁移前创建备份。升级完成前不会进入可写工作台；迁移失败时会保留旧数据和已完成的备份，供你重试或排查。原录像不会因迁移被删除。

新安装会进入首次设置。你需要选择语音识别方式、配置 AI 服务，并创建第一个项目；这些信息保存完成后才进入工作室。

## 界面预览

![Venus 工作室项目概览](docs/assets/readme/venus-studio.png)

| 项目处理记录 | 成片与 AI 判断 |
| :---: | :---: |
| ![Venus 项目处理记录](docs/assets/readme/venus-project.png) | ![Venus 成片与 AI 判断](docs/assets/readme/venus-results.png) |

截图使用隔离样例数据，不包含真实项目、文件路径或凭据。

## 主要能力

- **项目制工作台**：创建、暂停和恢复项目，查看每条剪辑记录的处理阶段、最近扫描和队列状态。
- **自动处理**：手动扫描或按计划发现稳定录像，随后自动完成转写、分析、AI 审阅、字幕和成片渲染。
- **结果与物料**：在成片页查看视频、AI 判断、标题、简介和其他发布物料。
- **问题恢复**：问题会说明影响和下一步；资源恢复后可继续同一条处理记录。
- **重新处理**：从原录像建立新版本，调整处理设置，并对比不同版本的结果。
- **本地文件边界**：工作副本、中间产物和成片保存在你选择的位置；原录像不因扫描、升级或重新处理被修改。

## 快速开始

1. 从 [GitHub Releases](https://github.com/dingshuxin353/live-clipper/releases/latest) 安装并打开 Venus。
2. 新用户完成首次设置；0.3.x 用户按应用内步骤检查、备份并升级旧数据。
3. 创建或进入项目，确认录像来源和成片目录。
4. 手动扫描录像，或在设置中启用定时扫描。
5. 处理完成后，在“成片”中查看视频、AI 判断和发布物料；遇到问题时按页面提示恢复或重新处理。

本地语音模型提供 Small（约 187 MB）、Medium（约 489 MB）和 Large（约 1.6 GB）三档，可从 ModelScope（中国大陆推荐）或 Hugging Face（国际官方）下载。首次下载需要网络；下载完成后，本地语音识别可以离线运行。AI 审阅是否联网仍取决于所配置的 AI 服务。

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
