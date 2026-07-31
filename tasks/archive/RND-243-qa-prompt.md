# RND-243 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收通过后由用户决定是否合入。
> 验收对象：开发 agent 按 RND-243 解析修复产出的**工作区改动**（未提交）——
> `backend/app/structured_message_parser.py`（`_parse_nested_item` 归一化 + 两个新辅助函数）+ `backend/tests/test_structured_message_parser.py`（10 个新增回归用例）。
> 本版本对照 2026-07-27 代码实际状态与本地 mock 复现结论复核。

---

## 一、验收目标

确认 RND-243「聚合消息（chatrecord / mixed）内嵌多媒体子消息显示异常」已修复，且**零回归**：
- **Bug 1（媒体被当文本回显）**：WeCom 聊天记录转发内嵌子消息的 `type` 用 `ChatRecord`-前缀词汇（`ChatRecordImage` / `ChatRecordVideo` / `ChatRecordVoice` / `ChatRecordText` / `ChatRecordLink` / …），旧代码按规范类型硬分支 → 落入 unknown/text 分支把整段媒体 JSON 当文本。修复：后端��一化为规范类型，复用既有 media / structured / text / composite 分支。
- **Bug 2（1970 时间戳）**：chatrecord 子消息 `msgtime` 单位是**秒**，前端 `fmtTime()` 只认**毫秒** → 渲染成 1970。修复：子消息 `msgtime` 秒→毫秒换算。
- 前端**无需改动**：归一化后 `node.type` 已是规范类型，`renderCompositeNode` 本就按规范类型 dispatch（`COMPOSITE_MEDIA_KINDS`）。

**范围外（本 QA 不验收，见第六节）**：已归档**历史**聊天记录的「重新解析 + 内嵌媒体下载回填」——那是独立后续任务，不影响本修复对「新归档 / 重解析」消息的正确性。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 后端（解析层）
- [ ] **K1** `_normalize_nested_child_type`（`structured_message_parser.py:179`）正确将 `ChatRecord*` 子类型归一为规范类型：
  - `ChatRecordImage→image`、`ChatRecordVideo→video`、`ChatRecordVoice→voice`、`ChatRecordFile→file`、`ChatRecordEmotion→emotion`、`ChatRecordText→text`、`ChatRecordLink→link`、`ChatRecordLocation→location`、`ChatRecordMixed→mixed`、`ChatRecordWeapp→weapp` 等。
  - 实现是「去 `ChatRecord` 前缀 + 小写 + 命中 `_NESTED_KNOWN_TYPES` 才归一」，对未知 `ChatRecordXxx` 原样保留（仍走 unknown 兜底，绝不丢）。
  - **对照官方名单**：核对企业微信「聊天记录（chatrecord）」官方子类型清单，确认无常见子类型被静默漏归一（若有官方子类型其后缀不在 `_NESTED_KNOWN_TYPES`，应仍可见、不崩溃；若发现高频子类型缺失，报告开发 agent 扩 `_NESTED_KNOWN_TYPES`）。
  - 证据：10 个新增回归用例（`test_parse_chatrecord_message_chatrecord_prefixed_child_normalized` 参数化 8 种 + `test_parse_chatrecord_message_chatrecord_image_no_longer_echoes_raw_json`）**全 PASS**。
- [ ] **K2** 既有规范类型（image/voice/video/text/link/location/mixed…）解析行为**不变**（无回归）。
  - 证据：`test_parse_mixed_message_*`、`test_parse_chatrecord_message_*（非 RND-243 新增部分）` 全 PASS。
- [ ] **K3** `_normalize_nested_timestamp`（`structured_message_parser.py:201`）秒→毫秒换算正确：
  - 秒级值（如 `1_700_000_000`）→ `1_700_000_000_000`；毫秒级值（如 `1_700_000_000_000`）**不变**（阈值 `<1e12`）。
  - 证据：`test_parse_chatrecord_message_chatrecord_child_msgtime_seconds_to_ms` PASS；另手验一个已 ms 的值不变。
- [ ] **K4** `media_refs` 登记的是**规范** `type`（如 `"image"`），属 `GENERIC_DOWNLOAD_MSGTYPES`（`app/media_download._SIGNATURE_CATEGORY_BY_MSGTYPE` 含 image/voice/video/file/emotion）→ `download_one()` 可处理。
  - 证据：`test_nested_media_types_are_a_subset_of_media_download_generic_types`（`test_structured_message_parser.py:1105`）在**完整依赖环境**下 PASS（`_NESTED_MEDIA_TYPES` 未被本次改动触碰，仍是 image/voice/video/file/emotion 子集）；`ChatRecordImage` 归一后 media_refs `type=="image"`。
  - ⚠️ 该测试在缺 `sqlalchemy`/`pydantic_settings` 的**最小沙箱**会因 `app.media_download` import 失败而误报 FAIL——非回归，必须在完整环境验证。

### 前端（渲染层，确认无需改动且行为正确）
- [ ] **F1** 前端**不应**有任何 `ChatRecord` 字面量依赖——归一化全在后端完成。
  - 证据：`grep -rn "ChatRecord" backend/app/web/static/console/` 应**无命中**（渲染只认规范类型）。
- [ ] **F2** `renderCompositeNode`（`message-renderers.js:871`）对归一后的 `image/video/voice/file/emotion` 走 `COMPOSITE_MEDIA_KINDS`（`message-renderers.js:832`）→ `renderCompositeNodeMedia`（`:834`）：当 `media.status==='available'` 且 `access_url` 存在时渲染真实媒体；否则降级为占位符（**绝不**回显原始 JSON）。
- [ ] **F3** 媒体装配链 `_enrich_nested_media_fields`（`timeline_service.py:348`）/`_build_nested_media_descriptor`（`:238`）按 `sdkfileid` 查 `MediaFile` 注入 `status`/`access_url`/`mime_type`/`size_bytes`，与归一后的 `media` 节点对接正确。
- [ ] **F4** 端到端（需一条**新归档**且内嵌媒体已下载的 chatrecord 消息）：时间线该子消息显示**真实图片/视频**，不是 raw JSON、不是裂图占位符。
  - 证据：UI 截图 / DOM 核对 `composite-node` 内含 `<img>`/`<video>` 而非文本节点。
  - ⚠️ **历史**已归档消息若无对应 `MediaFile` 会显示占位符——这是预期的，由第六节回填任务解决，**不计入本 QA FAIL**。
- [ ] **F5** 时间戳显示正确（非 1970）：chatrecord 子消息 `msgtime` 为秒级时，UI 显示正确北京时间。
  - 证据：UI 核对一个 ChatRecordText 子消息的时间戳格式为 `YYYY-MM-DD HH:mm:ss`、年份非 1970。

### 全局契约
- [ ] **C1** `make verify` **全绿**（含 K4 的 subset 不变式测试）。
- [ ] **C2** 无新增/删除路由（`test_http_contract` 路由数不变）。
- [ ] **C3** i18n 未动（无新增/删除 key）。

---

## 三、回归套件（必须全绿）

```
make verify
```
重点确认（任一失败即 FAIL，附失败栈）：
- `backend/tests/test_structured_message_parser.py` —— **含 RND-243 新增 10 例**（参数化 `chatrecord_prefixed_child_normalized` + `no_longer_echoes_raw_json` + `msgtime_seconds_to_ms`），以及既有 mixed/chatrecord 用例 + `test_nested_media_types_are_a_subset_of_media_download_generic_types`（K4）。
- 前端 composite 渲染测试（若存在 `test_archive_console_v2*.py` 中涉及 `renderCompositeNode` / chatrecord 卡片的用例）全绿。
- `test_http_contract.py`（路由数不变）。

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码有 Bug** → 反馈给开发 agent 修复，附具体错误 + 失败测试名 + 期望行为。**不自行改实现**。
- **测试代码有 Bug** → 你可自行修正测试（仅当断言了旧的坏行为；须在报告标注并说明依据）。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 对比。
最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮回归验证；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-243 验收结论：PASS / FAIL
Bug1（ChatRecord 归一）：PASS / FAIL（附 K1 官方名单核对结论）
Bug2（秒→毫秒）：PASS / FAIL（附 K3 证据）
前端渲染（F1-F5）：PASS / FAIL（附 F4 端到端截图结论；历史占位符是否出现=预期）
回归：make verify ___（绿/红，附失败项）
契约：路由数/租户/i18n —— 不变
遗留：___（若有）
```

---

## 六、范围外 / 后续（不在本 QA，需另立任务）

**历史数据回填**：已归档的 chatrecord 消息在旧代码时期 `media_refs` 未登记内嵌媒体 → 对应 `MediaFile` 从未下载，且存库 `structured_content` 是旧「text=原始JSON」形态。即便本修复部署后：
- **新归档**消息 → 正确归一 + 自动下载 → 正常显示。✅
- **历史**消息 → 仍显示旧形态 / 占位符，除非执行一次「重解析 chatrecord `structured_content` + 补下载内嵌媒体」回填。

该回填建议另立 ticket（可挂在 RND-243 下或独立），验收对象为回填脚本：重跑 `parse_chatrecord_message` 更新 `structured_content` 并登记/下载缺失 `MediaFile`。本 QA **不覆盖**该项。
