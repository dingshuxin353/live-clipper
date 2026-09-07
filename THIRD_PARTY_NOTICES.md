# Third-Party Notices

## MiSans

Venus 使用 MiSans 作为界面字体。MiSans 字体版权归小米科技有限责任公司所有，
不适用 Venus 的 MIT License。

Venus 内置以下四个未经修改的原版 WOFF2 文件：

- `MiSans-Regular.woff2`
- `MiSans-Semibold.woff2`
- `MiSans-Bold.woff2`
- `MiSans-Heavy.woff2`

官方许可协议：
<https://hyperos.mi.com/font-download/MiSans%E5%AD%97%E4%BD%93%E7%9F%A5%E8%AF%86%E4%BA%A7%E6%9D%83%E8%AE%B8%E5%8F%AF%E5%8D%8F%E8%AE%AE.pdf>

完整许可副本随字体保留在应用资源目录的相对路径
`web_static/fonts/MiSans-Font-License.pdf`。

## Astryx Design System 与 StyleX

Venus 的 React 界面使用以下精确版本：

- `@astryxdesign/core` 0.1.9 — MIT License
- `@astryxdesign/theme-stone` 0.1.9 — MIT License
- `@stylexjs/stylex` 0.19.0 — MIT License
- react-router-dom 7.18.2 — MIT License（直接运行时依赖）
- react-router 7.18.2 — MIT License（运行时传递依赖）

正式前端包还包含这些运行时传递依赖：

- `intl-messageformat` — BSD-3-Clause
- `@formatjs/fast-memoize`、`@formatjs/icu-messageformat-parser`、
  `@formatjs/icu-skeleton-parser` — MIT License
- `lucide-react` — ISC License
- `css-mediaquery` — BSD License
- `invariant`、`js-tokens`、`loose-envify`、`styleq`、`scheduler` —
  MIT License
- `react`、`react-dom` — MIT License

这些第三方组件分别遵循其上游许可证，不适用 Venus 自身代码的许可证声明。

## Remix Icon

界面使用 Remix Icon 4.9.0 的离线 SVG 路径子集，遵循 Remix Icon License v1.0（2026 年 1 月）。原始许可保存在 [remix-license.txt](frontend/src/ui/remix-license.txt)，随前端包分发，并可从应用设置打开。该图标许可不适用 Venus 自身代码的许可证声明。
