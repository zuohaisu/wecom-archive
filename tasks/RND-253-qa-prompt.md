[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-253 的 10 条 AC（重点验证「留空不覆盖」不会误提交掩码值损坏已保存密钥）并产出带证据的 PASS/FAIL 判定。

# RND-253 验收提示词（Acceptance / QA Prompt）— 配置中心 T9

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## ⚠️ 2026-08-01：本版取代旧稿（DOM 结构核实命令修正，见 dev prompt 开头说明）

第一版验收依据的分组容器类名（`settings-groups`/`settings-group`）是猜测值，与 T8 实际交付的真实 DOM（`settings-nav`/`settings-section`/`data-settings-section`，6 个分组：`general`/`account`/`third-party`/`storage`/`wecom`/`advanced`）不符。验收时按本版描述的真实结构核对，`account` 分组是既有密码卡片专属，本票不应往里塞配置项渲染逻辑。

## 任务身份
- 工单：RND-253「配置中心 T9：敏感字段交互」｜风险等级 R1
- **AC-3（留空不覆盖）是唯一有真实数据破坏风险的一条，从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。

## 验收方法（证据优先）

### AC-1 — 数据拉取渲染
- 证据：页面加载后 fetch 数据，`general`/`third-party`/`storage`/`wecom`/`advanced` 五个 `#settings-section-<group> .card-bd` 的"配置项加载中…"占位被替换成真实字段；`account` 分组**未被本票触碰**（内容仍是 T8/RND-302 的密码卡片原样）。
- 判定：符合 = PASS。**若发现本票往 `account` 分组塞了配置项渲染逻辑 → 记 finding（`SCOPE_VIOLATION`, minor）**，那个分组不对应任何 `CONFIG_REGISTRY` 字段。

### AC-2 — 密钥掩码 + 显隐
- 判定：默认掩码展示，显隐按钮切换展示态 = PASS；QA Summary 是否说明"显隐不等于看到真实明文"——若 UI 文案暗示能看到真密钥，记 finding（`IMPLEMENTATION_DEFECT`, minor，误导性文案）。

### AC-3 — 留空不覆盖（关键）
- 证据：审阅 `settings.js` 保存逻辑，确认对**未被用户修改**的密钥字段，提交给 `PUT` 的值**不是**展示用的掩码字符串（要么不提交该 key，要么提交空串走 T5 的保留分支）。
- 判定：符合 = PASS。**若发现掩码字符串（如 `"****ab12"`）被当作新值提交给后端 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——这会在用户毫无察觉的情况下把已保存的真实密钥永久覆盖成一串星号，是本票唯一能造成不可逆数据损坏的缺陷。

### AC-4 — 来源标签展示
- 判定：三种来源有可区分标签 = PASS。

### AC-5 — 重启提示
- 判定：`restart_required_keys` 非空时显示提示条 = PASS。

### AC-6 — 连通性测试按钮
- 判定：点击调用 T7 端点，展示结果 = PASS。

### AC-7 — 字段级错误展示
- 判定：`errors` 数组逐项展示到对应字段 = PASS。

### AC-8 — i18n 三 locale 齐全
- 判定：新增 key 三 locale 均存在 = PASS；任一缺失 → FAIL。

### AC-9 — 无 Jinja + D1 冻结
- 判定：同 T8 检查方式 = PASS。

### AC-10 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；T8 的分组导航切换仍正常。
- 判定：符合 = PASS。

## 本项目专属检查（必查）
1. **未改 T8 的 DOM 结构**：`git diff --stat -- backend/app/web/templates/settings.html` 应无输出。
2. **未改后端**：`git diff --stat -- backend/app/routers/settings.py backend/app/config/` 应无输出。
3. **文件所有权**：`git status --porcelain` 改动应限于 `web/static/settings.js`（扩展）、`assets/i18n.js`（仅新增）、`tests/test_rnd253_settings_interactions.py`（新）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd253_settings_interactions.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/templates/settings.html backend/app/routers/settings.py backend/app/config/
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-253-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不放松 AC。
- **AC-3 若掩码值被当新值提交 → 直接 FAIL（blocker）**，这是本票唯一能造成真实数据损坏的缺陷，不接受"概率很低"这类论证。
