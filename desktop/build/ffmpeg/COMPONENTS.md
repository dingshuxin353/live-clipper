# 媒体组件声明

ffmpeg 与 ffprobe 9.0.1 的本次组合采用 GPL v2 或更新版本，正文见 `GPL-2.0.txt`。
FFmpeg 版权所有 © 2000–2026 FFmpeg developers；x264 为 VideoLAN / x264 项目及其作者的作品，
按 GPL v2 或更新版本提供。x264 的精确源码提交见 `sources.lock.json`（包含在源码包中）。
不启用 nonfree 或 GPL v3 专属组件。完整原始版权、许可与源文件保留在对应源码包内。

## Independent JPEG Group

This software is based in part on the work of the Independent JPEG Group.

FFmpeg 的 `libavcodec/jfdctfst.c`、`jfdctint_template.c`、`jrevdct.c`
保留其原始 IJG 声明。本次未对这些文件作任何添加、删除或修改。
这些声明及 FFmpeg 自带其他组件的许可随未修改的 FFmpeg 源码归档提供。

## dav1d 1.5.4 — BSD-2-Clause

Copyright © 2018-2025, VideoLAN and dav1d authors
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## 系统库及构建工具

zlib 与 iconv 使用 macOS 系统库，不随 App 另行复制第三方动态库。
pkgconf 2.5.1 仅用于构建；其未修改源码及原始许可包含在源码包中。
Meson 与 Ninja 只安装到开发/发布机的隔离构建环境，不放入 App。
