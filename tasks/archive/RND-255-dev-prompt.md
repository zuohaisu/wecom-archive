[Goal check] This work advances 开发（Development） by 交付自托管配置指南文档，把配置中心（T1-T7）的能力写成用户能照做的操作手册，是 R2"30 分钟看到第一条消息"验收标准的最后一块拼图。

# RND-255 开发提示词（Developer Prompt）— 配置中心 T12

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-255 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 开工前必须先核实
```bash
grep -n "bootstrap-status\|/admin/settings/init" backend/app/routers/settings.py backend/app/routers/web.py
```
T5/T6 未就绪（命令无命中）→ **停止**，`BLOCKED_NEEDS_HUMAN`——文档必须描述真实存在的功能，不能先写文档再等功能补上。

## 任务身份
- 工单：RND-255「配置中心 T12：文档：自托管配置指南」｜父 Epic RND-244
- 优先级：Low（P1）｜风险等级：**R0**（纯文档）｜milestone：R2 · 开源发布闭环

## 背景与项目现状

README 目前应该已经有 <issue>RND-237</issue> 交付的开源发布章节（LICENSE/CLA/WeCom SDK 说明），本票在其基础上**新增**一个"Settings 配置中心"章节，而不是重写整个 README。

**品牌占位符**：UI 文案用 `{{PRODUCT_NAME}}`/`{{ product_name }}` 占位（<issue>RND-242</issue> 定名决策后统一替换），文档里提到产品名的地方**同样用占位符**，不要在这一份文档里率先写死一个具体品牌名。

**❗ 本项目高频踩坑：**
- 纯文档票，不要顺手"顺便"改动任何代码文件。

## 目标（Goal）
在 README 里新增一节，讲清楚自托管用户如何用 Settings 配置中心完成部署：加密 key 从哪来、env 兼容关系是什么、哪些改动需要重启、首启向导怎么走。

## 范围边界

**In scope：**
1. `README.md`（**追加新章节**，标题建议"配置中心 / Settings"或等价）：
   - `SETTINGS_ENCRYPTION_KEY` 是什么、如何生成（给一条生成 Fernet key 的具体命令）、缺失会怎样（fail-closed，拒绝保存密钥类配置）。
   - env 与 DB 的关系（`DB > env > 默认`，说明"改 .env 仍然有效，但优先级低于 UI 里保存的值"这一点，避免用户困惑"为什么改了 .env 没生效"）。
   - 需要重启才生效的配置项清单（引用 T3 的 `RESTART_REQUIRED_KEYS`，不要在文档里手写一份可能漂移的副本——写清楚"以 UI 上的重启提示为准"）。
   - 首启向导操作步骤（对应 T6 的 `GET /admin/settings/init`）：`git clone` 后启动服务 → 访问 `/admin/settings/init` → 设置管理员账号 → 补齐企微三件套 → 登录 → 看到第一条消息，**这一段是本文档最核心的部分，直接服务 R2 的验收标准**。
2. 测试：无需新增测试文件（纯文档），但需要验证 README 里提到的所有命令/路径**真实可用**（如生成 Fernet key 的命令、`/admin/settings/init` 路径）。

**Out of scope（显式非目标）：**
- 不改任何代码文件。
- 不写死具体品牌名（用占位符）。
- 不重写 README 其他既有章节。

**本工单拥有的文件（只许写这些）：**
- `README.md` —— **仅追加**新章节，既有内容不动

**只读、绝不可写：** 所有代码文件。

> 若发现文档描述与实际功能不符（例如某个端点路径与本票的假设不同）→ 以实际代码为准调整文档描述，不要为了文档"看起来完整"而描述不存在的功能。

## 验收标准（Acceptance Criteria）

- **AC-1 覆盖四个必答问题**：加密 key 管理、env 兼容说明、重启项清单、首启向导——四部分均在文档里，缺一不可。
- **AC-2 命令真实可执行**：文档里给的生成 Fernet key 命令、提到的 API 路径/页面路径，均与代码里实际存在的一致（须逐一核对，不是抄一份"看起来对"的示例）。
- **AC-3 品牌占位符**：涉及产品名的地方用 `{{PRODUCT_NAME}}` 或等价占位符，不写死具体品牌名。
- **AC-4 未改代码**：`git diff --stat` 中只有 `README.md`。
- **AC-5 既有内容不变**：`git diff -- README.md` 中 <issue>RND-237</issue> 交付的既有章节逐字未变，本票是纯追加。

## 验证方式（Verification — 确定性闸）
```bash
grep -n "bootstrap-status\|/admin/settings/init" backend/app/routers/settings.py backend/app/routers/web.py   # 开工前核实
grep -n "SETTINGS_ENCRYPTION_KEY\|admin/settings/init" README.md   # AC-1/AC-2：文档确实提到这些
git diff --stat    # AC-4：应只有 README.md
git diff -- README.md    # AC-5：人工核对既有章节未变
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T5（<issue>RND-249</issue>）+ T6（<issue>RND-250</issue>）**必须先落地**（文档要描述真实存在的功能）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-5 全满足
- [ ] `git status` 只显示 `README.md`
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：文档描述与实际实现细节不符——开工前核实 + 逐项核对命令/路径的真实性防守。
- 回滚：`git checkout -- README.md` 即可。

## 人工点位
- **Trigger**：Haisu 置 In Progress（建议 T5/T6 落地后派发，是本 epic 收尾票）。
- **Gate**：无需特别审阅（R0，纯文档）。
- **Escalation**：T5/T6 未就绪 → `BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引
1. 先跑前置核实命令。
2. 读现有 README.md（找到 <issue>RND-237</issue> 章节位置，确定追加位置）、T6 的引导流程实现、T3 的 `RESTART_REQUIRED_KEYS`。
3. 追加新章节，覆盖四个必答问题。
4. 逐一核对文档里的命令/路径与实际代码一致。
5. 输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push；不改代码；不写死品牌名；证据优先（文档准确性以实际代码为准）。
