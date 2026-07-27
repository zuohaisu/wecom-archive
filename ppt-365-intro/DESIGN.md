# DESIGN.md — 365企微会话存档 产品介绍

## 1. 画布与全局母版（A/B/C 三区）

- 画布：1280 × 720，安全区 padding：上下 20px、左右 72px。
- **A 标题块** 0–120px（含上 padding 20）：主标题 34px bold，位于左对齐 x=72；章节/页码徽标在右上（角落，全篇一致）。
- **B 内容区** 120–660px（540px 可用）：正文、图、卡片、SVG。禁止挤占 A/C。
- **C 页脚条** 660–720px（含下 padding 20）：左侧项目名「365企微会话存档」14px 灰字；右侧页码 `NN / 13` 14px 灰字。封面/过渡/结束页可省略 C 区。
- 同类内容页标题位置、页码位置全程一致。

## 2. 颜色系统（仅 4 个 hex）

| 角色 | hex | 用途 |
| :--- | :--- | :--- |
| 主色 primary | `#3B82F6` | 标题栏渐变起色、主按钮、主图标、焦点数字 |
| 辅色 secondary | `#06B6D4` | 渐变止色、第二系列、分隔线、次图标 |
| 墨色 ink | `#0F172A` | 正文/标题主色、深底块 |
| 背景 bg | `#FFFFFF` | 页面底色、卡片底 |

- 浅色卡片底用 `rgba(59,130,246,0.08)`；次级文字用 `rgba(15,23,42,0.6)`；蒙版用 `rgba(15,23,42,0.55)`。均派生自上述 4 色，不新增 hex。
- **面积分配**：主色≤60%（背景/大色块/标题栏底）；辅色≤30%（卡片/第二系列/分隔）；强调色≤10%（仅巨型数字、CTA、焦点标注），Hero 页强调色可到 15–20%。
- 色彩节奏：封面/定位/结束为「深底+渐变」爆发页；信息页以白底+主色标题栏为主，强调色克制。

## 3. 字体系统（商务现代）

- 中文：`'PingFang SC','Source Han Sans SC','Microsoft YaHei',sans-serif`
- 西文/数字：`'Inter','Helvetica Neue',Arial,sans-serif`
- 字号阶梯：
  - 封面主标题 64px bold；章节大字 60px bold；巨型锚点数字 96px bold。
  - 页面主标题（A 区）34px bold；副标题/卡片小标题 24px bold。
  - 正文 20px regular / lineHeight 1.6；引文 18px italic。
  - 页脚/页码 14px regular。
- 巨型数字与正文用同一家族不同字重，确保层级跳跃。

## 4. 渐变与半透明策略

- 渐变：`linear-gradient(135deg, #3B82F6 0%, #06B6D4 100%)` → 用于封面/过渡/结束蒙版叠层、标题栏底、CTA 按钮、巨型数字渐变文字。
- 半透明：`rgba(59,130,246,0.08)` 浅色卡片底；大图蒙版 `rgba(15,23,42,0.55)` 保证白字可读；卡片 `boxShadow:'0 4px 20px rgba(15,23,42,0.08)'` 浮起感。
- 背景图上叠蒙版后再骑线放文字；元素重叠底层 `opacity:0.15~0.3`。

## 5. 配图系统（分级 + 风格统一）

- 风格：全篇统一「摄影感科技氛围插画」（蓝图/青调、干净、低饱和），禁止摄影/插画/3D 混用。
- L1 主视觉（B 区 ≥40%）：hero_cover.png / risk_concept.png / secure_decrypt.png / console_review.png / scenario.png。
- L2/L3：FAIcon 图标（统一实心、尺寸一致）、SVG 数据流图、页脚徽标。
- 严禁 200×70 装饰小贴片塞标题栏；不同页 L3 位置一致。

## 6. 页面映射表（逐页契约）

| # | 文件 | 类型 | 角色 | 版式 | L1 文件 | 字数 | 留白 | 色彩分配 | 关键约束 |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 01 | slide_01_cover.jsx | cover | hero | 全屏视觉+骑线 | hero_cover.png | 40 | 35% | 深底+渐变爆发 | 全幅底图+左侧大标题 |
| 02 | slide_02_catalog.jsx | catalog | supporting | 左标题+右内容 | — | 120 | 25% | 主40%+辅20% | 5 章列表，每项≥20字 |
| 03 | slide_03_why.jsx | section | hero | 全幅图+骑线 | risk_concept.png | 60 | 40% | 深底+蒙版 | 章节01+风险洞察 |
| 04 | slide_04_pain.jsx | content | supporting | 非对称双栏 | — | 240 | 28% | 主50%+辅15% | 60:40 监管vs盲区 |
| 05 | slide_05_position.jsx | content | hero | 居中金句 | — | 70 | 45% | 渐变爆发 | 一句话定位 |
| 06 | slide_06_capabilities.jsx | content | supporting | 左标题+右内容 | — | 300 | 25% | 主40%+辅20% | 2×3 能力网格 |
| 07 | slide_07_archive.jsx | content | supporting | 左大图+右文字 | secure_decrypt.png | 220 | 28% | 主50%+辅20% | 图占60%宽 |
| 08 | slide_08_search.jsx | content | supporting | 巨型数字+洞察 | 数字100% | 160 | 30% | 强调20%爆发 | ≥48px锚点数字 |
| 09 | slide_09_console.jsx | content | supporting | 上大图+下卡片 | console_review.png | 200 | 26% | 主45%+辅20% | 图占55%宽 |
| 10 | slide_10_security.jsx | content | supporting | 非对称双栏 | — | 260 | 28% | 主45%+辅20% | 60:40 安全vs合规 |
| 11 | slide_11_scenario.jsx | content | supporting | 左大图+右文字 | scenario.png | 240 | 28% | 主50%+辅20% | 图占60%宽 |
| 12 | slide_12_architecture.jsx | content | supporting | 图表+洞察 | SVG架构 | 220 | 30% | 主40%+辅20% | 左SVG右要点 |
| 13 | slide_13_ending.jsx | ending | hero | 全屏视觉+金句 | hero_cover派生 | 50 | 40% | 深底+渐变 | CTA预约演示 |

## 配图清单（ImageGen）

| 文件名 | 用途页 | prompt 要点 | 风格 | 核对 |
| :--- | :--- | :--- | :--- | :--- |
| hero_cover.png | 01/13 | 企业微信会话存档抽象概念：漂浮对话气泡+安全云锁+蓝色数据流，深蓝青科技氛围，干净留白，无文字 | 科技插画 蓝青 | 已核对：无水印，风格一致，适配封面/结束页全幅 |
| risk_concept.png | 03 | 合规风险概念：放大镜审视文档+警示盾牌+隐去人脸的对话剪影，蓝青低饱和，氛围感，无文字 | 科技插画 蓝青 | 已核对：无水印，风格一致，适配章节过渡页 |
| secure_decrypt.png | 07 | 数据安全解密：盾牌包裹钥匙与锁、加密数据流进入保险库，蓝青科技，无文字 | 科技插画 蓝青 | 已核对：无水印，风格一致，适配左大图页 |
| console_review.png | 09 | 管理员在屏幕前审查企业微信会话（三栏界面暗示），现代办公室，蓝青光，无文字UI | 科技插画 蓝青 | 已核对：无水印，风格一致，适配上大图页 |
| scenario.png | 11 | 企业团队协作使用企业微信服务客户场景：多人围绕屏幕、对话气泡浮现，蓝青商务，无文字 | 科技插画 蓝青 | 已核对：无水印，风格一致，适配左大图页 |
