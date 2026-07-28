# RND-257 开发 agent 执行提示词

> 面向开发 agent（按项目 DEV_AGENT_RULES 的「开发」角色）。
> 你**只产出代码改动 + 测试**，不自行 commit/push；改动经独立 QA agent 验收通过后，由用户决定是否合入。
> 本 ticket 是 **RND-243 的子任务（第二层收口）**：RND-243 的解析修复（chatrecord/mixed 内嵌媒体子消息 `ChatRecord`-前缀归一 + 子消息 `msgtime` 秒→毫秒）已部署，只修**新归档 / 重解析**消息；本 ticket 负责把**已归档历史** chatrecord/mixed 消息的结构化内容重解析正确，并尽量补全其内嵌媒体字节。

---

## 一、目标

让历史 `chatrecord` / `mixed` 消息在前端不再显示 raw-JSON 文本垃圾，而是规整的媒体节点（图片/视频/语音/文件/表情等）+ 正确时间戳；并尽量为其中**未过期**的 `sdkfileid` 内嵌媒体补全 `MediaFile` 字节。

交付物：
1. 一个幂等、可 `--dry-run` 的历史重解析作业（一次性脚本 `scripts/reparse_structured_content_once.py`，沿用仓库既有 `*_once.py` 约定），核心是**对每条候选行重新做隔离解密**拿到明文，再喂修复后的解析器。
2. 触发既有嵌套媒体下载扫描以补全字节（**复用 RND-200 管线，不新写下载逻辑**）。
3. 对应单元测试（重解密+重解析幂等、旧坏形态识别、只改 `structured_content` 的安全边界、SF-1 不被破坏）。

---

## 二、背景与已部署前提

- RND-243 解析修复已合并并进入 CI/CD。**新** chatrecord/mixed 消息解析正确。
- **历史**消息仍坏，两层原因：
  1. **结构化形态坏损**：旧解析器把内嵌媒体子消息存成 `text=原始JSON`（如 `{"md5sum":...,"filesize":902691,"play_length":6,"sdkfileid":"CtYB..."}`），不是媒体节点。
  2. **媒体字节从未下载**：旧解析器没把 `ChatRecordImage` 等登记进 `media_refs` → RND-200 的嵌套下载管线从未为这些内嵌媒体建 `MediaFile` 行。
- 本 ticket 不动 RND-243 已修的解析逻辑；只是**用修复后的解析器重跑历史数据**。

---

## 三、可行性依据（关键，已核实 — 注意与初版错误的「复用 decrypted_payload」前提相反）

> **⚠️ 修订说明（2026-07-27）**：本 ticket 原始可行性前提写错了。初版假设 `ArchiveMessage.decrypted_payload` 在 `decrypt_status='success'` 时持久保留、可直接喂 `parse_structured_content`。**这是错的。** 经开工前落点核实，代码里 `decrypted_payload` **从不**被持久化（下列证据），生产上基本恒为 NULL——这是刻意的数据最小化设计（SF-1），不是 bug。正确路径是**用始终保留的加密信封重新隔离解密**，再喂解析器。

证据（开工前已逐条核实，开发 agent 执行时请勿依赖已存 `decrypted_payload`）：
- `decrypt_worker.py:531` 注释明写 `do NOT persist full decrypted_payload — SF-1`；逐字段更新段 `531-542` 写入 `msgtype/sender/roomid/msgtime/tolist/sdkfileid/content_text/structured_content/decrypt_status`，**没有** `record.decrypted_payload =`。
- migration `0008_structured_message_content.py` 文档字符串（14-27 行）：`decrypted_payload` 有「documented, intentional constraint against populating … reversing that constraint was explicitly ruled out」。
- `test_decrypt_structured_content.py:200-207` 的 SF-1 回归测试：`assert "record.decrypted_payload" not in source`。
- `git log -S "record.decrypted_payload" -- backend/app` 返回空；生产 `app/` 内从无任何 `record.decrypted_payload =` 赋值（唯一 setter 在 `mock_ingest.py` 测试夹具，带 `_mock: True`）。

**正确可行性**：`ArchiveMessage` 的**加密信封字段始终保留**（`models.py:252-253` 文档字符串：「`raw_encrypted_payload` / `encrypt_random_key` / `encrypt_chat_msg` are always retained so decryption can be re-run after a key rotation」）。因此历史重解析**无损但需重解密**：

1. 对每条候选行，复用既有解密原语（与 `run_decrypt_once` 完全一致的解密链路）重新隔离解密信封 → 得到 `decrypted_str`；
2. `json.loads(decrypted_str)` → `decrypted` dict；
3. `normalised = parse_structured_content(msgtype, decrypted)`（`structured_message_parser.py:1253`）→ 重建正确 `structured_content`（含正确 `media_refs`、`sdkfileid`、归一后的规范 `type`）；
4. 仅写回 `record.structured_content` 单列。

> 相比初版（误以为可直接读 `decrypted_payload`），真实方案**多了一步 RSA 解密 + SDK 子进程调用**（即重跑 `run_decrypt_once` 的解密部分），但**完全复用既有、已上线的隔离解密原语**，不新写解密逻辑。

嵌套媒体下载管线已存在（RND-200，`media_download.build_nested_media_candidate_query` / `select_nested_media_candidates` / `download_one`），其输入正是 `structured_content.media_refs`。重解析写入正确 `media_refs` 后，**现有下载扫描会自动拾取这些 `sdkfileid`** 去补 `MediaFile`。所以本 ticket **不需要重写下载逻辑，只需触发它**。

---

## 四、精确落点（文件 / 函数 / 行）— 已按真实代码核实

| 关注点 | 位置 |
|---|---|
| **加密信封字段（始终保留 — 重解析数据源）** | `backend/app/db/models.py:327-329` `raw_encrypted_payload` / `encrypt_random_key` / `encrypt_chat_msg`；文档字符串 `:252-253` |
| `decrypted_payload` 列（**存在但生产恒为 NULL，SF-1 禁止填充**） | `backend/app/db/models.py:333` |
| `structured_content` 列 | `backend/app/db/models.py:340` |
| RSA 解密密钥 | `backend/app/services/decrypt_worker.py` `_rsa_decrypt_encrypt_key(private_key, encrypt_random_key)`（调用点见 `run_decrypt_once:485`） |
| **隔离解密入口（重解析必须走这条）** | `backend/app/services/decrypt_worker.py:114` `_decrypt_message(lib, encrypt_key, encrypt_msg, sdk=, lib_path=)` → 经 `lib_path` 走子进程 `decrypt_isolation.decrypt_message_isolated`（`backend/app/services/decrypt_isolation.py`） |
| 解密调用示范（run_decrypt_once 解密段） | `backend/app/services/decrypt_worker.py:466-515`（`encrypt_key_raw=record.encrypt_random_key`；`encrypt_msg=record.encrypt_chat_msg`；`encrypt_key=_rsa_decrypt_encrypt_key(...)`；`ret, decrypted_str=_decrypt_message(lib, encrypt_key, encrypt_msg, lib_path=...)`） |
| 解析入口 | `backend/app/structured_message_parser.py:1253` `parse_structured_content(msgtype, decrypted) -> Optional[dict]` |
| **写入形状（必须照搬）** | `backend/app/services/decrypt_worker.py:541` `record.structured_content = normalised["structured_content"]` |
| 嵌套消息类型集合 | `backend/app/media_download.py:96` `NESTED_MEDIA_MSGTYPES = frozenset({"mixed","chatrecord"})` |
| 提取 media_refs | `backend/app/media_download.py:329` `iter_nested_media_refs(structured_content)` |
| 候选查询 | `backend/app/media_download.py:363` `build_nested_media_candidate_query` |
| 候选选择 | `backend/app/media_download.py:396` `select_nested_media_candidates` |
| 单条下载 | `backend/app/media_download.py:568` `download_one(...)`；`:633` 返回 `sdk_error`（令牌过期） |
| 既有 `*_once.py` 约定 | `scripts/decrypt_wecom_messages_once.py` / `scripts/download_wecom_media_once.py`（用 Glob 确认实际路径与参数风格） |

> **注意**：`run_decrypt_once` 正常只扫描 `decrypt_status.in_(["pending","failed"])`（见 `:457`）。本回填脚本**必须扫描 `decrypt_status == "success"` 的历史行**（它们解密成功但 `structured_content` 坏），并重跑其解密段只取 `decrypted_str`，**绝不写回 `decrypt_status`**（安全边界，见下）。

**写入形状务必与 `decrypt_worker.py:541` 完全一致**：`parse_structured_content` 返回的**整个 dict**（含 `fields` / `raw` / `parse_warnings`，嵌套消息额外含 `media_refs`）直接赋给 `record.structured_content`。不要只存 `fields`，也不要改动 dict 键名——前端序列化只读 `fields`/`parse_warnings`，下载管线只读 `media_refs`。

---

## 五、实施步骤

### 步骤 1 — 识别「旧坏形态」候选行
扫描条件（全部满足）：
- `msgtype in ("chatrecord","mixed")`（`NESTED_MEDIA_MSGTYPES`）；
- `decrypt_status == "success"`（已成功解密的历史行；**不是** `pending`/`failed`）；
- **当前 `structured_content` 处于旧坏形态**，判定（满足任一即视为坏）：
  - `structured_content` 为 NULL；或
  - `structured_content.get("media_refs")` 缺失或为空列表；或
  - 递归遍历 `structured_content["fields"]["items"]` 任一子项 `item["text"]` 是字符串、可 JSON 解析、且解析结果含 `sdkfileid` 键（即旧「raw-JSON 回显」特征）。
- **不再要求 `decrypted_payload` 非空**（该列生产上恒为 NULL）；重解析数据来自加密信封字段。
- 默认按 `id` 升序分批遍历，支持 `--tenant`、`--since`/`--until`、`--limit`。

### 步骤 2 — 重解密 + 重解析写入（核心）
对每行：复用 `run_decrypt_once` 的解密原语（**不要**调用 `run_decrypt_once` 整体，因为它会改写 `decrypt_status`/多个列，违反安全边界）：
```python
encrypt_key = _rsa_decrypt_encrypt_key(private_key, record.encrypt_random_key)
ret, decrypted_str = _decrypt_message(
    lib, encrypt_key, record.encrypt_chat_msg, sdk=sdk, lib_path=lib_path
)
if ret == 0 and decrypted_str:
    decrypted = json.loads(decrypted_str)
    normalised = parse_structured_content(msgtype, decrypted)
    # normalised 即 parse_structured_content 的整返回值 dict
    record.structured_content = normalised   # 与 decrypt_worker.py:541 完全一致
# 仅此一列写入；不写 decrypted_payload / media_files / decrypt_status / 其它列
```
- **幂等**：重跑安全——已正确的行重解析结果相同；为保险可对比新旧 `media_refs` 是否变化，未变则跳过写。
- **安全边界（硬约束）**：事务内只 `UPDATE structured_content` 单列；不触碰 `decrypted_payload`（SF-1 仍绑定，本脚本**不得**填充它）、`media_files`、解密状态、收件人、其它字段。
- **SIGSEGV / 隔离（RND-208 / RND-231）**：`_decrypt_message` 在 `lib_path` 给定时走 `decrypt_isolation` 子进程，单次 SIGSEGV 被折叠成 `SIGSEGV_SENTINEL`（ret != 0），与任何其它解密失败走同一「跳过该行、继续下一条」分支（见 `decrypt_worker.py:498-511`）。重解密这几十行**不会比重解密任何其它历史行更危险**（RND-208 §5：该崩溃与 msgtype/输入内容无关，对所有可解密 ver=4 行稳定复现，与内容无关）；隔离已上线，单条崩溃被安全兜住、不中断整批。遇到 `ret != 0` 的行计数跳过即可。
- 分批提交（如每 200 行一 commit），失败行跳过并记日志，绝不中断整批。

### 步骤 3 — 触发下载补全（复用 RND-200 管线）
重解析提交后，运行既有嵌套媒体下载扫描（定位 `scripts/download_wecom_media_once.py` 或等价管理端 action），参数覆盖受影响 `tenant` / 时间范围，使 `select_nested_media_candidates` 拾取新登记的 `media_refs.sdkfileid` 并 `download_one` 补全 `MediaFile`。**不要新写下载代码**；若现有脚本不支持「仅嵌套消息」范围，按需加过滤参数（复用 `build_nested_media_candidate_query`）。

### 步骤 4 — 一次性脚本 `scripts/reparse_structured_content_once.py`
- 参数：`--tenant`（可空=全租户）、`--limit`、`--since`/`--until`、`--dry-run`（只统计、不写库）、`--download`（重解析后是否自动触发下载补全）、`--lib-path`（隔离解密子进程的 SDK 库路径，复用 `run_decrypt_once` 的同款参数）。
- 输出：重解析行数、跳过行数、检测到坏形态数、`media_refs` 新增数、重解密失败（含 sigsegv）行数；`--dry-run` 下额外打印抽样 JSON。
- 沿用既有 `*_once.py` 的 DB 连接、日志、信号处理、密钥/SDK 加载风格（Glob 参考 `scripts/decrypt_wecom_messages_once.py`）。

### 步骤 5 — 单元测试（`backend/tests/`，新建或并入 `test_structured_message_parser.py` / `test_media_download.py`）
- 用 RND-243 已落地的 mock 旧坏形态 `structured_content`（子项 `text=含sdkfileid的JSON`）喂入「重解析函数」，断言：
  - **重解析函数确实走了重解密路径**（mock `_decrypt_message` / `decrypt_message_isolated` 返回一段 chatrecord 明文信封，断言它被调用且返回值被 `parse_structured_content` 消费；**断言没有读 `record.decrypted_payload`**）；
  - 输出 `structured_content["media_refs"]` 含正确规范 `type` + `sdkfileid`；
  - `iter_nested_media_refs` 能提取到（`media_download.py:329`）；
  - 只改 `structured_content`，`decrypted_payload` **不变**（安全边界 + SF-1：断言重解析后 `record.decrypted_payload` 仍为原值/NULL，且无任何赋值）；
  - 幂等：同输入重跑结果稳定。

---

## 六、不要做（范围外 / 禁止）

- ❌ 不改动 RND-243 已修的解析逻辑、前端渲染、时间戳换算。
- ❌ 不新写媒体下载逻辑（复用 RND-200）。
- ❌ 不重解析 `chatrecord`/`mixed` 之外的 `msgtype`（不受 RND-243 影响）。
- ❌ **不读取、不填充 `decrypted_payload`**（SF-1 约束仍生效——该列生产上恒为 NULL 是有意为之；重解析数据一律来自加密信封 `raw_encrypted_payload`/`encrypt_random_key`/`encrypt_chat_msg`）。
- ❌ 不触碰 `media_files` 表、解密状态（即便重解密也只取 `decrypted_str`，不写 `decrypt_status`）、收件人。
- ❌ 不假设所有历史 `sdkfileid` 都可下载——WeCom 媒体令牌会过期，旧消息下载可能 `sdk_error`（`media_download.py:633`）。这属于 WeCom API 限制，不是缺陷；重解析后前端体验已显著改善（文本垃圾→媒体节点+占位符+原始元信息）。

---

## 七、与 RND-243 的关系

- 父：RND-243（解析层修复，已部署）。
- 本 ticket = 第二层：把修复**应用到历史数据**（通过重解密信封 + 重解析）。
- 关联：RND-200（嵌套媒体下载管线，复用）、RND-206（媒体预览）、RND-226（nested media 实体上下文）、RND-231（隔离解密，本回填重解密直接复用）、RND-208（SIGSEGV 根因，隔离已上线）。

---

## 八、验收衔接

完成后交独立 QA agent，依据 `.workbuddy/prompts/rnd-257-qa-prompt.md` 验收（重解密+重解析正确性、下载补全、零回归、安全边界含 SF-1、过期令牌降级）。
