# RND-283 QA / 验收 agent 提示词

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始下面的验收步骤，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发文档规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：确认工单号，然后直接进入验收核对步骤。

---

> 面向独立测试 / QA agent。只读言、不改实现、不 commit / push。
> 验收对象：开发 agent 按 `RND-283-dev-prompt.md` 产出的改动。
>
> 归属：本票是 Epic **RND-266（A2 用量分析）** 的唯一具体子票，是该 Epic 的可执行切片。验收结论即代表 Epic RND-266 的「图表由真实数据渲染；不暴露消息内容」AC 是否达成。
>
> **依赖链**：RND-283 blocker = A1-1 = **RND-281**（`[BLOCKED: F0]`，父 Epic RND-262），其模块 `app.services.usageservice`。RND-281 自身 blocker = F0。真实前置链 **F0 → RND-281 → RND-283**。验收前先确认 RND-281 已合并，否则整体 FAIL。

## 一、验收目标
确认 RND-283 达成：4 张图 + 4 张指标卡均由真实聚合数据渲染；**零消息内容泄露**；不做导出；租户隔离；架构边界与既有契约不变；`make verify` 全绿。

## 二、逐条验收清单（PASS / FAIL，附证据）

### 前置依赖
- [ ] P0 **A1-1（RND-281）已合并**：`python -c "import app.services.usageservice"` 成功 + `alembic check` 绿。若未合并，开发 agent 应已停下报告——本项 FAIL 且整体判 FAIL，附「依赖未就绪（RND-281 自身 blocker 为 F0）」说明。

### 后端聚合（落点 A/B）
- [ ] B1 `/api/admin/usage` 返回 概览(archived_days/total_messages/avg_daily/storage_bytes) + 4 类真实聚合（trend / type_composition / storage_composition / hourly_distribution）+ meta。证据：`curl -H "Cookie: ..." /api/admin/usage | jq`。
- [ ] B2 `trend` 按日分桶（当前+上一周期）、`type_composition` 按 msgtype 归并到 registry 大类、`storage_composition` 来自 `media_files.file_size` 求和（文本/索引为文档化估算）、`hourly_distribution` 为 24 桶；概览卡复用 `app.services.usageservice` 原语。证据：读源码 `app/services/analytics_service.py` + `app/services/usageservice.py` + 抽样数据比对。
- [ ] B3 所有查询 `where(tenant_id == 会话租户)`，**无**请求参数传入 tenant。证据：grep `get_current_user` / `auth` 取值 + 断言端点不接受 `?tenant=`。

### 零泄露（Epic 首要 AC）
- [ ] L1 响应 JSON 中**不存在任何**内容键：`msgid` / `sender` / `roomid` / `content_text` / `decrypted_payload` / `tolist` / `sdkfileid` / 任意 wecom/user 原始标识符。证据：`curl ... | jq 'paths(scalars)'` 全量扫描键路径。
- [ ] L2 页面 DOM / `analytics.js` 不发任何指向单条消息的请求，不渲染消息片段。证据：读 `analytics.html` + `analytics.js`，grep `msgid`/`content`。
- [ ] L3 错误/空数据分支只返回聚合占位（0 / 空数组），不回退暴露明细。证据：tenant 内无数据时接口返回形状正确。

### 前端（落点 D）
- [ ] F1 `/admin/analytics` 经会话可达（未登录 302 到 `/admin/login`），4 图 + 4 卡均由接口数据渲染（非硬编码）。证据：浏览器/ cur 访问 + 检查 SVG 节点被注入、指标卡数值来自接口。
- [ ] F2 **无「导出 Excel」按钮**（Non-goals 不做导出，见 C2）。证据：grep `导出` / `export` / `Excel` 在 `analytics.html` + `analytics.js` 中无命中。
- [ ] F3 侧栏「数据」分组（review_console.html:360 之后）含「用量分析」项（`nav.usageAnalytics`），`app/assets/i18n.js` 的 **zh-CN / zh-TW / en 三个 locale 块**均有该键。证据：读 `review_console.html:361` 附近 + `i18n.js` 三处。
- [ ] F4 纯原生 JS / SVG，无 React / 外部框架引用。证据：grep `react` / `import` 框架于 `analytics.js`。

### 架构边界
- [ ] C1 新聚合模块 `backend/app/services/analytics_service.py`（包下 service 层）**未** import `app.routers.*` / `app.main`；无需加入 `_FLAT_SERVICE_MODULES`（包模块自动归类）。证据：读模块 import 列表 + `test_architecture_boundary.py` 通过。
- [ ] C2 新路由 `app/routers/analytics.py` 经 `main.py` 的 `include_router(analytics_router)` 注册，`main.py` 内**无**新增 `@app.get(...)` 业务路由。证据：grep `include_router` + `_HEALTH_PROBE_PATHS`。

### 全局契约
- [ ] C3 路由数基线：新增 `/api/admin/usage` + `/admin/analytics` 共 2 条属预期；若 `test_http_contract.py` pin 了精确路由数，开发 agent 已同步更新且测试绿。证据：`make verify` 结果。
- [ ] C4 租户隔离：用租户 A 的会话访问，返回数据仅含 A 的聚合（可造两租户数据验证）。
- [ ] C5 i18n 未动既有键；仅新增 `nav.usageAnalytics`（三 locale）。证据：git diff `app/assets/i18n.js`。
- [ ] C6 `make verify` 全绿（lint-diff typecheck build test）。证据：日志。

## 三、回归套件（必须全绿）
`make verify` + 重点：`test_architecture_boundary.py`、`test_http_contract.py`、`test_password_auth.py`、`test_i18n_foundation.py`。

## 四、智能路由判定（每轮必给）
- 源码有 Bug → 反馈开发 agent 修复，附错误 + 失败测试 + 期望；不自行改实现。
- 测试代码有 Bug（如 pin 了旧路由数 / 断言旧路径）→ 可自行修正测试（仅当断言旧路径，须标注）。
- 全部通过 → 报告 SUCCESS，附 RED→GREEN 对比（B1 采样数据）。
最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-283（Epic RND-266 切片）验收结论：PASS / FAIL
A1-1(RND-281) 就绪：是 / 否（否则整体 FAIL，注：RND-281 自身 blocker=F0）
零泄露 L1-L3：通过 / 未过（证据）
真实数据渲染：是 / 否
导出按钮：已删除 / 仍存在（FAIL 项）
make verify：绿 / 红
契约：路由数 / 租户隔离 / i18n 不变
遗留：___
```
