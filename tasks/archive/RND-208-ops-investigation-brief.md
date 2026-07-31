# RND-208 运维上机调查派工 Brief

> **目标**：在生产服务器（持有 `libWeWorkFinanceSdk_C.so` 与官方 `.h` 的环境）上，收口 RND-208「历史 revoke 消息 `DecryptData` 崩溃（exit 139 / SIGSEGV）」的**根因责任边界**。
> **来源报告**：`docs/RND-208-decryptdata-sigsegv.md`（调查-only 报告，本地因缺 SDK 二进制/.h 未能完成 §6 复现与 backtrace）。
> **本 brief 性质**：只调查、只出结论；**不实现修复**（修复另立 ticket，见末尾）。
> **敏感级别**：最高。这是会话存档产品，解密过程与 core dump 可能含真实聊天明文、密钥、完整密文。

---

## 0. 授权范围与红线（必读）

你**被授权**在生产服务器上做**只读 recon** 与**隔离容器内的受控复现**。以下为硬红线，**任何情况下不得跨越**：

### ❌ 绝对禁止
- 在**生产实时进程**上开启**不受控 core dump**（RND-208 非目标明确禁止）。
- 拿**生产实时解密数据流 / 实时 backfill 进程**跑崩溃 harness（会真实触发"一条坏消息终止整批回填"）。
- 导出、记录或上传任何含**聊天明文、私钥、随机密钥明文、完整密文、密文哈希、SDK SHA-256** 的内容。
- 修改 RND-201 已上线 schema、实时 revoke 处理、前端行为。
- 在生产直接替换 SDK 版本。
- 实现任何代码修复（本 brief 只调查；修复走独立 ticket + 分支 + PR）。

### ⚠️ 必须隔离 / 脱敏
- 触发 SIGSEGV 的复现**只能在同架构隔离容器**中进行，容器持有 SDK 二进制但只处理**脱敏样本**。
- 若必须取 core dump：仅在隔离容器内、限权目录、`ulimit -c` 受控；core 文件**不上传、不入库、不离开受控环境**；如需分析，只提取栈帧文本，绝不搬运原始 core。
- 优先使用 **faulthandler**（进程内、可控）直接拿栈，替代系统级 core dump。

### ✅ 推荐更稳方案（如可行）
把 `libWeWorkFinanceSdk_C.so` + 官方 `.h` **从服务器拷贝出来**，放进一个隔离沙箱，配**脱敏样本**复现——完全不碰生产数据，比"在服务器上查"更安全。本 brief 以下步骤在沙箱内同样适用。

---

## 1. 任务拆解（建议顺序）

### 阶段 A：只读 Recon（低风险，直接在生产服务器做）
目标：补齐 RND-208 §6.2 / §6.3，确认 ABI/架构/依赖与 FFI 签名合同。

1. **定位 SDK 二进制**
   - 在运行解密的环境里，打印 `load_sdk` 实际解析到的 `libWeWorkFinanceSdk_C.so` 绝对路径。
   - `sha256sum` 记录 SDK 哈希（**仅本地记录，不入库、不上报原文到票据**）。
2. **ABI / 架构 / 依赖 / 加载路径核查**
   ```bash
   uname -m                                  # 架构
   ldd libWeWorkFinanceSdk_C.so              # 动态依赖是否齐全
   readelf -h libWeWorkFinanceSdk_C.so       # ELF 头：字长/ABI
   ldd --version 2>&1 | head -1              # glibc 版本
   ```
3. **FFI 与官方头文件逐项核对（§6.3）**
   - 在服务器定位官方 `WeWorkFinanceSdk_C.h`。
   - 逐项比对 `app/sdk/wecom_sdk.py` 中每个符号的 `argtypes` / `restype` / 64 位指针类型：
     `NewSdk` / `Init` / `DestroySdk` / `NewSlice` / `FreeSlice` / `GetSliceLen` / `GetContentFromSlice` / **`DecryptData`**（重点确认是否为**三参无 handle** 变体）。
   - 确认 `Slice_t` 结构体布局与 `NewSlice`/`FreeSlice` 分配/释放语义是否一致。
   - 输出：每个符号「匹配 / 不一致（具体字段）」。

### 阶段 B：隔离容器内受控复现（§6.1 / §6.6）
目标：稳定复现 SIGSEGV 并拿 native backtrace，定位崩溃栈顶归属。

1. **脱敏样本导出（从生产，仅取定位字段）**
   - 只导出 31 条历史 revoke 行的：`seq` / `msgid` / `publickeyver`、密文长度、密文哈希、`encrypt_key` 长度与哈希。
   - **绝不**导出明文、私钥、随机密钥明文、完整密文。
   - 把这些定位字段带入隔离沙箱，在沙箱内用**同版本 SDK + 对应样本**重建可复现输入（如无法直接重建密文，退而用§6.4 最小 C 复现验证 SDK 行为）。
2. **最小 ctypes harness（新建于隔离容器）**
   - 仅 `NewSdk` / `Init` / `DecryptData` 调用。
   - **成功样本**（近期消息）作同环境对照；**失败样本**（历史 revoke）复现 SIGSEGV。
   - 每条记录 outcome：成功 / SDK 返回错误码 / SIGSEGV（退出码 139）/ 其他异常。
3. **Native backtrace**
   ```bash
   # 隔离容器、限权目录
   ulimit -c unlimited
   python scripts/diagnose_decrypt_crash.py   # 仅失败样本，退出 139
   gdb -c core.<pid> $(which python) -ex "bt" -ex "quit"
   # 或优先 faulthandler（进程内、可控、无需 core）：
   #   import faulthandler; faulthandler.enable(); faulthandler.dump_traceback_later(...)
   ```
   - 确认崩溃栈顶在 **SDK 内部（输入解析/拷贝）** 还是 **ctypes 边界**。
4. **跨版本 / 跨环境矩阵（§6.5，若另一 SDK 版本可得）**
   | 组合 | 成功样本 | 失败样本 |
   |---|---|---|
   | 当前 SDK + 当前环境 | 对照 | 复现目标 |
   | 另一可用 SDK 版本 + 当前环境 | 对照 | 判定是否版本相关（H4） |
   | 当前 SDK + 干净容器 | 对照 | 排除环境污染（H6） |

---

## 2. 假设排序（调查时对照验证）
| # | 假设 | 验证手段 |
|---|---|---|
| H1 | 历史密文过大/旧格式触发 SDK 内部越界 | backtrace 栈顶位置 + 样本密文长度分布 |
| H2 | `encrypt_key`（RSA 解出）长度/编码不符 | 对照 SDK 期望密钥长度（通常 32 字节 AES） |
| H3 | `publickey_ver` 通过守卫但密钥已轮换 | 取样 31 行 `publickey_ver` 与当前值、密钥可用性 |
| H4 | SDK 对特定历史输入的固有缺陷 | 跨 SDK 版本矩阵（§6.5） |
| H5 | Python FFI 签名/指针类型错误 | 阶段 A 头文件核对收口 |
| H6 | 环境/ABI/架构不匹配 | 阶段 A `ldd`/`readelf`/`uname`/glibc 核查 |

---

## 3. 交付物（上报要求）
- 一份结论报告，至少覆盖 RND-208 §9 验收项：
  - 固定样本是否稳定复现 / 证伪
  - 成功样本同环境对照结果
  - FFI 与官方头文件逐项核对结果（匹配 / 具体不一致）
  - SDK 二进制/架构/依赖/加载路径核查结果
  - native backtrace 栈顶归属（SDK 内部 vs ctypes 边界）
  - **责任边界判定**：FFI/环境/输入或密钥版本/SDK 缺陷/版本缺陷 之一
  - 后续处理建议（指向具体修复方向）
- **所有输出不得含敏感内容**（明文/密钥/完整密文/密文哈希/SDK SHA-256 原文）。
- 把结论回填到 **RND-208**（评论或更新描述），并标注「根因核查完成 / 仍待 X」。
- 若判定需代码修复，在汇报中明确指出应落地的修复方向（前置校验 / 进程隔离 / SDK 升级评估），但**不要自行实现**。

---

## 4. 非目标（本项目不做的）
- 不实现历史 backfill
- 不在生产替换 SDK
- 不修改 RND-201 已上线 schema / 实时 revoke 处理 / 前端
- 不在生产开启不受控 core dump
- 不实现任何代码修复（修复将由独立 ticket + 分支 + PR 承接）

---

## 5. 与修复 ticket 的关系（给 PM 参考，运维 agent 无需实现）
RND-208 报告 §7 / §3 / §8 已设计两项防御性修复，应由**独立 ticket** 承接（非本 brief 范围）：
1. **单条消息进程隔离**：`_decrypt_message` 外包隔离层，单条消息在子进程执行，失败/SIGSEGV 只记 outcome 继续下一条。
2. **输入前置校验**：拒绝含 NUL、超长、密钥长度不符的样本，明确 outcome 跳过，不进 `DecryptData`。

这两项不依赖根因确认，可立即消弭"整批回填被一条坏消息终止"的运维风险。
