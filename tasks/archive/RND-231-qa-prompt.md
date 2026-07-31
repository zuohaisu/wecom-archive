# RND-231 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「Codex 验收」角色）。
> 你**只读、不改实现、不 commit/push**。验收通过后由用户决定是否提交。
> 验收对象：开发 agent 按 `rnd-231-execution-prompt.md` 产出的工作树改动。
> 背景必读：`docs/RND-208-decryptdata-sigsegv.md`（根因：SDK DecryptData 内部缺陷，ver=4 行 SIGSEGV / exit 139）。

---

## 一、验收目标

确认 RND-231「DecryptData SIGSEGV 防御性兜底」达成，且零回归：
- 单条消息 SDK 解密已隔离进**子进程**（spawn）；一条 SIGSEGV 只记 outcome，**不终止整批**；
- 输入前置校验只拦真正畸形输入（内嵌 NUL / 超长 / 非 str），**正常 88 字节 encrypt_key 绝不被拒**；
- 两个脚本（`decrypt_wecom_messages_once.py` / `backfill_revoke_associations_once.py`）**仅做 lib_path 接线 + 可观测计数**（解密调用语义不变），且隔离在生产中**真正接通**（非死代码）；
- sigsegv 可观测（计数进 summary / return_codes）；
- SIGSEGV 复现已由确定性 CI 测试闭环（长驻实例上机对照降级为用户侧可选跟踪项）。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 隔离（核心）
- [ ] **I1** 隔离层落点在 `backend/app/services/decrypt_worker.py` 的 `_decrypt_message`（委托给 `app/services/decrypt_isolation.py` 的 `decrypt_message_isolated`），对外签名 `(lib, encrypt_key, encrypt_msg, sdk=..., lib_path=None) -> tuple[int, str | None]` 保持兼容（新增 `lib_path` 可选形参，默认 `None` 走旧 in-process 路径）。
  - 证据：读源码；`grep -rn "_decrypt_message" backend/` 确认两个脚本的导入路径未变（仅新增 `lib_path` 透传）。
- [ ] **I2** SDK 调用链（`new_slice → decrypt_data → get_slice_len → get_content_from_slice → free_slice`）确实在**子进程**中执行，且子进程**不继承父进程的 DB session / SQLAlchemy engine / SDK 状态**（用 `spawn`，非 `fork`）。
  - 证据：读 `decrypt_isolation.py` 确认 `get_context("spawn")`、子进程内重新 `load_sdk(lib_path)`、`configure_sdk_decrypt_data`、结果经 `Pipe` 回传、`detail` 不含密钥/明文/密文；确认无 `fork`、无 `os._exit` 依赖。
- [ ] **I3** 子进程 SIGSEGV（`exitcode == -11`）→ 父进程存活，返回 sigsegv sentinel（实现常量 `SIGSEGV_SENTINEL`），行标记 `failed`，**继续处理下一条**。
  - 证据：跑 `backend/tests/test_decrypt_isolation.py` 的 `test_run_isolated_classifies_sigsegv`（exitcode == -11）、`test_decrypt_message_isolated_classifies_real_sigsegv_via_child_crash`（outcome == "sigsegv"）；确认崩行 failed、其它行 success。
- [ ] **I4** 子进程超时有兜底（超时 kill + 独立 outcome），父进程继续。
  - 证据：`test_run_isolated_times_out_and_kills_child` 绿（子进程被 promptly kill，未等到 sleep 结束）。
- [ ] **I5** 成功路径逐字节等价：隔离开启下正常样本 `(ret, decrypted_str)` 与旧 in-process 行为一致，含非 ASCII round-trip（由 `test_run_isolated_returns_child_result_on_success` 等佐证机制）。
- [ ] **I6** 密钥/密文**不经命令行参数**传入子进程（`ps` 不可见）；解密明文只在内存管道流转，不落盘不进日志。
  - 证据：读源码逐项确认；检索改动中不得出现向 argv/环境变量塞 `encrypt_key`/`encrypt_msg`/明文的代码。

### 输入校验（⚠️ 本票最大风险点）
- [ ] **V1** 含内嵌 NUL 的 key/msg 被前置拦截（抛 `MalformedDecryptInput`），且 SDK **未被调用**（缺 lib 场景下表现为同步异常）。
- [ ] **V2** 超长上限宽松（`encrypt_msg` 上限 ≥ MB 级；生产观测跨度 438–20,380 字符须远低于阈值）。
- [ ] **V3 【一票否决】** 不存在**任何**基于 `encrypt_key` 长度阈值的拒绝逻辑（<32 / !=32 / !=88 等一律 FAIL）。
  - 证据：通读 `validate_decrypt_inputs` 全部分支；`grep -n "32\|len("` 改动文件并逐处解释。
- [ ] **V4 【一票否决】** 88 字节 key 正向放行用例存在且绿（`test_accepts_normal_88_byte_encrypt_key` 中 `"k" * 88` 进入校验通过），且另有 `test_accepts_short_encrypt_key` 证明**完全无长度下界**。
  - 证据：跑上述两测试绿。

### 脚本改动范围（⚠️ 已修正：原「零 diff」一票否决作废）
> 原设想「两个脚本零 diff」是**错误**的：若不把 `lib_path` 从 env 透传到解密调用，隔离层就是永不触发的死代码。正确要求是「diff 仅限接线 + 计数，且必须真正接通」。
- [ ] **S1 【一票否决】** `git diff` 中两个脚本的改动**仅限于**：①从 env 取 `WECOM_SDK_LIB_PATH` 得 `lib_path`；②将其透传进 `_decrypt_message` / `run_decrypt_once` / `recover_historical_revoke_structured_content`；③新增 `summary.sigsegv` / `isolation_other` / `malformed_input` 计数与对应打印。**不存在**对解密调用**语义/业务逻辑**的改动（`_decrypt_message` 仍为唯一调用点、outcome 分类不变）。
  - 反向一票否决：若两个脚本**完全没有 diff**，则 `lib_path` 未被透传 → 隔离在生产中是死代码 → **同样 FAIL**（必须真正接通）。
  - 证据：`git diff backend/scripts/decrypt_wecom_messages_once.py backend/scripts/backfill_revoke_associations_once.py` 逐行核对；确认 `main()` 中 `lib_path = _require_env("WECOM_SDK_LIB_PATH")` 且传入 `run_decrypt_once(lib_path=lib_path)`；backfill 中 `lib_path` 透传至 `recover_...`。
- [ ] **S2** `test_decrypt_wecom_messages_once_cli.py`、`test_backfill_historical_revoke_recovery.py` 全绿（证明 re-export 与 CLI 语义未破，接线未引入回归）。

### 可观测性
- [ ] **O1** sigsegv / isolation_other / malformed_input 有命名 sentinel 常量与 docstring；`run_decrypt_once` 的 `summary.return_codes`（及 `summary.sigsegv` / `isolation_other` / `malformed_input` 字段）可读出 sigsegv 计数。
- [ ] **O2** sigsegv 行落库 `decrypt_status="failed"` —— 后续可安全重跑（pending/failed 扫描仍会捞回它）。

### SIGSEGV 复现（跟踪项）
> RND-208 未解答项（长驻实例 vs harness 全部 SIGSEGV）已通过**确定性 CI 测试**闭环，无需独立上机 harness。
- [ ] **H1** 确定性复现存在且绿：`backend/tests/test_decrypt_isolation.py` 中 `_crash_with_sigsegv` / `test_run_isolated_classifies_sigsegv`（exitcode == -11）/ `test_decrypt_message_isolated_classifies_real_sigsegv_via_child_crash` —— 用 `os.kill(getpid(), SIGSEGV)` 在一次性子进程中复现 RND-208 观测到的 exit 139 形状，无需真实 .so。
- [ ] **H2** 复现测试输出/异常**绝不含**密钥、明文、密文（仅 signal N / 异常类型名）；该测试未被任何生产定时器/worker 引用，纯测试代码。
- [ ] **跟踪项（用户侧）**：RND-208「长驻 SDK 实例」未解答项，由用户决定是否需要真实上机对照（交付 `decrypt_isolation` 机制后通常已无需）；结论由用户回填 Linear，不在本票自动验收范围。

### 全局契约
- [ ] **C1** 无 schema / migration 改动（`git diff` 无 `alembic` / `models.py` 变更，除非纯注释）。
- [ ] **C2** 路由数不变（`test_http_contract` = 33）；未触碰实时链路 / 前端。
- [ ] **C3** `test_architecture_boundary.py` 绿（隔离层未引入违规依赖方向）。
- [ ] **C4** `make verify` 全绿。
- [ ] **C5** 所有新增日志/异常/测试夹具无敏感数据（私钥、随机密钥明文、聊天明文、完整密文）。

---

## 三、回归套件（必须全绿）

```
make verify
```
重点确认（任一失败即 FAIL，附失败栈）：
- `backend/tests/test_decrypt_isolation.py`（新增）
- `test_decrypt_wecom_messages_once_cli.py`
- `test_backfill_historical_revoke_recovery.py`
- `test_architecture_boundary.py`
- `test_http_contract`（路由数 = 33）

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码有 Bug** → 反馈给开发 agent 修复，附具体错误 + 失败测试名 + 期望行为。**不自行改实现**。
- **测试代码有 Bug** → 仅当测试断言明显违背本票口径（如断言了长度阈值拒绝）时可自行修正测试，须在报告标注依据。
- **全部通过** → 报告 SUCCESS。

最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮回归验证；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-231 验收结论：PASS / FAIL
一票否决项：V3 __ / V4 __ / S1 __（S1 已重定义为「diff 仅限于接线+计数，且已真正接通」）
隔离：I1–I6 __（附证据要点）
校验：V1–V4 __
可观测：O1–O2 __
复现：H1–H2 __
契约：C1–C5 __
回归：make verify __（绿/红，附失败项）
遗留：__（若有；含「长驻实例二次复现」用户侧跟踪项状态）
```
