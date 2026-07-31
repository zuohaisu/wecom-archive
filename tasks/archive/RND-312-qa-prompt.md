[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-312 的 7 条 AC（重点验证复用 get_wecom_token、失败信息不泄露、测试不发真实企微请求）并产出带证据的 PASS/FAIL 判定。

# RND-312 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## ⚠️ 2026-07-31：本版新增 AC-8（缓存隔离），因第一版 dev prompt 的指令曾经写反

第一版 dev prompt 曾指示"不传 `cache_key`"，后核实这会导致自检复用生产路径共享的 corp_id 缓存（最长 ~2 小时），已反转为"必须传 `cache_key=f"connectivity-check:{tenant_id}"`"。若你验收到的实现仍是不传 `cache_key`，按 AC-8 判 FAIL，不要因为它"能跑通 mock 测试"就放过。

## 任务身份
- 工单：RND-312「B2-2 连通性自检」｜风险等级 R1

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 自检可用
- 证据：测试 mock `get_wecom_token` 返回假 token → 端点返回 `{"ok": true}`。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 失败分类
- 证据：测试 mock `get_wecom_token` 抛 `RuntimeError` → 端点返回 `{"ok": false, "reason": ...}`，HTTP 状态码不是 500。
- 判定：不 500 且有合理分类 = PASS。

### AC-3 — 零泄露（关键）
- 证据：响应体断言不含 `app_secret`/`decrypted_app_secret`/`private_key_encrypted` 字样；`reason` 字段应是**粗粒度分类**（如 `invalid_credentials`/`network_error`/`unknown`），不是底层异常的原始 message 直接透传。
- 代码审阅：`except RuntimeError as e` 之后是否把 `str(e)` 原样塞进响应——若是，判定失败倾向（企微侧异常信息可能间接暴露诊断细节）。
- 判定：分类脱敏 + 无密钥泄露 = PASS。**响应中出现任何密钥值 → 直接 FAIL（`SECURITY_VIOLATION`, blocker）**；原始异常信息未脱敏透传 → 记 finding（`IMPLEMENTATION_DEFECT`, minor-major 视具体内容严重性）。

### AC-4 — 目标不存在 → 404
- 判定：符合 = PASS；否则 FAIL。

### AC-5 — 鉴权
- 判定：无凭据/错凭据 → 401 = PASS。

### AC-6 — 复用而非重写
- 证据：`grep -n "get_wecom_token" backend/app/routers/platform.py` 应有命中；确认没有另一份直接请求 `qyapi.weixin.qq.com` 的 HTTP 调用代码。
- 判定：真复用 = PASS。**平行实现一套企微 API 调用 → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-7 — 契约同步 + 回归
- 判定：`test_http_contract.py` 全绿 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过；`test_rnd311_tenant_provision.py` 全绿。

### AC-8 — 缓存隔离（关键，新增）
- 证据：`grep -n "get_wecom_token(" backend/app/routers/platform.py` 找到调用点，**核对第三个实参 `cache_key` 是否被显式传入**，值应形如 `f"connectivity-check:{tenant_id}"`（或功能等价的自检专属前缀），而不是省略该参数。
- 判定：显式传入自检专属 `cache_key` = PASS。**若调用时省略 `cache_key`（即让它默认落到 `corp_id`）→ 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**——这会让自检读到生产解密/同步路径共享的缓存 token（最长 ~2 小时内），可能在密钥已经损坏的情况下仍返回"连通性正常"，完全违背自检的目的。

## 本项目专属检查（必查）
1. **测试未发真实请求**：审阅 `test_rnd312_connectivity_check.py`，确认 `get_wecom_token` 被 mock（如 `unittest.mock.patch`），**没有**测试用例会真的打到 `qyapi.weixin.qq.com`。若发现真实网络调用 → FAIL（`IMPLEMENTATION_DEFECT`, major，且测试会在无网络环境下 flaky）。
2. **未改 `get_wecom_token`**：`git diff --stat -- backend/app/auth.py` 应无输出（本票只调用）。
3. **架构边界**：router 未 import `app.main`。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform.py`（仅新增自检端点）、schema 文件、`tests/test_rnd312_connectivity_check.py`（新）、`tests/test_http_contract.py`。
   > `platform.py` 是本波次共享文件（RND-307/310/313/314 也会改它）——先 `git status`/`git diff` 分离归因（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假 `corp_id`/`app_secret`（明显占位符，非真实企微凭据）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd312_connectivity_check.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "get_wecom_token\|qyapi.weixin.qq.com" backend/app/routers/platform.py backend/tests/test_rnd312_connectivity_check.py
git diff --stat -- backend/app/auth.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-312-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若响应含密钥值 → 直接 FAIL（blocker）**。
- **测试若发现真实网络请求 → 直接判 major finding**，不接受"测试环境里也能跑通"。
