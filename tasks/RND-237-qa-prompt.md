[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-237 的开源发布包装交付物（AGPL-3.0 全文 / CLA / README SDK 说明 / .gitignore 敏感项）并产出带证据的 PASS/FAIL 判定。

# RND-237 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-237「开源发布包装：LICENSE / 公开 README / .gitignore 复核」｜父 Epic RND-232
- 风险等级：**R1**（无代码改动，但**决定仓库能否合法开源**）｜milestone：R2 · 开源发布闭环
- 本票是纯文档 / 许可证工单，**不改任何业务代码**。

## 你的角色与权限
- 可以：读所有文件、跑只读命令（`grep`、`git diff`、`git status`、`test -f`）。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — LICENSE 是 AGPL-3.0 全文（**决策已拍板，不得偏离**）
- 背景：2026-07-29 Haisu **将许可证从 MIT 反转为 AGPL-3.0**（理由：项目并行运营云托管商业版，AGPL 的网络服务条款防止第三方直接拿代码另起云生意）。此前所有提示词/文档里的 MIT 表述均已作废。
- 证据：
  ```bash
  test -f LICENSE && echo ok
  grep -c "GNU AFFERO GENERAL PUBLIC LICENSE" LICENSE     # 应 ≥ 1
  grep -c "Version 3, 19 November 2007" LICENSE            # AGPL-3.0 版本行
  grep -ci "MIT License" LICENSE                           # 应为 0
  wc -l LICENSE                                            # AGPL-3.0 全文约 660 行；明显偏短说明是摘要不是全文
  ```
- 判定：AGPL-3.0 **全文**存在且无 MIT 残留 = PASS。**若是 MIT、或是 AGPL 摘要/改写版而非官方全文 → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——许可证文本必须逐字为官方版本，改写会导致法律效力不确定。

### AC-2 — CLA 文档存在且 README/CONTRIBUTING 有签署指引
- 背景：AGPL + 自营云的标准配置。**没有 CLA，外部贡献合并后项目将失去单方面调整授权（如未来再许可）的能力**——这对同时运营商业云版本是实质风险。
- 证据：
  ```bash
  ls CLA.md .github/CLA.md 2>/dev/null                      # 至少一处存在
  grep -rn "CLA" README.md CONTRIBUTING.md 2>/dev/null      # 应有签署指引
  ```
- 内容检查：CLA 至少应含 ① 贡献者授予维护方在 AGPL-3.0 之外**额外再许可**该贡献的权利 ② 贡献者保证拥有贡献版权 ③ 免责声明。
- 判定：文档存在 + 三项要素齐全 + README/CONTRIBUTING 有指引 = PASS。缺文档或缺指引 = FAIL。
- **允许的降级**：签署机制（CLA bot vs 手动）本票**不要求**接入自动化，只需文档到位 + 指引清晰。仅未接自动化不判 FAIL。

### AC-3 — README 含 WeCom SDK 获取说明（**"30 分钟上手"的隐形拦路虎**）
- 背景：`backend/vendor/wecom_sdk/` 是**腾讯专有 SDK**（两个 tgz 共约 11.8MB），已被 `.gitignore:221` 排除 —— **仓库不含该二进制，这点是干净的**。但代价是自托管用户必须自行从腾讯获取，否则会在归档解密链路卡住，且**没有任何报错提示**。
- 证据：
  ```bash
  grep -n "wecom_sdk\|WeCom SDK\|企业微信.*SDK" README.md    # 应有命中
  ```
- 内容检查：说明应出现在**显著位置**（快速开始的前置说明或常见问题区），且讲清：这是腾讯专有 SDK、仓库不含、需自行获取、缺失会导致什么现象。
- 判定：说明存在且位置显著 = PASS。完全没有 → FAIL；仅在角落一笔带过 → 记 minor finding。

### AC-4 — README 无真实身份 / 凭据泄露
- 证据：
  ```bash
  grep -rniE "crowntime\.cn|康冠时代|ICP备|hs@|[0-9]{11}" README.md
  grep -rniE "password|secret|token|api[_-]?key" README.md   # 应只在占位符/说明语境
  ```
- 注意：本票与 RND-233（域名去标识化）/ RND-242（品牌公开化）协同。品牌名「康冠时代」**按 RND-242 决策是可以公开的**（选项 A 公开实体），所以出现品牌名不一定是问题；**真正要拦的是**：真实域名、真实邮箱/电话、真实凭据。
- 判定：无真实凭据、无未经决策的身份信息 = PASS。发现真实密钥/token → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-5 — .gitignore 覆盖敏感类文件
- 证据：
  ```bash
  grep -nE "^\.env$|^\.env|\*\.pem|\*\.key|\*\.crt|secrets|\.qoder/" .gitignore
  grep -n "!.env.example\|\.env\.example" .gitignore          # .env.example 应被允许（占位符文件）
  git check-ignore -v .env 2>/dev/null                         # 应命中忽略规则
  ```
- 判定：`.env` / `*.pem` / `*.key` / `*.crt` / `secrets*` / `.qoder/` 均被忽略，且 `.env.example` **未**被误忽略 = PASS。
- **额外核查**：`git ls-files | grep -iE "\.env$|\.pem$|\.key$|\.crt$"` **应无输出**——若有敏感文件已被 tracked，这是既有问题，如实记入 findings 并标注需 Haisu 处理（不是本票新引入的则记 major 而非 blocker）。

### AC-6 — 未改业务代码
- 证据：`git diff --stat -- backend/` **必须无输出**。本票是纯文档/许可证工单。
- 判定：`backend/` 零改动 = PASS。有改动 → FAIL（`SCOPE_VIOLATION`）。

### AC-7 — 回归
- 证据：`make verify` exit 0（本票不该影响它，但确认没意外破坏）。
- 判定：exit 0 = PASS。

## 本项目专属检查（必查）
1. **许可证决策一致性**：全仓搜索是否还有描述本项目许可证为 MIT 的残留文本：
   ```bash
   grep -rniE "MIT (License|许可)" README.md docs/ tasks/ 2>/dev/null | grep -v "第三方\|dependency\|依赖"
   ```
   若在**描述本项目自身许可证**的语境下仍写 MIT → 记 finding（`IMPLEMENTATION_DEFECT`）。描述第三方依赖是 MIT 则正常，不算问题。
2. **未越界做 RND-239 / RND-261**：本票不含营销网站建设、不含域名变更。diff 出现这些 → FAIL（`SCOPE_VIOLATION`）。
3. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于：`LICENSE`（新）、`CLA.md` 或 `.github/CLA.md`（新）、`README.md`、`.gitignore`、可选 `CONTRIBUTING.md` / `SECURITY.md` / `.github/` issue 模板。
   > 共享工作树可能含他票在途改动（见 `docs/ticket-autopilot-workflow.md` §3.4）——先 `git status` 分离归因。

## 附加检查（Security）
- 无真实凭据 / 密钥 / 私钥被新增进任何文件。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL（`SECURITY_VIOLATION`）。
- 未改 CI/CD、部署配置。

## 验证命令（只读）
```bash
test -f LICENSE && echo "LICENSE exists"
grep -c "GNU AFFERO GENERAL PUBLIC LICENSE" LICENSE
grep -ci "MIT License" LICENSE                      # 应为 0
wc -l LICENSE                                        # AGPL-3.0 全文约 660 行
ls CLA.md .github/CLA.md 2>/dev/null
grep -rn "CLA" README.md CONTRIBUTING.md 2>/dev/null
grep -n "wecom_sdk\|WeCom SDK" README.md
grep -nE "^\.env|\*\.pem|\*\.key|\*\.crt|secrets|\.qoder/" .gitignore
git ls-files | grep -iE "\.env$|\.pem$|\.key$|\.crt$"    # 应无输出
git diff --stat -- backend/                              # 必须无输出
make verify
git status --porcelain
git log origin/main..HEAD                                # 必须无输出
```

## 产出
写入 `tasks/RND-237-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：CLA 放在哪个路径、签署机制是否已接自动化（未接不算失败）、是否发现已被 tracked 的敏感文件。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-1 若 LICENSE 是 MIT 或 AGPL 摘要而非官方全文 → 直接 FAIL（blocker）。** 许可证文本必须逐字官方版本。
- 不接受"CLA 之后再补"——AGPL + 自营云下，CLA 缺失是实质性法律风险，本票必须交付。
