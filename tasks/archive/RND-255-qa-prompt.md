[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-255 的 5 条 AC（重点验证文档命令/路径真实可用、未改代码、既有 README 章节零回归）并产出带证据的 PASS/FAIL 判定。

# RND-255 验收提示词（Acceptance / QA Prompt）— 配置中心 T12

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-255「配置中心 T12：文档」｜风险等级 R0

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。

## 验收方法（证据优先）

### AC-1 — 覆盖四个必答问题
- 判定：加密 key 管理/env 兼容/重启项清单/首启向导，四部分均在 README 新章节里 = PASS；缺任一 → FAIL。

### AC-2 — 命令真实可执行（关键）
- 证据：文档里的 Fernet key 生成命令实际跑一遍，确认产出合法 key；提到的路径（如 `/admin/settings/init`）在代码里 `grep` 确认真实存在。
- 判定：全部核实通过 = PASS。**任一命令/路径与实际代码不符 → FAIL（`IMPLEMENTATION_DEFECT`）**——文档描述不存在的功能会直接误导陌生用户，与 R2"30分钟看到消息"的验收目标背道而驰。

### AC-3 — 品牌占位符
- 判定：产品名处用占位符，无写死品牌名 = PASS。

### AC-4 — 未改代码
- 证据：`git diff --stat` 只有 `README.md`。
- 判定：符合 = PASS。**改动了任何代码文件 → 直接 FAIL（`SCOPE_VIOLATION`）**。

### AC-5 — 既有内容不变
- 证据：`git diff -- README.md` 中既有章节（RND-237 交付部分）逐字未变。
- 判定：符合 = PASS。

## 验证命令（只读）
```bash
grep -n "SETTINGS_ENCRYPTION_KEY\|admin/settings/init\|bootstrap" README.md
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key())"   # 对照文档给出的命令是否等价可行
grep -n "bootstrap-status\|/admin/settings/init" backend/app/routers/settings.py backend/app/routers/web.py
git diff --stat
git diff -- README.md
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/archive/RND-255-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不放松 AC。
- **AC-2 若文档描述的功能/命令与实际代码不符 → 直接 FAIL**，不接受"意思差不多"。
