# RND-208 调查报告：WeCom SDK `DecryptData` 历史消息解密崩溃（exit 139 / SIGSEGV）

- **Issue**：RND-208（父任务 RND-201）
- **状态**：Backlog / P3，调查-only（非目标：不改 RND-201 schema、不碰实时链路/前端、不在生产换 SDK、不开 uncontrolled core dump）
- **本报告性质**：静态审计 + 复现方法论 + 假设排序 + 后续建议。**未修改任何代码**。
- **敏感数据**：本报告不记录私钥、随机密钥明文、聊天明文、完整密文、密文哈希、SDK SHA-256。

---

## 0. 执行方式与本地限制（重要前提）

本调查在 `main` 分支以**调查-only**方式执行。调查过程中：

- 复现 harness、崩溃实验**只规划、未运行**；且按约定只应在隔离环境/`/tmp` 中运行，**绝不提交、绝不修改生产解密脚本**（`scripts/decrypt_wecom_messages_once.py`、`app/sdk/wecom_sdk.py`）。
- 本地环境**不具备**复现条件：仓库与标准路径（`/usr/local/lib`、`/opt`）均**不存在** `libWeWorkFinanceSdk_C.so`，也**无官方 `.h` 头文件**。因此：
  - 动态复现（固定失败样本触发 SIGSEGV）、GDB backtrace、**头文件逐项核对**必须在持有 SDK 二进制的**隔离环境**完成。
  - 本报告对 FFI 的核对以**代码静态审查 + 公开 SDK 签名惯例**为准；最终以隔离环境拿到 `.h` 后的逐项比对收口（见 §3、§6）。

> 结论先行：鉴于“近期消息可正常解密、仅历史消息在进入 `DecryptData` 时崩溃”，**FFI 签名本身是正确合同的概率很高**（否则近期消息同样会崩或失败），崩溃**极大概率由历史样本的特定输入/密钥版本触发**，其次为 SDK 对特定历史输入的健壮性缺陷。最终责任边界以隔离环境的 native backtrace 为准。
>
> 推理局限（务必注意）：上述“近期消息可解密 ⇒ FFI/ABI 概率低”**并非“已排除”**。ctypes 签名或结构体布局不匹配引发的未定义行为往往是**尺寸/形状相关**的——小缓冲区不越界、特定大尺寸/历史格式才触发越界。因此“近期消息成功”只能说明常见路径合同基本正确，**不能完全排除**仅对某些历史消息尺寸/格式触发的 FFI 层 UB。H5/H6 标为“低”而非“已排除”，最终仍以 §6.3 头文件核对 + §6.6 native backtrace 收口。

---

## 1. 崩溃现象还原（来自 RND-208 描述）

| 项 | 观察 |
|---|---|
| 退出码 | 139（= 128 + 11，SIGSEGV） |
| SDK | `libWeWorkFinanceSdk_C.so` v20240606 |
| RSA 解密随机密钥 | 成功 |
| SDK 初始化 `Init` | 成功 |
| 崩溃位置 | 历史加密消息进入 `DecryptData` 时 |
| 受影响范围 | 历史 `revoke` 消息约 31 条 |
| 实时链路（RND-201 已上线） | 不受影响 |

关键推论：**崩溃发生在 `DecryptData` 调用内部，且 `(RSA成功) + (Init成功) + (近期消息可解密)` 说明调用约定与运行环境整体可用**，问题被“历史消息”这一维度特异性触发。

---

## 2. 崩溃入口定位（代码静态审计）

`DecryptData` 经 Python 包一层的唯一调用路径：

```
scripts/decrypt_wecom_messages_once.py::_decrypt_message()
        └─ app/sdk/wecom_sdk.py::decrypt_data()
                └─ lib.DecryptData(encrypt_key, encrypt_msg, slice_ptr)   # C 原生
```

**存在两个业务入口共用同一 helper `_decrypt_message`**：

1. `scripts/decrypt_wecom_messages_once.py`（主解密回填脚本）
   - `main()` 遍历 `decrypt_status IN ('pending','failed')`，逐条 `_decrypt_message(lib, encrypt_key, encrypt_msg)`。
2. `scripts/backfill_revoke_associations_once.py`（历史 revoke 关联恢复，RND-201）
   - `recover_historical_revoke_structured_content()` 第 304 行 `_decrypt_message(lib, encrypt_key, row.encrypt_chat_msg)`。
   - 该脚本正是为恢复“历史 revoke 消息”而存在，**与 RND-208 描述的 ~31 条历史 revoke 直接相关**。

> **落点结论**：SIGSEGV 的单一 chokepoint 是 `_decrypt_message` → `wecom_sdk.decrypt_data`。任何输入前置校验或“单条消息进程隔离”都应落在 `_decrypt_message`（共享 helper），这样两个脚本同时受益；不要在各自调用点分别修补。

两个入口均已有守卫（在调用 `_decrypt_message` 之前）：
- `publickey_ver != expected_pubkey_ver` → 标记 `failed`/`key_mismatch`，**不进入 `DecryptData`**（主脚本 L532；backfill L295）。
- `encrypt_random_key` / `encrypt_chat_msg` 缺失 → 标记 `failed`/`missing_envelope`，不进入 `DecryptData`（主脚本 L525；backfill L297）。

> 注意：密钥版本守卫**只比对版本整数，不校验密钥本身有效性**。若某历史行的 `publickey_ver` 与当前一致（通过守卫）但其密文实际由已轮换的密钥加密，SDK 将拿到不匹配的 `encrypt_key` → 未定义行为，可能即表现为 SIGSEGV。这是 §5 的 H3。

---

## 3. Python FFI 静态核对表（`app/sdk/wecom_sdk.py`）

| 符号 | 代码中 argtypes / restype | 评估 | 待隔离环境确认 |
|---|---|---|---|
| `NewSdk` | `[]` → `c_void_p` | OK | — |
| `Init` | `[c_void_p, c_char_p, c_char_p]` → `c_int` | OK | — |
| `DestroySdk` | `[c_void_p]` → `None` | OK | — |
| `NewSlice` | `[]` → `c_void_p` | OK | — |
| `FreeSlice` | `[c_void_p]` → `None` | OK（在 `finally` 中释放，生命周期正确） | — |
| `GetSliceLen` | `[c_void_p]` → `c_int` | OK | — |
| `GetContentFromSlice` | `[c_void_p]` → `c_char_p` | 解密结果以 NUL 结尾的 JSON，读取正确；非崩溃源 | — |
| **`DecryptData`** | `[c_char_p, c_char_p, c_void_p]` → `c_int` | **无 SDK handle 的三参变体**，与“部署版 v20240606 且近期消息可解密”一致；**近期消息可解密即证明签名合同正确** | 需在隔离环境用官方 `.h` 逐项比对（见 §6） |

**关于 `c_char_p` 入参的健壮性提醒（建议纳入后续修复，非本票实现）**：

- `encrypt_key` / `encrypt_msg` 以 `c_char_p` 传入，ctypes 会在末尾补 NUL。若历史样本中存在**内嵌 NUL 字节**（密文损坏/格式异常），C 侧将只收到截断到首个 NUL 的缓冲区 → 可能触发越界读取/写入。
- 建议后续在 `_decrypt_message` 中对 `encrypt_msg` / `encrypt_key` 做前置校验：拒绝含 NUL、长度超上限、非预期字符集的输入，并以明确 outcome（skip）替代调用 SDK。这能把“坏样本”与“SIGSEGV”解耦。

---

## 4. 解密管线静态审计要点

- **Slice 生命周期**：每次调用 `new_slice` 分配、`finally` 中 `free_slice` 释放，**不存在跨调用复用** → 排除“Slice 复用/悬垂指针”作为崩溃源。
- **线程共享**：两个脚本均为单线程顺序执行，无并发调用 SDK → 排除线程共享/allocator 竞争（仍列入 §6 核查清单，因 SDK 内部未必线程安全，后续若有并发调用需重评）。
- **调用顺序**：`_decrypt_message` 先 `decrypt_data`（可能崩），再 `get_slice_len`/`get_content_from_slice`（崩在前者之内，后者不会被执行）。崩溃即发生在 `lib.DecryptData(...)` 这一行。
- **异常不可捕获**：Python `try/except` **无法捕获原生 SIGSEGV**（进程直接被杀，退出码 139）。因此“单条消息进程隔离”（§7）是防止一条坏样本终止整批回填的关键运维修复。

---

## 5. 假设排序（按可能性）

| # | 假设 | 可能性 | 支持证据 | 反驳/待确认 |
|---|---|---|---|---|
| H1 | 历史 `encrypt_chat_msg` 过大/为旧版密文格式，触发 SDK 内部缓冲区越界 | **高** | 仅历史消息崩；近期正常；SDK 对特定历史 payload 健壮性差属已知类别 | 需 backtrace 定位栈顶在 SDK 解析/拷贝逻辑 |
| H2 | 历史 `encrypt_key`（RSA 解密后）长度/编码与 SDK 预期不符 | **高** | 密钥由 RSA 解出后直接传入；历史密钥可能异于当前 | 需对照 SDK 期望密钥长度（通常 32 字节 AES 密钥） |
| H3 | `publickey_ver` 通过守卫但实际密钥已轮换 → SDK 拿到不匹配密钥 | **中** | 版本守卫只比整数不校验密钥有效性 | 需取样核对 31 行 `publickey_ver` 与当前值、及密钥实际可用性 |
| H4 | SDK 对特定历史输入的固有缺陷（缺陷类） | **中** | 企业微信会话存档 SDK 历史上存在对畸形输入 segfault 的报告 | 需跨 SDK 版本矩阵（§6 第 5 项）确认是否版本相关 |
| H5 | Python FFI 签名/指针类型错误 | **低** | 近期消息可正常解密 → 签名合同正确 | 仍需 `.h` 逐项核对收口 |
| H6 | 环境/ABI/架构不匹配（glibc、字长） | **低** | 同一环境下近期消息正常 | 仍需 `ldd`/`readelf -h`/`uname -m`/glibc 核查 |

**初步责任边界判定（待 backtrace 收敛）**：最可能为 **“历史输入 / 密钥版本问题”** 或 **“SDK 对特定历史输入的缺陷”** 二者之一——均与 RND-208 描述“仅历史消息崩溃、不影响实时链路”自洽。FFI 与环境/ABI 概率低（**但属“低”而非“已排除”，推理局限见 §0**）。

---

## 6. 隔离环境复现与核查步骤（待执行）

> 所有步骤在**隔离环境**（同架构干净容器）执行，避免直接在生产长期调试。core dump 含敏感信息，必须限权访问、不得上传未脱敏 core 文件。

### 6.1 复现（固定样本对照）
1. 从生产**仅导出 31 条历史 revoke 行的定位字段**（`seq`/`msgid`/`publickeyver`、密文长度、密文哈希、`encrypt_key` 长度与哈希、**不含明文/密钥/完整密文**），于隔离环境取回对应样本。
2. 用最小 ctypes harness 分别调用 `DecryptData`：
   - **成功样本**（近期消息）作为同环境对照；
   - **失败样本**（历史 revoke）复现 SIGSEGV。
3. 记录每条结果：成功 / SDK 返回错误码 / SIGSEGV（退出码 139）/ 其他异常。

### 6.2 ABI / 架构 / 依赖 / 加载路径核查
```bash
uname -m                                  # 架构
ldd libWeWorkFinanceSdk_C.so              # 动态依赖是否齐全
readelf -h libWeWorkFinanceSdk_C.so       # ELF 头：字长/ABI
ldd --version 2>&1 | head -1              # glibc 版本
sha256sum libWeWorkFinanceSdk_C.so        # 记录 SDK SHA-256（不入库）
# 实际加载路径：在 harness 中打印 load_sdk 解析到的绝对路径
```

### 6.3 FFI 与官方头文件逐项核对
- 取得与 v20240606 对应的官方 `WeWorkFinanceSdk_C.h`，逐项比对 §3 每个符号的 `argtypes`/`restype`/64 位指针类型。
- 重点确认 `DecryptData` 是否为三参（无 handle）变体，以及 `Slice_t` 结构体布局与 `NewSlice`/`FreeSlice` 的分配/释放语义。

### 6.4 最小原生 C 复现（可选， strongest evidence）
- 用官方 demo 的 C 代码，仅调用 `NewSdk/Init/DecryptData`，喂入失败样本，确认是否同样 SIGSEGV，排除 Python/cpython 中间层干扰。

### 6.5 跨版本 / 跨环境矩阵
| 组合 | 成功样本 | 失败样本 |
|---|---|---|
| 当前 SDK + 当前环境 | 对照 | 复现目标 |
| 另一可用 SDK 版本 + 当前环境 | 对照 | 判定是否版本相关（H4） |
| 当前 SDK + 干净容器/同架构隔离环境 | 对照 | 排除环境污染（H6） |

### 6.6 Native backtrace
```bash
# 隔离环境、限权目录
ulimit -c unlimited
python scripts/diagnose_decrypt_crash.py   # 仅跑失败样本，退出 139
gdb -c core.<pid> $(which python) -ex "bt" -ex "quit"
# 或用 faulthandler 在子进程中直接拿栈：
#   import faulthandler; faulthandler.enable(); faulthandler.dump_traceback_later(...)
```
- 确认崩溃栈顶位于 SDK 内部（输入解析/拷贝）还是 ctypes 边界。
- core 文件限权、不入库、不上传未脱敏版本。

> 注意：`scripts/diagnose_decrypt_crash.py` **当前不在仓库中**（已确认），并非已存在的文件——它只是本文档规划的步骤代号。需按 §6.1 的“最小 ctypes harness”设计，在隔离环境中**新建**：其职责是仅用失败样本调用 `DecryptData` 触发 SIGSEGV 并落 core，不入库、不记录敏感字段。交接时请勿误认为该脚本已就绪。

---

## 7. 单条消息进程隔离设计（后续修复落点，非本票实现）

满足 RND-208 验收项“单条消息进程隔离”，且直接消弭“一条坏样本终止整批回填”的运维风险：

- 将 `_decrypt_message` 的执行移入**子进程**（推荐 `multiprocessing` 独立进程或 `subprocess` 调用最小 worker）。
- 父进程按 `msgid` 收集每条结果：
  - 成功 → 正常归一化；
  - SDK 返回非 0 → 记录 `sdk_decrypt_failed`（含返回码，不记录密文）；
  - **子进程退出码 139 / 负 SIGSEGV** → 记录 `sigsegv` outcome，**继续下一条**；
  - 其他异常 → 记录并继续。
- **落点**：在共享 helper `_decrypt_message` 外包隔离层（或新增 `_decrypt_message_isolated`），两脚本零改动即可受益。
- 该修复若实施，应作为**独立 RND / 独立分支 + PR**（违反“非目标”则不在本票范围）；本票仅给出设计。

---

## 8. 后续处理建议与历史 backfill 恢复方案

1. **先收口责任边界**：在隔离环境完成 §6.1–6.6，以 backtrace 确认栈顶归属（SDK 内部 vs ctypes 边界）。
2. **若判定为输入/密钥版本问题（H1/H2/H3）**：
   - 在 `_decrypt_message` 增加前置校验（长度上限、内嵌 NUL 拒绝、`encrypt_key` 长度断言）；不合规样本以明确 outcome 跳过，不再进入 `DecryptData`。
   - 核对 31 行 `publickey_ver` 与密钥可用性；对密钥已轮换的历史行，走“密钥版本不匹配”显式分类（而非崩溃）。
   - 配合 §7 进程隔离，重跑 `decrypt_wecom_messages_once.py` 与 `backfill_revoke_associations_once.py` 恢复历史 backfill。
3. **若判定为 SDK 缺陷（H4）**：
   - 用 §6.5 矩阵确认是否特定 SDK 版本问题；若官方有更新版本且修复该输入，评估在隔离环境升级 SDK（仍属“非目标”中的“不在生产替换 SDK”——需另行立项审批）。
   - 在官方修复前，以 §7 进程隔离 + 前置校验作为兜底，避免整批终止。
4. **历史 backfill 恢复顺序建议**：先以进程隔离 + 前置校验跑主解密脚本消化 `pending/failed`，再跑 `backfill_revoke_associations_once.py` 恢复 revoke 关联；每条失败样本单独留痕，便于后续定点处理。
5. **监控**：回填脚本已有安全运营指标输出（成功/失败/返回码分布），新增 `sigsegv` 计数后可观测坏样本规模。

---

## 9. 验收标准对照

| RND-208 验收项 | 本报告覆盖情况 |
|---|---|
| 固定样本稳定复现 / 证伪 | 待隔离环境（本地无 .so）— §6.1 |
| 成功样本同环境对照 | 待隔离环境 — §6.1 |
| Python FFI 与官方头文件逐项核对 | 静态核对完成（§3）；头文件逐项待隔离环境收口（§6.3） |
| SDK 二进制/架构/依赖/加载路径核查 | 命令已列（§6.2），待部署/隔离环境执行 |
| 最小原生 C/C++ 调用结果明确 | 方法已给（§6.4），待隔离环境 |
| native backtrace / 等价证据 | 方法已给（§6.6），待隔离环境 |
| 区分责任边界（FFI/环境/输入/SDK 缺陷/版本缺陷） | 初步判定见 §5（历史输入或 SDK 缺陷概率高），最终待 backtrace |
| 后续处理建议 + 历史 backfill 恢复方案 | 已给（§8） |
| 日志/附件不含敏感内容 | 本报告遵守；harness 设计须同样遵守（§0、§6） |

---

## 10. 非目标重申

本票**不**：执行历史 backfill、在生产替换 SDK、修改 RND-201 已上线 schema/实时 revoke 处理/前端、在生产开启不受控 core dump。若调查指向代码修复，须另立 RND 走分支 + PR。
