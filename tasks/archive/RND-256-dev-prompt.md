[Goal check] This work advances 开发（Development） by 交付配置的只读导出能力（复制为 .env 文本，敏感值占位），方便自托管用户备份/迁移配置。

# RND-256 开发提示词（Developer Prompt）— 配置中心 T10

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-256 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 2026-08-01 追加：契约测试同步已预先授权

本 epic 里 T5/T6/T7（<issue>RND-249</issue>/<issue>RND-250</issue>/<issue>RND-252</issue>）都因为遗漏 `test_http_contract.py` 的契约同步而触发过 `BLOCKED_NEEDS_HUMAN`——本票同样新增了 `GET /settings/export`，会踩到一样的规则（`docs/ticket-autopilot-workflow.md` §3.3），已提前把这个文件加进下方文件所有权清单，不要再触发同一个 escalation。

## 开工前必须先核实
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.config.schema import CONFIG_REGISTRY; from app.routers.settings import settings_router; print('OK')"
```
T5（<issue>RND-249</issue>）未就绪 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 任务身份
- 工单：RND-256「配置中心 T10：导入/导出 .env（敏感 `***` 占位）」｜父 Epic RND-244
- 优先级：Low（P2）｜风险等级：**R1**｜milestone：R2 · 开源发布闭环
- **冻结设计 G1：首期仅导出，导入写回本期不做（fast-follow），不要因为"顺手"就把导入也做了。**

## 背景与项目现状

导出是一个纯只读的格式转换：把 `CONFIG_REGISTRY` 全部字段的当前值（经 T4 `resolve()`），转成 `.env` 文件文本格式（`KEY=value` 逐行），密钥字段用 `***`（**固定占位符，不是 T2 的 `mask()` 末4位格式**——导出场景不需要"能反推出部分原文"的掩码，直接整体占位更安全，明确区分"UI 展示掩码"和"导出占位"这两个不同用途）。

**❗ 本项目高频踩坑：**
- **不做导入**（G1 冻结）：diff 中不应出现任何"读取 .env 并写回配置"的逻辑。
- 架构边界同前面各票。

## 目标（Goal）
交付一个只读导出端点，返回当前全部配置的 `.env` 格式文本，密钥用 `***` 占位。

## 范围边界

**In scope：**
1. `backend/app/routers/settings.py`（**追加**）：`GET /settings/export`，`require_role()`（同 T5 的临时鉴权占位，T6 落地后可统一替换成 `require_settings_admin`，本票不主动做这个替换，避免和 T6 抢改同一处）。响应 `Content-Type: text/plain`，body 是 `.env` 格式文本：每行 `KEY_UPPER_SNAKE=value`（**key 需要从 `CONFIG_REGISTRY` 的 snake_case 转成大写 SCREAMING_SNAKE_CASE，与真实 `.env` 文件的命名习惯一致**——如 `wecom_corp_id` → `WECOM_CORP_ID`），密钥字段值固定输出 `***`。
2. `backend/app/web/static/settings.js`（**扩展**，若 T9 已落地则在其基础上加；若尚未落地，本票只加一个独立的"复制为 .env"按钮 + 对应最小 JS，不依赖 T9 的其他交互逻辑）：调用导出端点，把返回文本复制到剪贴板（`navigator.clipboard.writeText`）。
3. 测试：`backend/tests/test_rnd256_settings_export.py`。

**Out of scope（显式非目标）：**
- **不做导入**（G1 冻结，明确排除）。
- 不做文件落盘（不写入服务器本地文件系统，纯 HTTP 响应体返回文本，由前端"复制"或浏览器"另存为"处理——这样自托管场景下不需要额外的文件系统写权限）。
- 不改 T5/T4/T3 的任何既有逻辑。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/settings.py` —— **仅追加** `GET /settings/export`
- `backend/app/web/static/settings.js` —— **仅追加**"复制为 .env"按钮相关逻辑
- `backend/tests/test_rnd256_settings_export.py`（新）
- `backend/tests/test_http_contract.py` —— **强制随附**（见 `docs/ticket-autopilot-workflow.md` §3.3）：`route_count` 读当前实际基线 +1、expected path 集合与 snapshot 追加 `GET /settings/export`

**只读、绝不可写：** `app/config/resolver.py`/`schema.py`（只调用）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 导出格式正确**：响应文本每行 `SCREAMING_SNAKE_CASE=value`，覆盖 `CONFIG_REGISTRY` 全部字段。
- **AC-2 密钥占位（关键）**：密钥字段值固定为 `***`，**绝不输出明文或 T2 的部分掩码格式**。须有测试断言导出文本中不含任何测试写入的真实密钥值片段。
- **AC-3 未做导入**：`grep -rn "import.*env\|parse.*env" backend/app/routers/settings.py`（限定本票改动范围）应无命中"解析上传的 .env 文件并写回配置"这类逻辑。
- **AC-4 不落盘**：响应直接返回文本，`grep -n "open(.*'w'\|write_text" backend/app/routers/settings.py`（本票改动范围内）应无命中——不写服务器本地文件。
- **AC-5 契约同步 + 回归**：`test_http_contract.py`（route_count 当前基线 +1）已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.config.schema import CONFIG_REGISTRY; from app.routers.settings import settings_router; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd256_settings_export.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T5（<issue>RND-249</issue>）**必须先落地**。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-5 全满足
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：密钥占位符实现不当导致明文泄露到导出文本——由 AC-2 防守。
- 回滚：纯追加，`git checkout -- <files>` 即可。

## 人工点位
- **Trigger**：Haisu 置 In Progress（建议 T5 落地后派发）。
- **Gate**：测试绿即可（R1，只读导出）。
- **Escalation**：T5 未就绪 → `BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引
1. 先跑前置核实命令。
2. 读 `app/config/schema.py`（`CONFIG_REGISTRY`）、`app/config/resolver.py`。
3. 追加导出端点，snake_case → SCREAMING_SNAKE_CASE 转换，密钥 `***` 占位。
4. 前端加"复制为 .env"按钮。
5. 写测试覆盖 AC-1~AC-4。
6. 同步 `test_http_contract.py`（route_count 读当前实际值 +1）。
7. 跑验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push；不做导入；不落盘；复用优先；证据优先。
