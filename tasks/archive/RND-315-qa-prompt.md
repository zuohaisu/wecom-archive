[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-315 的 7 条 AC（重点验证 SF-1 数据最小化合规与未擅自新增依赖）并产出带证据的 PASS/FAIL 判定。

# RND-315 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-315「C2-1 导出服务（PDF/Excel）」｜风险等级 R1
- **导出物是高敏感 PII 载体（真实聊天内容）。AC-4（SF-1 合规）与依赖检查从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 两种格式均可生成且可解析
- 证据：测试**真正解析回读**产出的 Excel / PDF（用对应库打开并断言内容），**不是**只断言"字节数 > 0"或"文件存在"。
- 判定：两种格式都有解析回读断言 = PASS。仅断言非空 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）——那证明不了文件是有效的。
- **允许的降级**：若因依赖不可用只交付了 Excel、PDF 走了 Escalation 上报（见下方依赖检查），**不判 FAIL**，在 `notes` 中记录并标注 PDF 待补。

### AC-2 — 内容正确、展示名经既有函数
- 证据：导出内容含时间 / 发送方展示名 / 类型 / 文本；`grep -n "resolve_person_display_name\|resolve_room_display_name" backend/app/services/export_service.py` 应有命中。
- **参数核对**：`resolve_person_display_name(raw_id, name)` 第二参数是**展示名**，不是 `tenant_id`（RND-288 首轮曾在此出错）。逐字核对实参。
- 判定：内容齐全 + 复用既有函数 + 实参正确 = PASS。

### AC-3 — 不支持类型有规范占位
- 证据：构造一条不支持类型的消息，断言导出结果里是 `message_type_registry` 的规范名称，不是空白 / 裸类型码。
- 判定：有该用例且通过 = PASS。

### AC-4 — SF-1 数据最小化合规（**最高风险项，重点查**）
- 背景：本项目铁律——`decrypted_payload` 恒为 NULL，**从不持久化全量解密包**（`decrypt_worker.py:531` + migration 0008 + 回归测试锁定）。导出功能最容易破这条：为了"导得更全"而回读原始加密信封或重建明文。
- 证据：
  - 代码审阅：`grep -nE "decrypted_payload|raw_encrypted_payload|encrypt_key|encrypt_msg" backend/app/services/export_service.py` —— **应无命中**。
  - 测试断言导出内容不含这些字段。
- 判定：无任何读取/输出加密信封或 `decrypted_payload` = PASS。**发现读取 → FAIL（`SECURITY_VIOLATION`, severity: blocker）。**

### AC-5 — 媒体只导元数据
- 证据：媒体消息导出为类型 + 文件名/大小引用；测试断言产出文件体积**不随媒体二进制大小膨胀**。
- 判定：仅元数据 = PASS。嵌入了媒体二进制 → FAIL（`IMPLEMENTATION_DEFECT`）——体积与权限双重问题。

### AC-6 — 租户隔离
- 证据：构造两租户数据，以租户 A 导出，断言产出**不含**租户 B 的任何消息（须有反例测试）。
- 判定：有隔离反例测试且通过 = PASS。缺失 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 若本票新增了 HTTP 路由（**预期不应有**）：须同步 `test_http_contract.py`（见 `docs/ticket-autopilot-workflow.md` §3.3），否则 FAIL（`REGRESSION`）。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）

1. **依赖检查（重点）**：`git diff -- backend/requirements.txt`
   - **预期无输出**。若有新增依赖 → QA Summary **必须**说明获 Haisu 授权的经过（本票 dev prompt 要求先 Escalation 再加）。
   - 未经说明就新增依赖 → FAIL（`SCOPE_VIOLATION`）。本项目要开源发 AGPL，依赖许可证兼容性是产品决策，不是 agent 能自行决定的。
2. **未越界做 RND-316 / RND-317**：diff 中不得出现审批 gate（`export_approval`）或审计写入（`write_audit`）。本票只生成文件。出现 → FAIL（`SCOPE_VIOLATION`）——那会与并行的两张票打架。
3. **未改既有 service**：`git diff --stat -- backend/app/services/listing_service.py backend/app/services/timeline_service.py backend/app/display_names.py backend/app/message_type_registry.py` **必须全无输出**（只调用不改）。
4. **架构边界**：service 层未 import `app.routers.*`；若新建了扁平域模块须已登记 `_FLAT_SERVICE_MODULES`（放 `app/services/` 下则无需登记）。
5. **无导出产物落库/落仓**：确认导出文件不写入仓库内固定路径、不提交进 git（`git status` 无产出的 .xlsx/.pdf）。
6. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `services/export_service.py`、可选 `schemas/export.py`、`tests/test_export_service.py`、（获授权时）`requirements.txt`。
   > 共享工作树可能含他票在途改动（见 §3.4）——先分离归因再判。

## 附加检查（Security）
- 测试中**无**真实聊天内容 / 真实客户姓名 / 真实归档消息 → 否则 FAIL（`SECURITY_VIOLATION`, blocker）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_export_service.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -nE "decrypted_payload|raw_encrypted_payload|encrypt_key|encrypt_msg" backend/app/services/export_service.py   # AC-4：必须无命中
grep -n "resolve_person_display_name\|resolve_room_display_name" backend/app/services/export_service.py             # AC-2
git diff -- backend/requirements.txt                                # 预期无输出
git diff --stat -- backend/app/services/listing_service.py backend/app/services/timeline_service.py backend/app/display_names.py backend/app/message_type_registry.py backend/app/audit.py   # 必须全无输出
git status --porcelain
git log origin/main..HEAD                                           # 必须无输出
```

## 产出
写入 `tasks/RND-315-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：**用了哪个库生成 PDF/Excel（是否既有依赖）**、导出服务的调用签名（供 RND-316/317 对接）、是否走了 PDF 降级。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-4 若发现读取 `decrypted_payload` 或原始加密信封 → 直接 FAIL（blocker）。** 这是本项目最硬的数据最小化铁律。
- **AC-1 若测试只断言"文件非空"而未解析回读 → 直接 FAIL。**
- 未经授权说明的新增依赖 → 直接 FAIL。
