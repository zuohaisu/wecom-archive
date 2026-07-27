# RND-231 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-231）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。
> Linear：RND-231「实现 DecryptData SIGSEGV 防御性兜底：单条消息进程隔离 + 修正输入校验」（父票 RND-201，来源调查 RND-208 已 Done）。

---

## 一、任务（一句话）

libWeWorkFinanceSdk_C.so v20240606 的 `DecryptData` 存在内部缺陷（RND-208 已确认根因），会以 SIGSEGV（exit 139）杀死整个 Python 进程。本票做**防御性兜底**：把单条消息的 SDK 解密调用隔离进子进程 + 对真正畸形的输入做前置校验，让「一条坏消息终止整批 backfill」的运维风险彻底消失。**这不是根因修复**——SDK 不可替换、缺陷不在我方可控范围。

---

## 二、必读背景（先读完再动手）

1. `docs/RND-208-decryptdata-sigsegv.md` — 根因调查报告全文，尤其：
   - §2 崩溃入口定位：SIGSEGV 的**单一 chokepoint** 是 `_decrypt_message` → `wecom_sdk.decrypt_data`；
   - §7 单条消息进程隔离设计（本票实现的直接蓝图）；
   - §5 未解答问题：生产实时链路 3,541 行（含 3,538 条 ver=4）解密成功，但隔离 harness 中**所有** ver=4 行均崩——疑似与 SDK 实例生命周期 / 调用频率 / 进程环境有关（见「六、跟踪项」）。
2. `DEV_AGENT_RULES.md` 的 Architecture Boundaries 一节 + `backend/tests/test_architecture_boundary.py`（CI 硬门禁，失败即 hard stop）。

### ⚠️ 重要纠正（本票与 RND-208 报告 §8 建议 #2 的冲突处，以本票为准）

RND-208 报告曾建议「`encrypt_key` 长度 < 32 字节时拒绝」——**这是错的，不要实现**。
`encrypt_key` 长度 88 是 SDK 内部协议的**正常格式**，生产 3,538 条 ver=4 行正是以 88 字节 key 解密成功的。任何基于长度阈值的拒绝都会把正常数据全部拒掉、制造新故障。**校验只拦真正畸形输入（见四、路 B），正常 88 字节 key 必须放行。**

---

## 三、精确落点（附文件:行号）

```
scripts/decrypt_wecom_messages_once.py        # re-export _decrypt_message
        └─ backend/app/services/decrypt_worker.py   _decrypt_message()   ← 唯一落点
                └─ backend/app/sdk/wecom_sdk.py     decrypt_data()       ← C 边界，SIGSEGV 发生地
        └─ backend/app/services/decrypt_isolation.py（新增，隔离机制本体）
```

- 调用方 1：`decrypt_worker.py` 的 `run_decrypt_once()` 主循环（新增 `lib_path` 形参向下透传）；
- 调用方 2：`backend/scripts/backfill_revoke_associations_once.py` 的 `recover_historical_revoke_structured_content`（经 `scripts/decrypt_wecom_messages_once.py` 的 re-export 导入 `_decrypt_message`）。
- **隔离层做在 `decrypt_worker.py` 的 `_decrypt_message` 内部（委托给 `app/services/decrypt_isolation.py` 的 `decrypt_message_isolated`）**，保持对外签名 `(lib, encrypt_key, encrypt_msg, sdk=_default_sdk, lib_path=None) -> tuple[int, str | None]` 不变。
- **关于两个脚本的改动（重要修正）**：本票原设想「脚本零 diff 即受益」是**错误**的——若不把 `lib_path` 从 `WECOM_SDK_LIB_PATH` 透传到 `_decrypt_message` / `run_decrypt_once` / `recover_...`，隔离代码就成了**永不触发的死代码**，生产毫无保护。因此两个脚本**允许且必须**做最小化改动：①从 env 取 `lib_path`；②透传进解密调用；③新增 sigsegv/隔离_other/malformed_input 可观测计数与打印。**禁止**改动的是脚本的解密调用**语义/业务逻辑**（`_decrypt_message` 仍是唯一调用点、outcome 分类不变）。验收见 QA 提示词 S1（已重定义为「diff 仅限于 lib_path 接线 + 可观测计数，且必须真正接通」）。
- 注意 `sdk=` 参数是 **in-process 路径**的测试注入点（`backend/tests/fakes.py`）；隔离路径在子进程内按 `lib_path` 重新 `load_sdk`，因此 fake sdk 仍只在 `lib_path=None` 的既有测试中使用，行为不变。

---

## 四、实现要求（两路）

### 路 A — 单条消息进程隔离（核心）

1. **子进程执行**：每次 `_decrypt_message` 的 SDK 调用（`new_slice → decrypt_data → get_slice_len → get_content_from_slice → free_slice`）在**独立子进程**中完成。使用 `multiprocessing.get_context("spawn")`（**不要 fork**）：
   - **必须用 spawn 而非 fork**：父进程是长驻 DB 连接（SQLAlchemy engine/session）的进程，fork 会把这些**非 fork-safe** 的连接与 SDK 全局状态一并继承进子进程，是经典隐患；spawn 生成全新子进程，在子进程内**重新 `load_sdk(lib_path)`** 并 `configure_sdk_decrypt_data`，天然不继承父进程的 DB/SDK 状态。
   - 密钥/密文以 `Process(args=...)` 形式传入——spawn 下 args 经 pickle 走**内存管道**，**绝不进 argv、`ps` 不可见**；**禁止**任何把 `encrypt_key`/`encrypt_msg`/明文塞进命令行参数或环境变量的代码。
   - 结果经 `Pipe` 回传 `(outcome, return_code, decrypted_json, detail)`；解密后的明文只在内存管道中流转，**绝不落盘、绝不写日志**；`detail` 仅含安全诊断（如 `signal 11` / 异常类型名），**绝不含密钥/明文/密文/完整密文**。
   - 子进程内**不得**使用任何 SQLAlchemy session / DB 连接；正常 `return` 即可（spawn 子进程无共享父状态需清理），无需 `os._exit()`。
   - 可选运维逃生门：环境变量开关（如 `WECOM_DECRYPT_ISOLATION=0` 关闭隔离、走旧 in-process 路径），默认**开启**隔离。
2. **父进程结果分类**（对齐票面口径）：
   - `success` — 子进程正常返回且 `ret == 0`；
   - `sdk_decrypt_failed` — 子进程正常返回但 `ret != 0`（记录 SDK 返回码，沿用现有 `summary.return_codes`）；
   - `sigsegv` — 子进程被信号杀死（`exitcode == -signal.SIGSEGV`，即 shell 视角 exit 139）；
   - 其他异常 / 超时 — 子进程超时（建议 15s，join 超时后 kill）或其它非零退出，归入独立 outcome。
   - **任何一类都只标记该条 outcome，然后继续下一条——绝不终止整批。**
3. **可观测性**：为 sigsegv / 其它崩溃 / 前置校验拦截 定义**模块级 sentinel 返回码常量**（现有惯例：-1 = missing envelope、-2 = alloc failed；新增如 `SIGSEGV_SENTINEL = -11`、`OTHER_SENTINEL = -3`、`MALFORMED_INPUT_SENTINEL = -4`，写成命名常量并 docstring 注明；数值以实现为准，只需互异且为负）。这样：
   - `run_decrypt_once` 的 `summary.return_codes` 自动计入各类计数；
   - 可在 `DecryptRunSummary` 上追加显式 `sigsegv` / `isolation_other` / `malformed_input` 字段（改 `decrypt_worker.py` 属 app 层，允许）；
   - sigsegv 行标记 `decrypt_status="failed"`，与现有失败路径一致，保证后续可安全重跑。
   - `backfill_revoke_associations_once.py` 的 `recover_historical_revoke_structured_content` 对 `ret != 0` 统一归入 `sdk_decrypt_failed`/`sigsegv` 并且不动该行——sentinel 走同一分支即为正确行为。

### 路 B — 输入前置校验（只拦真正畸形）

在进入子进程 / SDK **之前**校验 `encrypt_key` 与 `encrypt_msg`，命中即抛 `MalformedDecryptInput`（不调用 SDK）：

- 含内嵌 NUL（`"\x00"`）——`c_char_p` 会在 NUL 处静默截断，SDK 看到的是残缺输入；
- 空串 / 非 str 类型；
- 超长（防御性上限要**宽松**，如 `encrypt_msg > 50 MB`、`encrypt_key > 4 KB`——生产观测 `encrypt_msg` 跨度 438–20,380 字符、`encrypt_key` 88 字节，上限须留出几个数量级余量）；
- 注意：`encrypt_msg` 的字符集校验若引入，须谨慎避免误伤正常 base64 密文；优先只拦上述明确畸形项。

**明令禁止**：
- ❌ 任何基于 `len(encrypt_key)` 阈值（如 <32、!=32、!=88）的拒绝；
- ❌ 88 字节 key 被任何新校验路径拒绝（专门写正向测试锁死）。

---

## 五、测试（RED → GREEN）

新建 `backend/tests/test_decrypt_isolation.py`，至少覆盖：

1. **隔离机制（无真实 .so 可测）**：用 `os.kill(os.getpid(), signal.SIGSEGV)` 在一次性子进程中复现 RND-208 观测到的 exit 139 形状——`run_isolated` 返回 `exitcode == -11`；`decrypt_message_isolated` 返回 `outcome == "sigsegv"` 而非抛错/挂起。
2. 其它信号区分：子进程 `SIGABRT` → `exitcode == -6`，**不**误标为 sigsegv。
3. 超时：子进程 `sleep` 超阈值 → 超时 outcome，父进程在超时后 promptly kill 子进程并继续。
4. 成功路径：子进程正常发回 `("success", 0, '{"ok": true}', "")` → `run_isolated` 原样返回。
5. 前置校验拒绝项：空 key/空 msg/内嵌 NUL/非 str/超长 → 抛 `MalformedDecryptInput`，**且未 spawn 子进程**（在缺 lib 场景下表现为同步异常而非 outcome="other"）。
6. **88 字节 key 放行（V4 锁死）**：`validate_decrypt_inputs("k" * 88, "some-encrypted-message-blob")` 不抛异常；并附 `test_accepts_short_encrypt_key`（`"k" * 16`）证明**完全无长度下界**。
7. sentinel 互异且为负：`{SIGSEGV_SENTINEL, OTHER_SENTINEL, MALFORMED_INPUT_SENTINEL}` 三值互异、均 < 0。
8. 集成：monkeypatch `_decrypt_child_target` 为崩溃函数，确认 `decrypt_message_isolated` 经子进程崩溃后报告 `outcome="sigsegv"`。

回归（必须全绿）：`make verify`；重点 `test_decrypt_wecom_messages_once_cli.py`、`test_backfill_historical_revoke_recovery.py`、`test_architecture_boundary.py`、`test_http_contract`（路由数 = 33 不变——本票根本不该碰任何路由）。

---

## 六、跟踪项：长驻 SDK 实例二次复现（已降级为用户侧可选）

RND-208 未解答项：**为什么生产实时链路 ver=4 全部成功、隔离 harness 全部 SIGSEGV？**（疑似 SDK 实例生命周期 / 调用频率 / 进程环境差异。）

本票**不强制**交付独立上机 harness：上述 §五 的确定性 CI 测试已用 `os.kill(SIGSEGV)` 在一次性子进程中精确复现 exit 139 信号形状，等价于 harness 的「崩」分支，且无需真实 .so、可在 CI 跑。如确需真实上机对照长驻实例，由**用户/运维 agent** 在 `WECOM_SDK_LIB_PATH` 可用的生产环境运行，结论由用户回填 Linear RND-231——不属于本票自动验收范围。

---

## 七、硬约束（违反即判失败）

- ❌ 两个脚本的**解密调用语义/业务逻辑**不得改动：`_decrypt_message` 仍是其唯一解密调用点，`recover_historical_revoke_structured_content` 的 outcome 分类不变。允许且必须的最小改动仅限：从 env 取 `lib_path`、透传进解密调用、新增 sigsegv/isolation_other/malformed_input 可观测计数与打印（`git diff` 的 scope 受限于此，详见 QA 提示词 S1）。
- ❌ 不改 schema / 不新增 Alembic migration；不碰实时同步链路、前端、路由（路由数 33 不变）。
- ❌ 不替换 / 不升级 SDK；不开启不受控 core dump。
- ❌ 不做任何基于密钥长度阈值的拒绝；88 字节 key 必须放行。
- ❌ 任何日志 / 异常消息 / 测试夹具不得包含真实私钥、随机密钥明文、聊天明文或完整密文。
- ❌ 不使用 `fork`（必须用 `spawn`）；不把密钥/密文放进 argv 或环境变量。
- ❌ 不执行 git commit / push。
- ✅ 遵守 `DEV_AGENT_RULES.md` Architecture Boundaries（`test_architecture_boundary.py` 必须绿）；`_decrypt_message` 对外签名与成功路径语义保持不变。

---

## 八、收尾（交付物）

向用户交付：
1. RED→GREEN 证据：全部新用例 + `make verify` 全绿日志。
2. 改动文件清单（预期：`backend/app/services/decrypt_worker.py`、`backend/app/services/decrypt_isolation.py`（新增）、`backend/tests/test_decrypt_isolation.py`（新增）、及两个 scripts 的最小 `lib_path` 接线/计数改动；**不含**路由/前端/schema）。
3. sentinel 返回码对照表（-1/-2 既有，-11/-3/-4 新增，以实现常量名为准）与运维观测方式说明（`return_codes` / `summary.sigsegv` 等如何读出 sigsegv 计数）。
4. 说明：两个脚本的 diff 仅限 `lib_path` 接线 + 计数，已真正接通（非死代码）。
5. 注明：未提交，待 QA agent 验收 + 用户 Review 后由用户自行 commit。
