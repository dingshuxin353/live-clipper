# FFmpeg 与 ffprobe 来源

这组 macOS arm64 工具由 Martin Riedl 构建，版本为
`9.0.1-https://www.martin-riedl.de`，构建日期为 2026-08-18。
Venus 通过独立进程调用工具，不修改 Venus 自身的许可证。

固定下载目录：
<https://ffmpeg.martin-riedl.de/download/macos/arm64/1787073674_9.0.1/>

| 文件 | 字节数 | SHA-256（上游签名原件） |
| --- | ---: | --- |
| ffmpeg.zip | 28447413 | 8287a1b2229e05eb41859f073e18e6c52c60a778f2f5e6881070fe51b79407fe |
| ffprobe.zip | 28370930 | 102a26b8940a053298d9929bfaae71e4b6ef65ba5f19a99a88c433108560741a |
| ffmpeg | 66334032 | 393e4c395020a1cb7cbd77fbe00599ce69d1c6466fee0dbd59d13f86a81a1611 |
| ffprobe | 66159232 | 7abc49fb2bdf2204f018e76dc6e0a8ae7643313bae09a9fa43e7eb12442271bc |

重新签名会改变可执行文件的哈希。上述身份用于构建前核验，
不能用来覆盖或修补已经签名的 App。

两个工具的 `-L` 声明为 GPL v3 或更新版本，未启用 nonfree。
GPL 全文见同目录 `GPL-3.0.txt`，原文取自
<https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.GPLv3>。
FFmpeg 的许可说明见 <https://ffmpeg.org/legal.html>。

## 分发材料尚未齐备

本说明和 GPL 正文不足以完成这组静态工具的分发材料。
打包入口还要求 `COMPONENTS.md` 和 `CORRESPONDING-SOURCE.md`：
前者需保留实际组件的必要版权及许可声明；后者需列出与二进制
准确对应的源码、构建脚本、补丁及正式下载位置。材料未核实前不能发布。

已找到的上游构建脚本提交为
`f63b8aab8f5ce1a067da86ba69e34a36a7e217e5`：
<https://git.martin-riedl.de/ffmpeg/build-script/src/commit/f63b8aab8f5ce1a067da86ba69e34a36a7e217e5>。
该提交的 x264 脚本下载浮动 master，构建目录的 `versions.txt`
仅记录 `0.165.x`。这两项不能证明实际 x264 源码身份，
也不能将构建脚本的 Apache 2.0 许可证当作二进制许可证。
