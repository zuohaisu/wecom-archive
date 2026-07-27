# RND-257 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收对象：开发 agent 按 `rnd-257-execution-prompt.md` 产出的**工作区改动**（未提交）——主要是 `scripts/reparse_structured_content_once.py`（或等价管理端 action）+ 对应单测。
> 本版本对照 2026-07-27 代码实际状态复核。RND-243 解析修复已部署，本 ticket 是其子任务（历史数据第二层收口）。

---

## 一、验收目标

确认历史 `chatrecord` / `mixed` 消息被**无损重解析**为正确 `structured_content`（媒体节点 + 正确 `media_refs`），并尽量补全内嵌媒体字节——**零回归、安全边界严格**：

- **核心修复**：旧坏形态（子项 `text=含sdkfileid的JSON`）重解析后变为规整媒体节点，`media_refs` 含正确规范 `type` 与 `sdkfileid`。
- **下载补全**：触发既有 RND-200 嵌套下载管线，未过期 `sdkfileid` 落在 `MediaFile`。
- **降级**：过期 `sdkfileid` 下载 `sdk_error` 时，前端仍显示媒体节点+占位符+原始元信息，不回退 raw-JSON、不崩溃。
- **安全**：重解析**只**改 `structured_content` 单列；不碰 `decrypted_payload` / `media_files` / 解密状态 / 收件人。

**范围外（本 QA 不验收）**：RND-243 的解析层逻辑本身（已在 RND-243 验收过）；前端渲染/时间戳换算代码（RND-243 已修，本 ticket 仅消费其成果）。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 后端（重解析层）
- [ ] **K1** 重解析**通过重新隔离解密获取明文**（复用 `decrypt_worker._decrypt_message` + `decrypt_isolation.decrypt_message_isolated`，数据来自加密信封 `raw_encrypted_payload`/`encrypt_random_key`/`encrypt_chat_msg`），再 `json.loads` + `parse_structured_content(msgtype, decrypted)`（`structured_message_parser.py:1253`）；写入形状与 `decrypt_worker.py:541` **完全一致**：`record.structured_content` = 解析返回的**整个 dict**（含 `fields`/`raw`/`parse_warnings`，嵌套消息额外含 `media_refs`）。
  - 证据：mock `_decrypt_message` / `decrypt_message_isolated` 返回一段 chatrecord 明文信封，断言：
    - 重解析函数**确实调用了隔离解密**拿到明文并喂给 `parse_structured_content`（**断言没有读 `record.decrypted_payload`** —— 该列生产上恒为 NULL，SF-1 禁止填充）；
    - `structured_content` 顶层含 `fields`/`raw`/`parse_warnings`；嵌套含 `media_refs`；
    - `media_refs[0]["type"]` 为规范类型（`image`/`voice`/`video`/`file`/`emotion`），且 `"sdkfileid"` 与原始一致。
- [ ] **K2** 旧坏形态识别准确：
  - 含 `text=含sdkfileid JSON` 的子项被正确判定为坏 → 纳入重解析；
  - 已正确行（`media_refs` 正常、无 raw-JSON 回显）**不被误判**、不重复改写。
  - 证据：正向/反向样例各一，计数符合预期。
- [ ] **K3** 幂等：同一条历史记录重解析两次，结果稳定；`media_refs` 未变时跳过写（无多余 UPDATE）。
  - 证据：单测对同输入跑两次，第二次写操作计数为 0 或等价稳定。
- [ ] **K4** **安全边界（硬约束）**：重解析事务内 `UPDATE` 仅含 `structured_content` 单列；且 `decrypted_payload` 仍受 SF-1 约束**不被填充**。
  - 证据：mock `Session`，断言 flush/commit 的 `UPDATE` 语句/属性变更集合**仅** `structured_content`；`media_files`、解密状态、收件人、其它列均未被触碰；`record.decrypted_payload` **未被任何赋值**（SF-1 仍生效，重解析后仍为原值/NULL）。
- [ ] **K5** 只重解析 `chatrecord`/`mixed`（`NESTED_MEDIA_MSGTYPES`，`media_download.py:96`）；其它 `msgtype` 行不进入候选。
  - 证据：候选查询/遍历断言 `msgtype.in_(NESTED_MEDIA_MSGTYPES)` 过滤生效。
- [ ] **K6** `--dry-run` 不写库：只统计与打印抽样，不 commit 任何变更。
  - 证据：dry-run 模式下 mock session 无 `commit` / `UPDATE` 调用，退出码 0。

### 后端（下载补全层，复用 RND-200）
- [ ] **K7** 重解析提交后，触发既有嵌套下载扫描（`scripts/download_wecom_media_once.py` 或等价），`select_nested_media_candidates`（`media_download.py:396`）能拾取新登记 `media_refs.sdkfileid` 并 `download_one`（`media_download.py:568`）。
  - 证据：用一条带未过期 `sdkfileid` 的 chatrecord 走完整链路，`MediaFile` 行被创建、`download_status='downloaded'`。
- [ ] **K8** 复用而非新写：未新增下载逻辑；新增参数（如有）仅作范围过滤，复用 `build_nested_media_candidate_query` / `iter_nested_media_refs`（`media_download.py:329`）。
  - 证据：diff 不含独立的下载实现，仅调用既有函数。

### 前端 / 端到端
- [ ] **F1** 端到端（历史 chatrecord，先重解析再经 timeline 渲染）：前端**不再**显示 raw-JSON 文本，显示规整媒体节点（图片/视频/语音/文件/表情）+ **正确时间戳（非 1970）**（RND-243 秒→毫秒已部署，此处只验证消费正确）。
  - 证据：前端渲染该消息，断言无 `sdkfileid` 字面量文本、有媒体节点 DOM 且时间戳非 1970。
- [ ] **F2** 未过期 `sdkfileid` 媒体字节落到 `MediaFile` 后，前端占位符变为可看媒体（图片可加载 / 视频可播）。
  - 证据：媒体节点 `access_url` 可用、资源 200。
- [ ] **F3** **降级（关键）**：过期 `sdkfileid` 下载返回 `sdk_error`（`media_download.py:633`）时，前端仍显示媒体节点 + 占位符 + 原始 `md5sum`/`filesize` 元信息，**不回退 raw-JSON、不崩溃**。
  - 证据：mock `sdk_error`，断言前端渲染为媒体占位符而非 JSON 字符串，无异常。

### 全局契约 / 回归
- [ ] **C1** 全库其它 `msgtype` 零回归：在**完整依赖环境**跑 `test_structured_message_parser.py` / `test_media_download.py` / `test_decrypt_worker.py`（或等价套件）**全绿**。
  - ⚠️ 在缺 `sqlalchemy`/`pydantic_settings` 的**最小沙箱**，`app.media_download` import 会失败、相关测试误报 FAIL——非本 ticket 回归，须在完整 backend 环境验证。
- [ ] **C2** 单测覆盖 K1–K6；新增测试命名清晰、可独立运行。

---

## 三、已知限制（验收时如实记录，非缺陷）

- WeCom 媒体下载 `sdkfileid` 令牌**会过期**。历史消息的 `sdkfileid` 取自**重新隔离解密得到的明文信封**（不是 `decrypted_payload`——该列生产上恒为 NULL），极旧消息其 `sdkfileid` 可能已失效，下载返回 `sdk_error` → 媒体字节无法恢复（WeCom API 限制）。K3/F3 已覆盖降级；若发现大量历史媒体因此无法恢复，在验收报告中标注比例与影响范围，供用户决策是否需其它补偿手段（如前端提示「历史媒体已过期」）。
- 重解析**会重新隔离解密**加密信封（`raw_encrypted_payload`/`encrypt_random_key`/`encrypt_chat_msg`）以重建 `structured_content`；`decrypted_payload` 列仍保持 SF-1 约束**不被填充**（与 RND-243 解析层一致）。
- **SIGSEGV / 隔离（RND-208 / RND-231）**：重解析重跑了隔离解密子进程，单条 SIGSEGV 被 `_decrypt_message` 折叠成 `SIGSEGV_SENTINEL`（ret != 0）并跳过该行、继续整批——隔离已上线，不会中断批量作业。RND-208 §5 显示该崩溃与 msgtype/输入内容无关，重解密这几十行不会比重解密任何其它历史行更危险；今早一度观察到的实时链路 100% 失败现象经复核生产 `decrypt_status` 分布（success 已到 id≈3945、无持续增长失败积压）显示目前已不再出现。验收如发现重解密某行 `ret != 0`（含 sigsegv），确认其被正确计数跳过、其余行不受影响即可。

---

## 四、环境要求

- 必须在**完整 backend 依赖环境**验收（含 `sqlalchemy`/`pydantic_settings`/`psycopg2`），否则 `app.media_download` 相关断言无法运行。
- 端到端（F1–F3）需可用 DB 与（mock 或部分真实）WeCom 下载桩；下载 `sdk_error` 用 mock 即可。
- 验收前确认 RND-243 解析修复已在本环境生效（否则 F1 时间戳/媒体节点仍异常）。

---

## 五、验收结论模板

```
RND-257 验收：PASS / FAIL
- K1–K8：各 PASS/FAIL + 证据
- F1–F3：各 PASS/FAIL + 证据
- C1–C2：各 PASS/FAIL + 证据
- 过期令牌影响比例（如有）：__
- 结论与遗留项：__
```
验收通过后由用户决定是否 commit/push；agent 绝不自行提交。
