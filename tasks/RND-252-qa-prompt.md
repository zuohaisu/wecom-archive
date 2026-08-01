[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-252 的 7 条 AC（重点验证复用 get_wecom_token/QiniuStorageProvider、cache_key 显式传递、测试不发真实请求）并产出带证据的 PASS/FAIL 判定。

# RND-252 验收提示词（Acceptance / QA Prompt）— 配置中心 T7

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-252「配置中心 T7：连通性自检」｜风险等级 R1

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。

## 验收方法（证据优先）

### AC-1 — 三项自检均可用
- 判定：`target=qiniu`/`wecom`/`domain` 各自返回 `{"ok", "reason"}` = PASS。

### AC-2 — 复用而非重写（关键）
- 证据：`grep -n "get_wecom_token\|QiniuStorageProvider" backend/app/config/validation.py` 均有命中；审阅确认无自造 HTTP 调用七牛/企微 API 的代码。
- 判定：真复用 = PASS。**平行实现 → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-3 — `cache_key` 显式传递（关键，RND-312 同款教训）
- 证据：`grep -n "get_wecom_token(" backend/app/config/validation.py` 核对 `cache_key` 参数是否显式传入。
- 判定：显式传入 = PASS。**省略 → FAIL（`IMPLEMENTATION_DEFECT`, major）**——会复用生产共享缓存导致自检失真。

### AC-4 — 零泄露
- 判定：`reason` 字段脱敏、不含明文密钥/原始异常 = PASS；否则 FAIL（`SECURITY_VIOLATION`）。

### AC-5 — 不接受临时凭据
- 证据：请求体 schema 只含 `target`，不含密钥字段；validation 函数入参来自 `resolve()`。
- 判定：符合 = PASS。

### AC-6 — 测试无真实网络请求
- 证据：审阅测试文件，确认 `get_wecom_token`/`QiniuStorageProvider` 均被 mock。
- 判定：全部 mock = PASS；发现真实网络调用 → 记 major finding。

### AC-7 — 契约同步 + 回归
- 判定：`test_http_contract.py`（route_count 当前基线 +1）已同步 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`（不是 `BLOCKED_NEEDS_HUMAN`，PM 已授权）。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查
1. **未改 `get_wecom_token`/`QiniuStorageProvider`**：`git diff --stat -- backend/app/auth.py backend/app/qiniu_storage.py` 应无输出。
2. **文件所有权**：`git status --porcelain` 改动应限于 `config/validation.py`（新）、`routers/settings.py`（追加）、`tests/test_rnd252_connectivity_check.py`（新）、`tests/test_http_contract.py`（契约同步）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd252_connectivity_check.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "get_wecom_token\|QiniuStorageProvider" backend/app/config/validation.py
git diff --stat -- backend/app/auth.py backend/app/qiniu_storage.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-252-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不放松 AC。
- **AC-3 若 `cache_key` 缺失 → 直接 FAIL（major）**。
