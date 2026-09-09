# 媒体工具源码与重建

Venus 内置 ffmpeg 与 ffprobe 9.0.1，由 FFmpeg 官方源码构建，
静态编入 x264 和 dav1d。工具通过独立进程运行；Venus 自身的许可证不变。
没有修改上游源码。输入下载地址、版本、提交及 SHA-256 均在 `sources.lock.json`。

- FFmpeg 9.0.1：<https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz>
- x264：<https://code.videolan.org/videolan/x264.git>，提交
  `b35605ace3ddf7c1a5d67a2eb553f034aef41d55`，使用该提交的规范 Git tar 归档。
- dav1d 1.5.4：<https://download.videolan.org/pub/videolan/dav1d/1.5.4/>
- pkgconf 2.5.1（仅构建时）：<https://distfiles.ariadne.space/pkgconf/>

随 App 的 `CORRESPONDING-SOURCE.md` 列出本版本源码包的准确文件名、
SHA-256 和下载地址。该源码包与安装包在同一 GitHub Release 提供。
GPL 正文及组件声明见 `GPL-2.0.txt`、`COMPONENTS.md`。

## 从源码包重建

需要 macOS arm64、Python 3.11，以及提供 Apple Clang
17.0.0（clang-1700.6.4.2）和 SDK 26.2 的 Command Line Tools。
最低运行系统参数为 macOS 14.0。不使用 Homebrew 媒体库。

解压 `Venus-<version>-media-sources.tar.gz`，在解压目录运行：

```sh
python3.11 desktop/scripts/build-media-tools.py --archives archives
```

源码包包含四份实际输入归档、构建脚本、锁定清单、声明和最小版本元数据，
不需要 Venus checkout。构建使用包内归档，不重新获取媒体源码；
仍需网络从 PyPI 安装固定 Meson 1.9.0、Ninja 1.13.0 到本次隔离环境。
实际安装件哈希、工具版本和编译器版本保存在构建记录中。

在 Venus checkout 中不传 `--archives` 时，脚本从锁定来源获取源码，
核对 x264 的精确提交与规范归档哈希后再编译。
不允许浮动分支、替代镜像或未经校验的本机二进制。

结果位于 `desktop/vendor/media-tools/darwin-arm64/`：两个工具、
`build-manifest.json`、`licenses/` 和 `media-sources.tar.gz`。
安装使用中性前缀 `/venus-media`，但只暂存到隔离目录，不写系统目录。
App 仅包含工具和声明；编译器、构建环境、源码包不进入 App。

缓存存在时构建脚本拒绝覆盖。打包入口会重新核验缓存；
输入、工具链或版本变化需要保留旧现场后进行新的构建。
重新签名会改变二进制哈希，不得用签名前缓存覆盖冻结 App。
不同工具链下的输出不承诺逐字节相同。
