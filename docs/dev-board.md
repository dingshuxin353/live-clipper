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

## 待派发队列

| 顺序 | 车道 | Spec | 依赖 | 状态 |
|---:|---|---|---|---|
| 1 | 0.3.1-local-mlx-release-operation · Provider 修复后重新制包 | `specs/2026-07-26-0.3.1-local-mlx-release-operation.md` | `0.3.1-updater-provider-config` 已合入；现有 candidate、finalize App 与 release assets 均已 superseded | 等待规划者读取本次合并与看板提交后的精确 master HEAD，修订发布 Spec 后另行派发 |
| 2 | 0.3.2-release-metadata-freeze | 待规划任务输出 | `0.3.2-astryx-stone-ui-system` 已验收并合入 | 待规划；未启动。等待规划任务读取本次合并与看板提交后的最终 master HEAD，输出精确基线 Spec |

## 当前发布操作

- **v9f2-local-async-notarization**：已完成。从不可变 `v0.3.0` 标签在本机完成正式构建、Developer ID 签名、异步公证、票据装订、制包和 GitHub Release 发布；原始 Apple 提交包与发布证据保存在 `release-work/v0.3.0/`，等待 v0.3.1 自动更新真机演练完成后再申请清理。
- **0.3.1 本地正式资产（SUPERSEDED）**：基于旧 release source `7a2e4291e59105e9d66eca7deefbdeba0b8d2500`、`0782bf74cbb42a606e054c3e485cca04744b0012` 的 candidate，以及基于 `327a8de61386496c588b9e50f451ee9ad985c053` 已完成签名、公证和制包但缺少 `app-update.yml` 的 candidate、finalize App 与四类 release assets，均只保留为历史证据，禁止发布或原地修改；Updater Provider 配置源修复已合入，下一步等待规划者从最终 master HEAD 重新冻结 release source 并派发差量重建。

## 产品版本路线

- **0.3.1 · 本地模型与首次使用闭环**：Qwen3 不进入本版本；下载基础设施、MiSans 默认字体、macOS 菜单栏图标修复、Whisper 三档/当前模型闭环、首启向导、统一逐次运行工作区、ASR 静音幻觉与模型列表 UI 修复、首次引导与 Electron 壳层体验修复、版本与发布文档冻结，以及自动更新 Provider 配置源修复均已合入，tag 自动发布工作流已停用；现有 0.3.1 candidate、finalize App 与 release assets 均已 superseded，下一步等待从最终 master HEAD 重新冻结 release source、差量重建与验收，尚未完成新的 Apple 公证或正式发布。
- **0.3.2 · UI 组件系统统一**：React + TypeScript + Vite renderer 等价迁移与 Astryx Stone UI 系统均已验收并合入；下一步仅为 `0.3.2-release-metadata-freeze` 待规划、未启动。
- **0.4.0 · Project 工作台**：原 V11 方向；原 V10c“切片偏好 + 提示词编辑器”并入 Project、场景模板和关注点预设。

版本与 Spec 统一规则见项目根：
`specs/plans/2026-07-24-product-version-roadmap.md`。

## 已合入

- 0.3.2-astryx-stone-ui-system（Astryx Stone UI 系统迁移；lane `4c45517835be9c2101b5ae20eb30cc6e8c5128aa`；merge `fcb33c352f31d785e8ec62a903e18367b1b0831f`；v1.15 测试合同同步 `003682f5301d30009f2429ed88c4cd8eb32f4723`；Node 24.14.0 / npm 11.9.0，npm ci 通过；Vitest 全量 50 passed、App 17 passed、App + Onboarding 39 passed；docs pytest 26 passed、静态合同单文件 12 passed、合并后联合定向 pytest 70 passed、全量 pytest 431 passed；TypeScript、theme/build、committed assets 零差异、Ruff、两个 Node 语法及 npm audit 0 vulnerabilities 均通过；下一项 `0.3.2-release-metadata-freeze` 待规划、未启动）
- 0.3.2-react-renderer-migration（React Renderer 等价迁移；lane `f0946c8608ce823f3183c000a90ae5b9e017ff27`；merge `99d453a0a7d45512579feb019f3a46a4ed7605d2`；Vitest 21 passed；定向 pytest 72 passed；全量 pytest 421 passed；TypeScript、Vite build、committed build 一致性、Ruff、两个 Node 语法、37 文件白名单及旧 runtime 禁入门通过；下一步等待规划任务输出组件系统 Spec）
- 0.3.1-updater-provider-config（自动更新 Provider 配置发布阻断修复；lane `4028f49455e5ace7a502136b471c91ceaed721d3`；merge `adcf3906257514a44a5ef900071521a9d0295365`；定向 7 passed；全量 426 passed；Ruff、两个 Node 语法、单提交与三文件边界通过；现有本地正式资产已标记为 superseded，等待从最终 master HEAD 重新冻结 release source）
- 0.3.1-onboarding-electron-ux-fixes（首次引导与 Electron 壳层体验回归修复；lane `ce9dabe64f731788314ac49282e0e052af28c1ef`；merge `dc01e45af1f3951ab6b85c1272cf29bbd089972e`；联合定向 123 passed；全量 424 passed；Ruff、两个 Node 语法、8 文件边界及禁用词/legacy 合同通过）
- 0.3.1-release-blockers-asr-model-ui（ASR 静音幻觉与模型列表 UI 发布阻断修复；lane `36f24053f3142a65c140542a80fd6082fc70d19e`；merge `edad8bca84cbfe52831344ad994706b299c05af3`；联合定向 123 passed；全量 424 passed；Ruff、两个 Node 语法、5 文件边界及未安装 MLX 检查通过）
- 0.3.1-unified-run-workspace（统一逐次运行工作区；lane `8f9c2ab3fadc964f47628c0b532991ec00d9c8b6`；merge `8fa17e6e716311b31680addab449074f153be423`；定向 132 passed；全量 401 passed；Ruff、Node 语法、17 文件边界及未安装 MLX 检查通过）
- 0.3.1-release-metadata-freeze（冻结 0.3.1 版本与发布文档；lane `206fb5171c1e5191fd25be96d095f9c24e6fb257`；merge `5e8584cddecd227998ac0cd7ad7a60e87e9d90c1`；定向 31 passed；全量 387 passed；Ruff、四项 Node 语法、npm ci、七文件边界及 lockfile 依赖图一致性通过）
- 0.3.1-disable-tag-release-workflow（停用 tag 自动发布工作流；lane `786edf083783db80ad8d149a0a58deb7685ee7f6`；merge `53b778407f23f349f8562a4a24e85ad5a8c23897`；定向 5 passed；全量 386 passed；发布能力与副作用审计零命中）
- 0.3.1-onboarding-local-asr（首次启动本地/云端语音识别闭环；lane `1df20b8041992c50f8d792a23118d588835afa10`；merge `ba5a272df406acdc491f77a465d9448fe843a0fb`；定向 70 passed；全量 385 passed；Ruff、Node 语法、禁用词与六文件边界通过）
- 0.3.1-model-matrix-selection（Whisper 三档模型与当前模型闭环；lane `45fd9d814ae904427b8bd59bf0a469c9e5eb484a`；merge `42214c87745e9dfdce39eac7f837b6517eb9362f`；定向 50 passed；全量 367 passed；Ruff、Node 语法、禁用词与九文件边界通过）
- 0.3.1-macos-tray-icon-alpha（macOS 菜单栏图标透明蒙版修复，89317ce4fa09c9a49b3738d313782534b41d973a）
- 0.3.1-misans-default-font（MiSans 默认界面字体与打包接线，62fc0ebff0f5c27cd69205f52f76a7a90166133d）
- 0.3.1-model-download-foundation（ModelScope / Hugging Face 两源模型下载基础设施，2d1b2c2b5b8d8f12847f60311ffcd5cf4d8b93ba）
- readme-product-home-v2-1（README 产品首页 V2.1，3018633df792f7445a1200277e0207583e9e3634）
- readme-product-home（README 产品首页 V2，d7c715767bbcfdde4178bb9625cd7d7f18bfc825）
- v10a-local-asr-models（本地 ASR 模型管理器，20a0033894e1e95b3f1a3dd3e331cea23fac6485）
- v9f1-notary-recovery（Apple 公证恢复工作流，005fa90；因 GitHub 托管 job 的 6 小时硬上限，v0.3.0 实际恢复改走本地 V9f.2 操作）
- v9f-release（签名公证发版流水线 + 包内自动更新，d62accf）
- v10ui-settings（设置页信息架构简化，1fc1623 + c001c6b，看板制之前在主仓完成）
