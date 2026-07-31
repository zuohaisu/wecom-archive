[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-254 的 5 条 AC（重点验证端到端旅程真实串联、全项目回归零红、未改任何生产代码）并产出带证据的 PASS/FAIL 判定。

# RND-254 验收提示词（Acceptance / QA Prompt）— 配置中心 T11

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-254「配置中心 T11：测试」｜风险等级 R1

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。

## 验收方法（证据优先）

### AC-1 — 端到端集成测试真实覆盖完整旅程（关键）
- 证据：审阅 `test_rnd254_config_center_e2e.py`，确认测试是**单条连贯的流程**（未初始化 → bootstrap → 登录 → 读取配置 → 改配置 → 自检），而不是几个互不相关、各自独立 setup 的小测试拼凑在一起冒充"端到端"。
- 判定：真正连贯 = PASS。**若各步骤互相独立、没有验证"前一步的产出确实是后一步的输入"（例如 bootstrap 设置的密码真的被用来登录）→ FAIL（`INSUFFICIENT_TEST_COVERAGE`）**。

### AC-2 — 全项目回归零红
- 证据：`make verify` 全绿。
- 判定：符合 = PASS。

### AC-3 — `monkeypatch.setenv` 全项目扫描
- 证据：确认跑过 `backend/tests/` 全量（不只是配置中心相关文件），且全部通过。
- 判定：符合 = PASS。

### AC-4 — 未改生产代码（关键）
- 证据：`git diff --stat -- backend/app/` 中不含 `tests/` 之外的路径。
- 判定：符合 = PASS。**发现生产代码改动 → 直接 FAIL（`SCOPE_VIOLATION`, severity: blocker）**——本票明确是纯测试票，不该改生产代码。

### AC-5 — 回归
- 判定：`test_architecture_boundary.py` 通过 = PASS。

## 本项目专属检查
1. **文件所有权**：`git status --porcelain` 改动应仅限 `tests/test_rnd254_config_center_e2e.py`（新）。
2. **QA Summary 完整性**：应附完整的用户旅程测试执行记录（不是只写"PASS"两个字）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd254_config_center_e2e.py -q
.venv/bin/python -m pytest backend/tests/ -q
git diff --stat -- backend/app/
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-254-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不放松 AC。
- **AC-4 若发现生产代码被改动 → 直接 FAIL（blocker）**。
