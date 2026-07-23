# 全系统缺陷巡检报告（2026-07-24）

- **范围**：`backend/app`、`backend/scripts`（抽样 `backend/tests`）
- **方式**：静态聚焦巡检（未运行、未修改代码）
- **目的**：列出**明显、有具体行号证据**且**需要修复**的缺陷，按严重度排序，供后续立项。非穷尽审计。
- **总体评价**：代码整体防御性较好——ORM 查询均参数化、token 缓存有锁、媒体下载二进制安全（`string_at` + `finally` 释放）、敏感字段日志脱敏到位。以下为仍需处理的少数明显缺陷。

---

## 严重度汇总

| # | 位置 | 问题 | 严重度 |
|---|---|---|---|
| 1 | `scripts/sync_wecom_archive_once.py:221-225` | `GetChatData` 解析失败被静默吞掉，整批数据被丢弃且脚本伪“成功”退出 | **中** |
| 2 | `app/qiniu_storage.py:259` | `httpx.get` 响应未关闭，常驻服务连接/套接字泄漏 | **中** |
| 3 | `scripts/sync_wecom_archive_once.py` / `download_wecom_media_once.py` / `decrypt_wecom_messages_once.py` | SDK `destroy_sdk`/`free_slice` 不在 `try/finally`，错误路径泄漏原生句柄 | 低 |
| 4 | `app/sdk/wecom_sdk.py:88-89` | `GetContentFromSlice` 用 `c_char_p` 读取 | 信息项 / 低风险（待头文件核对，见下） |
| 5 | `scripts/bootstrap_default_tenant.py:95,165` | f-string 直拼 SQL 标识符 | 低 |
| 6 | `app/auth.py:184-230` | token 缓存 check-then-act 半加锁，缓存惊群 | 低/信息 |

---

## 1. [中] `sync_wecom_archive_once.py:221-225` — 静默吞掉 JSON 解析异常，导致不可见的数据丢失

`GetChatData` 返回 `0` 后读取切片内容并 `json.loads(raw)`，但解析失败仅 `pass`，`records` 保持空列表：

```python
try:
    parsed = json.loads(raw)
    records = parsed.get("chatdata", [])
except (json.JSONDecodeError, ValueError):
    pass  # records stays empty
```

后续 `if records:`（L271）为假 → 不 commit、不更新 seq 游标（`new_seq = prev_seq`，L280），且脚本最终打印 `[PASS]` 并以 `sys.exit(0)` 退出。

**后果**：一旦 SDK 返回的数据被截断/损坏（典型如 §4 的边界场景或网络半包），整批拉取结果被**静默丢弃**，操作员只看到 `record_count: 0 / inserted: 0 / [PASS]`，误以为“无新消息”。更糟的是游标不前进 → 下一轮仍从同一 `prev_seq` 拉取，**反复空转**。这与 `decrypt_wecom_messages_once.py:557-561`（解析失败计为 `failed` 并对外可见）处理不一致。

**建议**：解析失败时至少记录 `WARN` 并返回非 0 退出，把“解析失败”与“无记录”区分开；不要因异常而让游标停滞。

---

## 2. [中] `app/qiniu_storage.py:259` — `httpx.get` 响应未关闭，常驻服务连接泄漏

```python
resp = httpx.get(signed_url, timeout=self._timeout)
...
return resp.content
```

读取 `resp.content` 不会释放底层连接；既不在 `with httpx.get(...) as resp:` 中，也无 `resp.close()`。这是**常驻 FastAPI 服务**的媒体读取热路径，每个请求都留下一个未显式关闭的连接/套接字，直到 GC 回收。高并发下可能耗尽连接池/文件描述符。同仓库其它网络调用（`wecom_contacts.py:42`、`auth.py:203`、`routers/auth.py:478`）均用 `with httpx.Client(...) as client:` 或 `with httpx.get(...) as resp:`，此处风格不一致。

**建议**：用 `with httpx.get(signed_url, timeout=...) as resp:` 包裹，或复用 `httpx.Client` 连接池；确保响应归还。

---

## 3. [低] 多个 `*_once.py` 脚本未在 `try/finally` 中释放 SDK 句柄

- `sync_wecom_archive_once.py:282-290`（`free_slice` + `destroy_sdk`）位于主逻辑 `with Session` 块**之后**，无 `finally` 保护；若 `session.commit()`（L272）或内层 `update_session.commit()`（L278）抛异常，清理被跳过。
- `download_wecom_media_once.py:447-450`、`decrypt_wecom_messages_once.py:653` 同理。

`backfill_revoke_associations_once.py:378-382` 已正确用 `try/finally: destroy_sdk(...)`，说明代码库存在正确范式。对一次性脚本危害有限（进程退出由 OS 回收），但若脚本被嵌入长驻/循环进程会持续泄漏原生句柄。

**建议**：将所有 `new_slice/new_sdk` 之后的逻辑包进 `try: ... finally: free_slice(...); destroy_sdk(...)`。

---

## 4. [信息项 / 低风险] `app/sdk/wecom_sdk.py:88-89` — `GetContentFromSlice` 的 `restype=c_char_p`

子代理曾将其列为“NUL 截断隐患”。本巡检的**初步判断（非最终定性，待收口）**：技术上大概率不是缺陷——这符合企业微信会话存档 SDK 官方 demo 的惯常读法，即 `GetContentFromSlice` 返回 SDK 写入并 NUL 结尾的文本，`GetChatData`/`DecryptData` 的输出走这条路径是标准做法；且“该路径**不是** RND-208 崩溃源”这一子结论**站得住**：`_decrypt_message` 中 `decrypt_data()` 先执行，若其在 `lib.DecryptData(...)` 内部崩溃，`get_content_from_slice` 根本不会被调用（`decrypt_wecom_messages_once.py:158-171`，已核对代码顺序）。

**为何不直接“盖棺定论为非缺陷”**：RND-208 报告已明确“本地无官方 `.h`，FFI 最终以隔离环境头文件核对收口”（§0、§3）。本巡检不应在没有同一份头文件的情况下给出确定性结论——该条须与 `DecryptData` 一并纳入 §6.3 头文件核对后再定性。

**交叉引用（与原巡检 #1 联动）**：这条读取路径在 `sync_wecom_archive_once.py` 中被用于解析 `GetChatData` 的原始输出（`wecom_sdk.py:219` → `sync_wecom_archive_once.py:219-225`）。若 SDK 返回的原始数据损坏/截断并恰好嵌入 NUL 字节，`c_char_p` 会**静默截断**而非报错；截断后的内容大概率 `json.loads` 失败——而这正好喂给原巡检 **#1** 的“静默吞异常”。两处叠加会放大数据不可见丢失的风险。

**可选加固（不阻塞，建议纳入后续 RND）**：仿照同文件媒体二进制路径（`GetData` + `GetSliceLen` + `ctypes.string_at`，`wecom_sdk.py:222-296`），将此处也改为 `restype = c_void_p` 后用 `ctypes.string_at(ptr, lib.GetSliceLen(slice_ptr))` 拷贝，彻底消除 NUL 截断这一理论风险，与二进制路径保持一致的防御姿态。

---

## 5. [低] `scripts/bootstrap_default_tenant.py:95,165` — f-string 直拼 SQL 标识符

```python
text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")
text(f"UPDATE {table} SET tenant_id = :tid WHERE tenant_id IS NULL")
```

当前 `table`/`column` 来自脚本内部硬编码常量（非外部输入），实际不可被利用（Bandit 已标 `# noqa: S608`）；但若将来改为接受参数即有注入风险，且与本仓库其余位置一律使用 `:param` 绑定参数的风格不一致。

**建议**：用白名单校验 `table`/`column`，或改用 SQLAlchemy 元数据反射（`Table(...).c[column]`）。

---

## 6. [低/信息] `app/auth.py:184-230` — token 缓存 check-then-act 半加锁

加锁读缓存（L196-199）→ 释放锁 → 锁外做 `httpx` 网络请求（L203）→ 再单独加锁写回（L226-227）。缓存失效时两个并发请求都会穿透到 WeCom 取 token，属缓存惊群（thundering herd）。

**后果**：良性——两个请求都拿到有效 token、最后一次写覆盖，一般无正确性后果（仅多一次 WeCom 调用，可能触发限流）。因 `_token_lock` 已存在，这种“半加锁”写法易被误读为线程安全。

**建议**：保持“锁内检查 → 锁外请求 → 锁内二次检查/写入”的单飞（single-flight）模式，避免持锁做网络 IO。

---

## 巡检未发现的（供背景，非缺陷）

- **SQL 注入**：搜索路由 `app/routers/search.py` 用 ORM `ilike` + 绑定参数，`_escape_ilike_pattern` 已转义 `%`/`_`；核心查询均参数化。未发现用户可控拼 SQL 的可利用点。
- **FFI 生命周期**：`iter_media_chunks` 每轮 `finally` 中 `free_media_data`，`get_media_data_bytes` 释放前用 `string_at` 拷出；介质写入路径稳健。
- **并发共享状态**：服务器内 SDK handle 不在请求线程间共享（仅脚本单线程使用）；`_token_cache`/`_state_store` 均有锁保护；`INTERNAL_STRUCTURED_FIELD_KEYS` 为不可变 `frozenset`。
- **敏感信息**：密钥/令牌/签名 URL 的日志脱敏到位。

---

## 建议优先级

1. **先修 #1（sync 静默吞异常）**：直接影响归档数据完整性，且可能让同步游标停滞形成死循环——最该优先。
   - 评级说明：#1 标为“中”而非“高”的理由——其触发条件是**数据损坏/截断这一非常态路径**（常态同步成功时不触发），且单批丢失不会立即破坏已落库数据；但其最坏表现（无限静默空转 + 操作员零可见性，仅见 `[PASS]`）确有“高”的潜质，故列为本巡检中优先级最高、最该先修的项。是否上调为“高”可在实施 RND 时据实际监控再定。
2. **其次 #2（qiniu 连接泄漏）**：常驻服务高并发下的资源泄漏，迟早触发 fd 耗尽。
3. **#3（SDK 释放 finally）**：一致性修复，低风险，顺手做。
4. **#5 / #6**：低优先，代码健康度改进。

> 本巡检为静态、聚焦性质，不替代完整安全/性能审计；上述各项如需落地，建议各自独立分支 + PR，不在本巡检报告中直接修改。
