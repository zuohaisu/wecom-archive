[Goal check] This work advances 开发（Development） by 交付配置中心的端到端集成测试 + 全量回归确认，把 T1-T7 各自的单元测试串成一条真实链路的证据。

# RND-254 开发提示词（Developer Prompt）— 配置中心 T11

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-254 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 开工前必须先核实
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.config.guard import is_initialized; from app.routers.settings import settings_router; print('OK')"
```
T4/T5/T6 未就绪 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 任务身份
- 工单：RND-254「配置中心 T11：测试（单元 + 既有 monkeypatch 回归 + 集成）」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R1**（纯测试，无生产代码改动）｜milestone：R2 · 开源发布闭环

## 背景与项目现状

T1-T7（<issue>RND-246</issue>/<issue>RND-245</issue>/<issue>RND-247</issue>/<issue>RND-248</issue>/<issue>RND-249</issue>/<issue>RND-250</issue>/<issue>RND-252</issue>）各自都已交付了针对自己模块的单元测试。**本票不是重新测一遍这些模块**，而是：① 补一条把这些模块串起来的**端到端集成测试**（模拟真实用户旅程：全新部署 → bootstrap 设置账号+企微三件套 → 登录成功 → 打开 Settings 页面看到刚设置的值（企微字段来源标为 `db`）→ 改一个非密钥字段 → 连通性自检通过）；② 对**全项目**（不只是配置中心相关文件）跑一次完整回归，确认新增的 `app/config/` 包和对 `app/routers/auth.py`/`app/routers/settings.py` 的改动没有连累任何既有测试，**尤其是全项目范围内依赖 `monkeypatch.setenv` 的测试**（这个项目的测试基线大量依赖这个模式，`app/settings.py` 的模块 docstring 专门强调了这一点）。

**❗ 本项目高频踩坑：**
- 本票**不写任何生产代码**，只写测试。若发现测试暴露了生产代码的 bug，按下方 Escalation 处理，不要在本票里顺手改生产代码（那是各自源票的范围）。

## 目标（Goal）
交付一条端到端集成测试，证明配置中心从"全新部署"到"日常使用"的完整链路真实可用；并确认全项目回归零红。

## 范围边界

**In scope：**
1. `backend/tests/test_rnd254_config_center_e2e.py`（**新建**）：端到端集成测试（见上方"背景"的用户旅程描述，完整走一遍）。
2. 跑一次全项目 `make verify`，若发现任何既有测试因本 epic 的改动而变红，**记录到 QA Summary**（不是在本票里去改那些测试或生产代码——除非失败原因明确是"测试代码本身断言了已废弃的旧行为"，那种情况可以照 `docs/ticket-autopilot-workflow.md` 的既定原则小范围修正测试断言）。

**Out of scope（显式非目标）：**
- 不改任何生产代码（`app/` 下除 `tests/` 之外的文件本票一律不碰）。
- 不重复 T1-T7 各自已有的单元测试覆盖。
- e2e 浏览器级测试（RND-244 原文说"可选"，本项目当前测试基线不含 headless 浏览器测试，本票不引入这类新的测试基础设施）。

**本工单拥有的文件（只许写这些）：**
- `backend/tests/test_rnd254_config_center_e2e.py`（新）

**只读、绝不可写：** `app/` 下所有生产代码文件、其他票拥有的一切文件。

> 若发现必须改生产代码 → **停止**，`BLOCKED_NEEDS_HUMAN`，说明具体是哪个源票的问题，不要代为修复。

## 验收标准（Acceptance Criteria）

- **AC-1 端到端集成测试真实覆盖完整旅程**：单条测试（或紧密相关的一组测试）覆盖"未初始化 → bootstrap → 登录成功 → 读取刚设置的配置（来源=db）→ 改配置 → 自检"，不是把各步骤拆成互不relate的独立测试。
- **AC-2 全项目回归零红**：`make verify` 全绿，覆盖全部既有测试套件，不只是配置中心相关的新文件。
- **AC-3 `monkeypatch.setenv` 场景全项目扫描**：至少运行一次全项目测试套件（非仅配置中心测试文件），确认没有既有的 `monkeypatch.setenv` 用例因本 epic 引入的缓存机制而失败。
- **AC-4 未改生产代码**：`git diff --stat -- backend/app/` 中不含 `tests/` 之外的任何路径。
- **AC-5 回归**：`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.config.guard import is_initialized; from app.routers.settings import settings_router; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd254_config_center_e2e.py -q
.venv/bin/python -m pytest backend/tests/ -q   # 全量回归，非仅本 epic 相关文件
git diff --stat -- backend/app/    # AC-4：应无 tests/ 之外的路径
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T4（<issue>RND-248</issue>）+ T5（<issue>RND-249</issue>）+ T6（<issue>RND-250</issue>）**必须先落地**。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-5 全满足
- [ ] `make verify` 全绿（全量，非仅新增文件）
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，附完整用户旅程的测试执行记录
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：集成测试测试环境搭建（模拟"全新部署"状态）与既有测试 fixture 冲突——若发生，按 Escalation 上报，不要为了绕开冲突而改动共享的 conftest/fixture（那可能是他票拥有的文件）。
- 回滚：纯新增测试文件，`rm backend/tests/test_rnd254_config_center_e2e.py` 即可。

## 人工点位
- **Trigger**：Haisu 置 In Progress（建议 T4/T5/T6 落地后派发，这是本 epic 的收尾验证票之一）。
- **Gate**：测试绿即可，无需额外人工审阅（R1，纯测试）。
- **Escalation**：T4/T5/T6 未就绪 → `BLOCKED_NEEDS_HUMAN`。若端到端测试暴露了某个源票的生产代码 bug → `BLOCKED_NEEDS_HUMAN`，说明具体现象和疑似源票，不要代为修复。

## 开发 agent 执行指引
1. 先跑前置核实命令。
2. 读 T1-T7 各自的测试文件，理解已覆盖的单元级场景，避免重复。
3. 写端到端集成测试，覆盖完整用户旅程。
4. 跑全项目 `make verify`，记录任何既有失败。
5. 输出 QA Summary（含完整旅程记录）+ `git status`，**不 commit**。

## 硬性约束
- 不 commit / push；不改生产代码；不碰生产数据密钥；证据优先。
