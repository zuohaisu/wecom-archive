# Handoff: 康冠时代标志 精修 v1.1（品牌资产替换）

## Overview

这是一次**品牌资产替换**，不是 UI 改版。标志（横版 / 竖版 / 图形标 / favicon / App 瓦片）在 v1.0 的基础上做了一轮精修：字距均化、汉字与拉丁字母回到同一基线、画布裁到实际墨迹、全套斜率统一、并针对设计系统 24px 侧栏槽位新增了一个专用瓦片。

**图形三元素的比例与位置、组合关系（图形 + 康冠时代 + CROWN TIME）、主色 `#1F5CC4` / 墨色 `#1D2733` 都没有改动**，注册商标形态不受影响。因此本次改动是：**替换文件 + 一处 `src` 改动 + 一段 `<head>` 引用**，没有布局、组件、状态或交互变化。

## About the Design Files

`assets/` 里是**可直接使用的成品矢量与位图资产**（SVG / PNG），不是需要重写的原型代码。`reference/Logo 精修 v1.1.dc.html` 是本轮的设计评审文档（HTML 原型，说明每一处改了什么、为什么改），只作参考，**不要把它搬进代码库**。

## Fidelity

High-fidelity。SVG 为最终交付件，坐标精确到 0.01 单位，请按原文件替换，**不要重新导出、不要重新裁切、不要另加 padding**（净空区由版式给，见「净空区」一节）。

## 替换映射

仓库现状（`wecom-archive`）：品牌文件在 `brand/`，生产环境静态路径通常为 `/static/brand/`。请按实际路径对应。

| 目标文件（仓库） | 替换来源（本包） | 说明 |
|---|---|---|
| `brand/logo-horizontal.svg` | `assets/logo/svg/logo-horizontal.svg` | 画布由 `275×96` 变为 `273.6×96` |
| `brand/logo-horizontal-reverse.svg` | `assets/logo/svg/logo-horizontal-reverse.svg` | 同上 |
| `brand/logo-icon-mono.svg` | `assets/logo/svg/logo-icon-mono.svg` | 几何未变，仅同步 |
| `brand/favicon.svg` | `assets/favicon/favicon.svg` | 斜边 `1:3.56` → `1:3` |
| `brand/favicon-16.png` `-32` `-48` | `assets/favicon/favicon-16.png` 等 | 按新矢量重出 |
| `brand/apple-touch-icon.png` | `assets/favicon/apple-touch-icon.png` | 180×180 |
| `brand/icon-tile.svg` | `assets/favicon/icon-tile.svg` | **仅供 App 图标**（iOS 圆角比例 21.9%） |
| — 新增 → `brand/icon-tile-24.svg` | `assets/favicon/icon-tile-24.svg` | **新增**，专供 20–28px 的 UI 槽位 |
| — 新增 → `brand/logo-horizontal-mono.svg` | `assets/logo/svg/logo-horizontal-mono.svg` | 单色黑（合同 / 资质 / 传真） |
| — 新增 → `brand/logo-stacked*.svg` | `assets/logo/svg/logo-stacked[-mono|-reverse].svg` | 竖版 `171.6×190`，登录页 / 启动页 |
| — 新增 → `brand/logo-icon.svg` `logo-icon-reverse.svg` `logo-icon-small.svg` | `assets/logo/svg/` | 图形标彩色 / 反白 / 小尺寸版 |
| `brand/icon-192.png` `icon-512.png` | `assets/favicon/` | PWA / Android |
| （位图各倍率） | `assets/logo/png/*` | 命名用 `-1x` `-2x` `-4x` `-8x` |

**`brand/favicon.ico` 需要开发端自行重新打包**（本包不含 ICO）：

```bash
magick assets/favicon/favicon-16.png assets/favicon/favicon-32.png assets/favicon/favicon-48.png brand/favicon.ico
```

## 需要改代码的地方（共两处）

### 1. 侧栏品牌区改用 24px 专用瓦片

`pages/shell.js`（生产环境为 Jinja include）里的 `.sidenav-brand`：

```diff
- <img class="sidenav-logo" src="../brand/icon-tile.svg" alt="康冠时代" width="24" height="24">
+ <img class="sidenav-logo" src="../brand/icon-tile-24.svg" alt="康冠时代" width="24" height="24">
```

原因：`icon-tile.svg` 缩到 24px 时，图形标左侧间隙只剩 0.6px，三元素糊成一块；且它自身的圆角是 5.25px@24，与 `.sidenav-logo` 上 `border-radius: var(--radius-md)`（7px）的裁切不同心。`icon-tile-24.svg` 按 24px 像素网格重画（间隙 1px / 2px），圆角直接取 `--radius-md`，`.sidenav-logo` 的 CSS 圆角保留即可（成为无害的同心裁切）。

**`.sidenav-logo` 的 CSS 不要动**，尺寸仍是 `24×24`、`border-radius: var(--radius-md)`。

### 2. `<head>` 图标引用

```html
<link rel="icon" href="/static/brand/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/static/brand/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">
<link rel="mask-icon" href="/static/brand/logo-icon-mono.svg" color="#1F5CC4">
<link rel="manifest" href="/site.webmanifest">
```

`site.webmanifest`：

```json
{
  "name": "康冠时代 · 企业微信会话存档",
  "short_name": "康冠存档",
  "icons": [
    { "src": "/static/brand/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/brand/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ],
  "theme_color": "#1F5CC4",
  "background_color": "#FFFFFF",
  "display": "standalone"
}
```

## 使用规则（放哪个版本）

| 场景 | 文件 | 尺寸 |
|---|---|---|
| 侧栏品牌区 | `icon-tile-24.svg` | 24×24（20–28px 均可） |
| 浏览器标签 | `favicon.svg` / `.ico` | 16 / 32 / 48 |
| 深色平台条 `.admin-bar`（高 44px） | `logo-horizontal-reverse.svg` | `height: 18px` |
| 深色页脚 / Blue 900 区块 | `logo-horizontal-reverse.svg` | `height: 28px` |
| 登录页 `.auth-aside` | `logo-stacked.svg` | `width: 180px` |
| 官网、文档、白底 | `logo-horizontal.svg` | `height ≥ 24px`（下限见「最小尺寸」） |
| 合同 / 资质 / 传真 | `logo-horizontal-mono.svg` | 印刷宽 ≥ 30mm |
| 头像 / 水印 / pinned tab | `logo-icon*.svg` | ≥16px |

**SVG 尺寸写法**：所有画布已裁到实际墨迹，只设 `height`（或只设 `width`）即可得到准确的另一边，不要同时写死两边。宽高比：横版 `2.85:1`，竖版 `0.903:1`，图形标 `0.833:1`。

**最小尺寸**：横版组合屏幕宽 ≥120px、印刷 ≥30mm；竖版 ≥96px / 24mm；图形标 ≥16px（≤32px 时用 `logo-icon-small.svg`，间隙已加宽）。

**净空区**：以图形标方块的宽度 `x`（= 横版高度 × 0.229）为单位，标志四周留空 ≥ `x`。资产文件里不含 padding，请用 CSS `margin` / 容器 padding 给。

**背景规则**：白 / 浅色底用彩色版；深色底（Blue 800/900、`--color-admin-bar` `#101720`）用反白版；禁止把彩色版放在明度接近 `#1F5CC4` 的蓝底上。

**禁止**：拉伸 / 压扁 / 旋转 / 倾斜；改变三元素相对位置与比例；给标志加描边、阴影、渐变、发光；重排为注册证之外的组合。

## Design Tokens（本次涉及的）

| 名称 | 值 | 用途 |
|---|---|---|
| Crown Blue 500 | `#1F5CC4` | 标志本体、`mask-icon` color、`theme_color` |
| Crown Blue 600 | `#1A4FA9` | hover（若迁移主色） |
| Ink 900 | `#1D2733` | 标志文字 |
| White | `#FFFFFF` | 反白版、瓦片内图形 |
| `--radius-md` | `7px` | `icon-tile-24.svg` 圆角、`.sidenav-logo` |
| App 瓦片圆角 | `21/96 = 21.9%` | `icon-tile.svg`、apple-touch-icon |

## 待决策（不阻塞本次替换）

设计系统主色 `--color-primary: #1677ff`（白底对比度 4.10:1，正文文字不过 WCAG AA）与品牌主色 Crown Blue 500 `#1F5CC4`（6.20:1）同时出现在一条顶栏里会互相打架。品牌规范建议把产品主色迁到 Crown Blue：

```diff
- --color-primary:#1677ff;--color-primary-hover:#0958d9;--color-primary-active:#003eb3;
+ --color-primary:#1F5CC4;--color-primary-hover:#1A4FA9;--color-primary-active:#15418C;
```

顺带把链接文字对比度从 4.10 提到 6.20。**需要业务方拍板后再改**，本次替换不含此项。相关的 `--color-primary-soft` / `-border` / `--color-viz-*` / `--focus-ring` 也要一起过一遍，属于独立任务。

## 验收清单

1. 侧栏 24px 瓦片：图形标三元素间可见分隔（1px / 2px），瓦片圆角与相邻卡片一致，无双重圆角毛边。
2. 浏览器标签 16px：图标不糊，三元素可辨。
3. 深色平台条与页脚：反白版，无灰边、无描边。
4. 横版只设 `height` 时宽度自动为 `height × 2.85`，右侧无多余空白。
5. 竖版底部 `C`、`O` 的圆底完整，未被画布切平。
6. 浅色 / 深色主题（`data-theme="dark"`）下均检查一遍。
7. `favicon.ico` 已按新 PNG 重新打包。

## Files

- `assets/` — 全套矢量与位图（34 个文件），目录结构与仓库 `brand/` 对齐
- `assets/CHANGELOG.md` — 七处修正的逐项数据（原值 → 新值）
- `reference/Logo 精修 v1.1.dc.html` — 设计评审文档（仅参考，需在设计工程里打开）
