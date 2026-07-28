# RND-320 QA / 验收 agent 提示词 —— 聚合聊天记录折叠态不渲染媒体

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-320-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认审阅控制台 chatrecord 折叠态已对齐企微客户端：折叠态 N 条均不渲染媒体（零 `<img>/<video>/<audio>`、零媒体网络请求），仅显示「发送人 + [消息种类] + 时间 + 文本摘要」；点「展开全部」后才完整 hydrate 媒体；嵌套 chatrecord 卡片与 mixed 内联行为不变；零回归；i18n 三语（zh-CN/zh-TW/en）一致。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 折叠态（根因 A/B）
- [ ] F1 折叠态下，N 条 chatrecord **每条都不出现** `<img>/<video>/<audio>` 标签 —— 证据：浏览器 Elements 面板检查该消息子树，或 `document.querySelectorAll('#cr-rows-... img, #cr-rows-... video, #cr-rows-... audio').length === 0`
- [ ] F2 折叠态下，对每条子项的媒体**不发网络请求**（含 thumbnail）—— 证据：DevTools Network，滚动到该消息，过滤 img/media，确认 0 请求（对比开发 agent 的 RED 基线）
- [ ] F3 每条显示「发送人 + [消息种类] + 时间」—— 证据：截图 + DOM 检查 `.chatrecord-row-sender` / `.chatrecord-row-kind` / `.chatrecord-row-time` 存在
- [ ] F4 纯文本子项显示文本片段摘要（无括号种类）；媒体子项显示 `[图片]/[语音]/[文件]/[链接]/[视频]/[表情]` 之一 —— 证据：截图核对各类型
- [ ] F5 [消息种类] 文案来自 `MessageTypeRegistry.resolve(type).placeholderKey` 或 `messageType.*`，非硬编码新集合 —— 证据：读源码 `chatrecordChildKindLabel`

### 展开态
- [ ] E1 点「展开全部」后 N 条全部完整渲染；图片显示缩略图/原图、语音出现播放器 —— 证据：截图 + Network 出现媒体请求
- [ ] E2 按钮文案在「展开全部」(`chatrecord.expandAll`) / 「折叠」(`chatrecord.collapse`) 间正确切换 —— 证据：点击前后 `btn.textContent`
- [ ] E3 「在弹层中查看完整记录」(`chatrecord.viewInViewer`) 仍可点击进入弹层 Viewer —— 证据：点击进入 `openChatrecordViewer`

### 不变路径
- [ ] N1 嵌套 chatrecord 卡片（`renderChatrecordCard`，弹层入口按钮）行为不变 —— 证据：点开含嵌套 chatrecord 的消息，弹层正常
- [ ] N2 mixed 内联展示（`renderMixedMessage`）行为不变 —— 证据：含 mixed 的消息仍正确内联渲染

### 刷新 / 重建
- [ ] R1 自动刷新该会话 / 切回会话 / 时间线重建后，新加载的 chatrecord **默认折叠**（不因 hydrate 时序「闪一下展开」）—— 证据：多次刷新观察，或检查 `renderChatrecordMessage` 默认不调用 `hydrateRichMedia`、无全局展开态

### 性能
- [ ] P1 含 5 条图片的 chatrecord，滚动到该消息时**不再触发 N 个 thumbnail 预加载请求** —— 证据：Network 面板对比 RED/GREEN（折叠态 0 媒体请求）

### i18n
- [ ] I1 新增 key（至少 `placeholder.image`）在 zh-CN / zh-TW / en 三语块均存在且翻译合理 —— 证据：`grep -n "placeholder.image" backend/app/assets/i18n.js` 三处命中
- [ ] I2 `make verify` 中 i18n 套件（`test_i18n_foundation.py`）全绿；`test_registry_contains_zh_cn_zh_tw_and_en` 仍 `['en','zh-CN','zh-TW']`

### 全局契约
- [ ] C1 `git diff` 不含任何 `.py` 业务逻辑改动（纯前端 JS + i18n 资源）—— 证据：`git diff --name-only`
- [ ] C2 `make verify` 全绿（含 RND-206/204/201 回归）
- [ ] C3 无 git commit 产生（改动全在工作区）

## 三、回归套件（必须全绿）
`make verify` + 重点：`test_rnd_206_rich_media.py`、`test_rnd_206_qa_fixes.py`、`test_message_type_registry_core.py`、`test_i18n_foundation.py`、`test_rnd_204_incremental_refresh.py`、`test_archive_console_v2_qa_fixes.py`。

## 四、智能路由判定（每轮必给）
- 源码有 Bug（折叠态仍发媒体请求 / 标签取不到 / 嵌套或 mixed 被破坏）→ 反馈开发 agent 修复，附文件:行号 + 期望；不自行改实现。
- 测试断言旧折叠行为（如断言 `#cr-rows-` 为 `display:none` 或前 2 条完整渲染）→ 可自行修正测试并标注（属「测试迁就旧路径」例外）。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-320 验收结论：PASS / FAIL
折叠态：F1–F5 各项 PASS/FAIL + 证据（DevTools 截图/计数）
展开态：E1–E3 PASS/FAIL
不变路径：N1–N2 PASS/FAIL
刷新/性能/i18n：R1 / P1 / I1–I2 PASS/FAIL
回归：make verify __（绿/红）
契约：backend/app 零 diff __；未 commit __
遗留：__
```
