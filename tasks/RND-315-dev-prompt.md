[Goal check] This work advances 开发（Development） by 交付消息导出为 PDF/Excel 的服务模块，为 C2 证据导出链提供生成能力。

# RND-315 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-315 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-315「C2-1 导出服务（PDF/Excel 生成）」｜父 Epic RND-271（C2 证据导出）
- 优先级：Medium｜风险等级：**R1**｜milestone：R3
- **本票 blocks RND-316（审批 gate）与 RND-317（导出审计钩子）** —— 它俩守卫的就是本票产出的能力

## 背景与现状（已实地核实）

C2 证据导出是本产品的合规价值落点（客户要能把某段会话作为证据导出）。三张子票分工：

| 票 | 职责 | 状态 |
|---|---|---|
| **RND-315（本票）** | **生成文件**（输入消息查询 → 输出 PDF/Excel） | 本票 |
| RND-316 | 导出前的安全审批 gate | 并行，独立文件 |
| RND-317 | 导出后写审计 | 并行，独立文件 |

**本票只做"生成"这一件事，不做 gate、不做审计。** 三张票文件不重叠，可并行。

**可复用的既有资产（先读，不要重造）：**
- 消息查询：`backend/app/services/listing_service.py` 与 `backend/app/services/timeline_service.py` 已有成熟的消息投影/组装逻辑。**优先复用查询与投影，本票只负责把结果渲染成文件。**
- 展示名：`backend/app/display_names.py` 的 `resolve_person_display_name(raw_id, name)` / `resolve_room_display_name(roomid, room_name)`。**第二参数是展示名，不是 tenant_id**（RND-288 首轮曾在此出错）。
- 消息类型：`backend/app/message_type_registry.py` 有官方类型注册表——渲染不支持类型时用它给出规范名称，不要自己硬编码类型名。

**❗ 本项目高频踩坑与本票特有风险：**
- **SF-1 数据最小化铁律**：`decrypted_payload` 恒为 NULL，系统**从不持久化全量解密包**。导出时只能用已存的 `content_text` / `structured_content` 等规范化字段，**绝不能**为了导出去回读或重建原始加密信封。
- **不得新增大依赖**（`DEV_AGENT_RULES.md` 明令）。先 `grep -i "reportlab\|openpyxl\|xlsxwriter\|weasyprint\|fpdf" backend/requirements.txt` 看有没有现成的；**若没有，不要擅自 `pip install`** —— 见下方「人工点位」的 Escalation。
- **架构边界硬闸**：新建扁平域模块须登记 `backend/tests/test_architecture_boundary.py:70` 的 `_FLAT_SERVICE_MODULES`；放 `app/services/` 下则自动属 service 层，无需登记。
- 架构冻结 D1：纯后端，无前端。

## 目标（Goal）
提供一个可被 C2 链路调用的导出服务：给定租户 + 消息筛选条件，产出结构正确、内容可读、不含敏感冗余的 PDF 或 Excel 文件。

## 范围边界

**In scope：**
1. 导出服务模块（建议 `backend/app/services/export_service.py`）：
   - 输入：`tenant_id` + 消息筛选条件（会话 / 时间范围 / 消息 ID 集合，具体参数自行设计但须租户隔离）。
   - 输出：文件字节流 + 建议文件名（**不落盘到固定路径**，由调用方决定怎么处置）。
   - 至少支持 **Excel**（结构化表格，最容易正确）与 **PDF**（可读排版）两种格式。
2. 渲染规则：
   - 每条消息含：时间（北京时间，与控制台一致）、发送方展示名、接收方/会话、消息类型、内容文本。
   - 不支持的消息类型：用 `message_type_registry` 的规范名称占位，**不要**输出空白或原始类型码。
   - 媒体消息：只导出**元数据引用**（类型 + 文件名/大小），**不嵌入媒体二进制**（体积与权限双重问题）。
3. 测试：`backend/tests/test_export_service.py`。

**Out of scope（显式非目标）：**
- **不做审批 gate**（RND-316）、**不做审计写入**（RND-317）。本票只生成文件。
- **不新增 HTTP 路由** —— 本票是服务模块；对外端点由 RND-316/317 或后续票提供。（因此本票**预期不触发契约测试同步**；若你确实新增了路由，必须按 `docs/ticket-autopilot-workflow.md` §3.3 同步 `test_http_contract.py`。）
- 不导出原始加密信封 / `decrypted_payload`（SF-1 铁律）。
- 不嵌入媒体二进制。
- 不做前端下载入口。

**本工单拥有的文件（只许写这些）：**
- `backend/app/services/export_service.py`（新）
- `backend/app/schemas/export.py`（新，若需要）
- `backend/tests/test_export_service.py`（新）
- `backend/requirements.txt` —— **仅当** Escalation 后获授权新增依赖时

**只读、绝不可写：** `listing_service.py`、`timeline_service.py`、`display_names.py`、`message_type_registry.py`（只调用）、`app/audit.py`（RND-317 的范围）、`app/export_approval.py`（RND-316 的范围）、其他票拥有的一切文件。

## 验收标准（Acceptance Criteria）

- **AC-1 两种格式均可生成**：给定一组消息，Excel 与 PDF 均能产出非空、可被对应库正确解析的文件（测试须真正解析回读，不能只断言"字节数 > 0"）。
- **AC-2 内容正确**：导出内容含时间 / 发送方展示名 / 类型 / 文本；展示名经 `resolve_*_display_name` 产出（代码可见 import + 调用）。
- **AC-3 不支持类型有规范占位**：构造一条不支持类型的消息，断言导出结果里是 `message_type_registry` 给出的规范名称，不是空白或裸类型码。
- **AC-4 SF-1 合规（关键）**：导出逻辑**不读取也不输出** `decrypted_payload` 或原始加密信封字段。代码审阅 + 测试断言导出内容不含这些。
- **AC-5 媒体只导元数据**：媒体消息导出为类型 + 文件名/大小的引用，**不含**媒体二进制（断言文件体积不随媒体大小膨胀）。
- **AC-6 租户隔离**：导出只包含指定 `tenant_id` 的消息；构造两租户数据，断言不串（须有反例测试）。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_export_service.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/audit.py backend/app/services/listing_service.py backend/app/services/timeline_service.py backend/app/display_names.py   # 必须全无输出（只调用不改）
git diff -- backend/requirements.txt   # 预期无输出；若有，QA Summary 必须说明获授权经过
```

## 依赖（Dependencies）
无前置阻塞（Linear 原文即标 `[BLOCKED: none]`），可立即开始。**本票 blocks RND-316 / RND-317。**

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**说明：用了哪个库生成 PDF/Excel（是否为既有依赖）、服务的调用签名（供 RND-316/317 对接）**
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：为了"导出得更全"而回读原始加密信封或重建 `decrypted_payload` → 违反 SF-1 数据最小化铁律。**由 AC-4 显式防守。**
- **风险 2**：擅自新增重量级 PDF 依赖（如 weasyprint 拉入整套 GTK）→ 违反"不加大依赖"规则且拖慢 CI。**先查既有依赖，没有就上报。**
- **风险 3**：导出大批量消息时内存爆掉 —— 应流式或分批生成，避免一次性把全部消息载入内存后再渲染。
- **风险 4**：导出物本身是**高敏感 PII 载体**（真实聊天内容）。测试必须用固定假数据，**绝不**引入真实归档消息；产物不得写入仓库内固定路径。
- 回滚：纯新增文件，`git checkout -- <files>` 即可。

## 人工点位
- **Trigger**：Haisu 置 In Progress（已置）。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation（重要）**：若 `requirements.txt` 里**没有**可用的 PDF/Excel 生成库 → **停止，`BLOCKED_NEEDS_HUMAN`**，列出候选库与各自体积/许可证/维护状态，让 Haisu 选。**不要擅自 `pip install` 或直接往 `requirements.txt` 加东西** —— 依赖选择是产品决策（尤其本项目要开源，许可证兼容性影响 AGPL 发布）。
  > 折中允许：若已有 `openpyxl` 之类可做 Excel 但无 PDF 库，可**先只交付 Excel**、把 PDF 标记为待授权后补，并在 QA Summary 说明——这好过为了凑齐两种格式而擅自引依赖。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`listing_service.py` 与 `timeline_service.py`（消息查询/投影现状）、`display_names.py`、`message_type_registry.py`。
2. **先查依赖**：`grep -iE "reportlab|openpyxl|xlsxwriter|weasyprint|fpdf|pandas" backend/requirements.txt`。无可用库 → 按 Escalation 上报。
3. 写 `export_service.py`：查询复用既有 service，渲染逻辑本票新写。
4. 写测试覆盖 AC-1~AC-6（**AC-1 要真正解析回读产出文件；AC-4/AC-6 是反例测试**）。
5. 跑 `make verify`，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- **不擅自新增依赖**（见 Escalation）。
- 不碰生产数据；测试用固定假数据，**绝不引入真实聊天内容**。
- 不扩大 Scope：gate、审计、路由、前端一律 Out。
- 复用优先：查询/投影/展示名/类型注册表只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
