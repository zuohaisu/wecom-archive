[Goal check] This work advances 开发（Development） by 把 T8 的分组导航骨架接上真实数据与交互（掩码/显隐/来源标签/重启提示/连通性测试），让 Settings 页面从骨架变成可用功能。

# RND-253 开发提示词（Developer Prompt）— 配置中心 T9

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-253 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 开工前必须先核实
```bash
grep -n "settings-groups\|settings-group" backend/app/web/templates/settings.html   # T8 的分组骨架是否存在
```
T8（<issue>RND-251</issue>）未落地 → **停止**，`BLOCKED_NEEDS_HUMAN`，不要自己现造一份骨架。

## 任务身份
- 工单：RND-253「配置中心 T9：敏感字段掩码/显隐/留空不覆盖 + 校验反馈 + 来源标签 + 重启 banner」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R1**｜milestone：R2 · 开源发布闭环

## 背景与项目现状

**技术栈：SSR + vanilla JS（D1，同 T8）**——本票把 T3 的 `CONFIG_REGISTRY` 描述的字段，用 `fetch` 从 T5 的 `GET /api/admin/settings` 拉取，动态渲染进 T8 已搭好的分组容器里；`PUT` 保存走 T5 的 `PUT /api/admin/settings`；连通性测试按钮调 T7 的 `POST /settings/test-connection`。

**掩码格式对齐 T2**：`app/config/crypto.py` 的 `mask()` 输出格式是本票 `SecretField` 组件展示的直接依据——**去读 T2 交付的 QA verdict 或代码里 `mask()` 的注释确认确切格式**，不要自己猜一个格式。

**❗ 本项目高频踩坑：**
- **留空不覆盖语义（对齐 T5 的 PUT 空串约定）**：`SecretField` 组件对于**未被用户修改过**的密钥字段，保存时**不要把展示用的掩码值当作新值提交**（掩码值本身不是真实密钥，提交上去会把真实密钥覆盖成一串星号！）——前端逻辑必须区分"用户没碰这个字段"（不提交，或提交空串触发 T5 的"留空不覆盖"分支）和"用户输入了新密钥"（提交新值）。**这是本票最容易踩的坑，且后果是永久损坏已保存的密钥。**
- 架构冻结 D1；模板引擎铁律（无 Jinja）同 T8。
- i18n 三 locale 齐全同 T8 的要求。

## 目标（Goal）
让 T8 的分组骨架变成真实可用的配置表单：拉取当前值、密钥掩码显示+可切换显隐+可复制、非密钥字段直接可编辑、保存时不误覆盖未修改的密钥、显示配置来源标签和重启提示、提供连通性测试按钮。

## 范围边界

**In scope：**
1. `backend/app/web/static/settings.js`（**扩展 T8 已创建的文件**）：
   - `fetch('/api/admin/settings')` 拉取数据，动态渲染每个分组的字段（复用 T8 已搭的容器）。
   - `SecretField`（vanilla 函数/模块，非 React 组件）：掩码展示 + 显隐切换按钮 + 复制按钮 + **"未修改则不提交/提交空串"逻辑**（见上方踩坑说明，这是本票核心正确性要求）。
   - `SourceBadge`：根据字段的 `source`（`default`/`env`/`db`）展示对应标签。
   - `RestartBanner`：`PUT` 响应含 `restart_required_keys` 非空时展示提示条。
   - `TestConnectionButton`：调用 T7 的自检端点，展示逐项结果。
   - 保存前前端校验（必填/类型），配合 T5 返回的字段级 `errors` 展示到对应字段旁。
2. `backend/app/assets/i18n.js`：本票新增的交互文案（显隐/复制/重启提示/测试连接等按钮文案），**三 locale 齐全**。
3. 测试：`backend/tests/test_rnd253_settings_interactions.py`（HTTP 行为特征测试，验证页面加载后关键 DOM 存在，不要求完整 headless 浏览器交互测试——这个项目目前的测试基线是这个粒度，照抄现有模式）。

**Out of scope（显式非目标）：**
- 不改 T8 已搭的分组导航结构本身（只是往容器里填内容）。
- 不改后端任何端点（T5/T7 已交付，本票纯前端消费）。
- 不做完整 e2e headless 浏览器测试（超出本项目现有测试基线粒度）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/static/settings.js` —— **扩展**（T8 拥有的初始版本，本票在其基础上添加交互，不要整体重写覆盖 T8 已有的分组切换逻辑）
- `backend/app/assets/i18n.js` —— 仅新增本票需要的 key
- `backend/tests/test_rnd253_settings_interactions.py`（新）

**只读、绝不可写：** `backend/app/web/templates/settings.html`（T8 拥有的 DOM 结构，本票不改结构只通过 JS 填充内容——若发现骨架结构不够用，见下方 Escalation）、`app/routers/settings.py`（T5/T7 拥有）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 数据拉取渲染**：页面加载后 `fetch` 拉取配置数据并渲染进对应分组容器。
- **AC-2 密钥字段掩码 + 显隐**：密钥字段默认掩码展示；点击显隐按钮切换（前端切换展示态，**不重新请求明文**——GET 本来就没返回明文，前端没有明文可显示，"显隐"切换的实际含义是切换掩码格式的展示细节，不是真的显示原始密钥，这一点须在 QA Summary 里说明清楚，避免产生"点了显隐就能看到真密钥"的误解性 UI 文案）。
- **AC-3 留空不覆盖（关键）**：用户未触碰某个密钥字段时，保存请求里该字段**不提交新值**（或提交能让 T5 走"保留原值"分支的空串——与 T5 的约定一致）。须有测试/审阅确认 JS 逻辑不会把展示用的掩码字符串当作待保存的新值发出去。
- **AC-4 来源标签展示**：`default`/`env`/`db` 三种来源分别有可区分的视觉标签。
- **AC-5 重启提示**：`PUT` 响应含 `restart_required_keys` 时显示提示条，列出具体是哪些字段。
- **AC-6 连通性测试按钮**：点击后调用 T7 端点，展示逐项结果（成功/失败 + 原因）。
- **AC-7 字段级错误展示**：`PUT` 400 响应的 `errors` 数组，逐项展示到对应字段旁，不是笼统一条错误提示。
- **AC-8 i18n 三 locale 齐全**。
- **AC-9 无 Jinja + D1 冻结**：同 T8 的检查方式。
- **AC-10 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；T8 已交付的分组导航切换功能不受影响。

## 验证方式（Verification — 确定性闸）
```bash
grep -n "settings-groups\|settings-group" backend/app/web/templates/settings.html
make verify
.venv/bin/python -m pytest backend/tests/test_rnd253_settings_interactions.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn '{%\|{{' backend/app/web/templates/settings.html
git diff --stat -- backend/app/web/templates/settings.html    # 应无输出（本票不改 T8 的 DOM 结构）
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T8（<issue>RND-251</issue>）**必须先落地**（消费其分组导航骨架）。理想情况下 T5/T7 也已就绪（否则前端只能先写好 fetch 逻辑，实际联调等后端就绪）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-10 全满足
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**明确说明"显隐切换"的真实含义（不是显示明文）**
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：未修改的密钥字段保存时被掩码值覆盖，永久损坏已保存密钥——由 AC-3 防守，这是本票唯一有真实数据破坏风险的地方。
- 风险 2：DOM 结构假设与 T8 实际交付不符——开工前先跑核实命令确认容器存在。
- 回滚：`git checkout -- backend/app/web/static/settings.js backend/app/assets/i18n.js` 即可，纯前端改动无数据影响（但 AC-3 若在开发阶段有 bug 并被误用来保存过真实配置，风险发生在使用侧而非代码本身）。

## 人工点位
- **Trigger**：Haisu 置 In Progress（建议 T8 落地后派发，T5/T7 最好也已就绪）。
- **Gate**：建议 Haisu 手动跑一遍"保存一个未修改的密钥字段"场景，确认原值真的没被覆盖，再 approve commit。
- **Escalation**：T8 未就绪、或 T8 的骨架结构无法支撑本票所需交互 → `BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引
1. 先跑「开工前核实」命令。
2. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、T8 交付的 `settings.js`/`settings.html`、T2 的 `mask()` 格式、T5 的 GET/PUT 响应结构。
3. 扩展 `settings.js`：拉取渲染 + `SecretField`（**留空不覆盖是重点**）+ `SourceBadge` + `RestartBanner` + `TestConnectionButton`。
4. `i18n.js` 加交互文案（三 locale）。
5. 写测试覆盖 AC-1~AC-8。
6. 跑验证命令，输出 QA Summary（**说明显隐切换真实含义**）+ `git status`，**不 commit**。

## 硬性约束
- 不 commit / push；不碰生产数据密钥；不改后端；不改 T8 的 DOM 结构；复用优先；证据优先。
