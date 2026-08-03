[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-256 的 5 条 AC（重点验证密钥用固定占位符而非明文/部分掩码、未做导入、不落盘）并产出带证据的 PASS/FAIL 判定。

# RND-256 验收提示词（Acceptance / QA Prompt）— 配置中心 T10

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-256「配置中心 T10：导出 .env」｜风险等级 R1

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。

## 验收方法（证据优先）

### AC-1 — 导出格式正确
- 判定：每行 `SCREAMING_SNAKE_CASE=value`，覆盖 `CONFIG_REGISTRY` 全部字段 = PASS。

### AC-2 — 密钥占位（关键）
- 证据：测试写入已知密钥值，导出文本断言：① 该值不以任何形式（明文/掩码片段）出现；② 对应字段值固定为 `***`。
- 判定：符合 = PASS。**明文或部分掩码（如末4位）出现在导出文本中 → 直接 FAIL（`SECURITY_VIOLATION`, severity: blocker）**——导出文件常被复制传播（备份、分享给同事排查问题），比 UI 掩码的泄露面更大，必须完全占位。

### AC-3 — 未做导入（冻结设计 G1）
- 证据：`git diff -- backend/app/routers/settings.py`（本票范围）中不含"解析上传 .env 并写回配置"的逻辑。
- 判定：无导入逻辑 = PASS。**发现导入实现 → FAIL（`SCOPE_VIOLATION`, major）**——G1 冻结设计明确排除，属于范围蔓延。

### AC-4 — 不落盘
- 证据：本票改动范围内无 `open(..., 'w')`/文件写入调用。
- 判定：符合 = PASS。

### AC-5 — 契约同步 + 回归
- 判定：`test_http_contract.py`（route_count 当前基线 +1）已同步 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查
1. **未改 T5 既有端点**：`git diff` 中 `GET`/`PUT /settings` 逻辑不变，只新增 `GET /settings/export`。
2. **文件所有权**：`git status --porcelain` 改动限于 `routers/settings.py`（追加）、`web/static/settings.js`（追加按钮）、`tests/test_rnd256_settings_export.py`（新）、`tests/test_http_contract.py`（契约同步）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd256_settings_export.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/archive/RND-256-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不放松 AC。
- **AC-2 若密钥以任何形式泄露到导出文本 → 直接 FAIL（blocker）**。
