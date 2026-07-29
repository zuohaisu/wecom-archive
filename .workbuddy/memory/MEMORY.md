# Project Memory — wecom-archive-365

## 项目身份
- Linear project「365企微会话存档」(ID: cfe726ec-810c-4848-8455-9b3607bf1fc1)，team Builder (RND)
- 仓库 /Users/zuohaisu/Documents/code/wecom-archive-365；规则见 DEV_AGENT_RULES.md + docs/AGENTS.md
- Linear MCP：~/.workbuddy/mcp.json（@calltelemetry/linear-mcp），已配置

## 硬规则（必须遵守）
- **Agent 绝不 git commit/push**——一律用户本人操作，即便 QA 通过/用户许可也只提示
- **QA 先行**：实现后交独立 QA agent 验收，用户决定是否提交
- 交付物=「开发提示词+验收提示词」两份文件（**2026-07-29 起统一存 `tasks/`，命名 `RND-<n>-dev-prompt.md`/`RND-<n>-qa-prompt.md`；不再写入 `.workbuddy/prompts/`——原 11 张开放工单提示词已搬迁改名进 `tasks/`，`.workbuddy/prompts/` 只保留 `archive/` 历史，勿在旧路径重新生成文件**），用户决定何时执行

## Linear API 备忘
- markdown `_` 会被解析为斜体→代码标识符一律加反引号
- 新 issue 默认 Backlog，要用需显式 status="Todo"

## 架构约束（durable, verified 2026-07-27）
- **SF-1 数据最小化**：`decrypted_payload` 恒 NULL，从不持久化全量解密包（decrypt_worker.py:531 + migration 0008 + 回归测试锁定）
- 历史重解析=重解密信封：仅存加密信封（raw_encrypted_payload 等），须走 `_decrypt_message(lib_path=...)` → decrypt_isolation 子进程 → parse_structured_content → 仅写 structured_content（RND-257）
- 隔离解密已上线（RND-231）：单条 SIGSEGV 折叠为 sentinel 跳过不中断整批

## 开源准备 epic RND-232（2026-07-27 拍板）
- **双仓库模式**：私有仓全量 tracked；发布=新开独立 public 仓（净快照无历史）→ git 历史清洗取消（RND-241 Canceled）；RND-236=写发布排除清单；导出脚本归 RND-237；公开仓 CI 去 deploy job
- **RND-260（Todo）逆向恢复**：决策前 untrack 已执行进 git（`8a1a7d9` RND-241 + `3fee9bd` remove process docs，共 278 文件：.workbuddy/.qoder/.trae/ppt-365-intro/company_homepage/TASK_TRACKING/PROJECT_DOC…），文件均在磁盘。需删 .gitignore L250~265 规则（ppt-365-intro 只留 .slidep/.cache 忽略）+ git add 加回，用户本人提交
- **命名落定**：产品名=「康冠时代」企业微信会话存档（公司 100% 持股+注册商标第17528858号）；品牌化=选项A 公开实体；RND-233 域名 env 化降级 SHOULD-FIX；「企业微信」作描述性用语+「非腾讯官方」免责声明；许可证扫描干净
- 子任务：RND-233 域名env化 / RND-236 排除清单 / RND-237 发布包装 / RND-239 项目内营销站+官网 / RND-242 品牌公开化更名（文案总开关）
- **商标要点**：注册证有效期至 2026-11-13，续展窗口已开；注册为黑白版→可用任意颜色

## 2026-07-29（另一 session）新增决策与状态，已核实（18:21 通读 final-roadmap-2026-07-29.md + ticket-autopilot-workflow.md 确认）
- **许可证反转**：MIT → **AGPL-3.0**（原因：项目将并行运营云托管商业版，AGPL 防第三方白嫖云生意）。RND-237 已同步新增 CLA 文档 + WeCom SDK 获取说明两项交付物。
- **两个业务决策**：计费模式=**按存储量付费**（新票 RND-331，回填 `media_files.file_size` NULL + 收紧 NOT NULL，否则发票口径不可信）；云版密钥托管=**官方托管客户私钥**（新票 RND-332，`KeyProvider` 抽象 local_file/kms_envelope 双实现，**须先于 RND-269 首配向导完成**）。
- **重复票清理**：RND-296/298/308 已标 Canceled（分别是 RND-288/289/303 的重复票，同一需求建了两次）。此前"外部联系人列表 API 归 RND-296"应改为归 **RND-288**。
- **新 milestone 体系**（按客户旅程 R1~R5，与既有 6 个后端模块 milestone 并存，用于排"现在该做哪张票"）：R1 前端快赢四页 / R2 开源发布闭环 / R3 Dashboard与Analytics / R4 多租户地基 / R5 云商业化前台。新建 Epic RND-325（R1）下 RND-326（设计系统+侧栏落地，前置阻塞票）→ RND-327/328/329/330（users/audit-log/media/contacts 四页）。
- **排序约束**：RND-324（语言切换弹层窄屏溢出修复，改 `review_console.html:69` 的 `.lang-menu`）**必须先于 RND-326 合入**——RND-326 会重写整段 `<nav>`（含语言切换 markup），先后顺序反了会静默吃掉 RND-324 的修复且无测试能发现。Linear 已加 `RND-324 blocks RND-326`。
- **权威新文档**：`docs/ticket-autopilot-workflow.md`（本轮并行策略/文件所有权矩阵/风险分级，不与 DEV_AGENT_RULES.md 冲突）；`deliverables/final-roadmap-2026-07-29.md`（取代 `page-launch-roadmap.md`/`context-pack-handoff.md` 作为最新路线图结论，旧文件保留作历史记录）。
- 以上未逐条重新核实 Linear 当前状态，下次操作这些票前建议自行 `get_issue` 确认。

## 配置中心 epic RND-244（设计冻结）
- 12 子票 RND-245~256；混合存储 DB>env>默认、Fernet 加密、热加载、复用登录态
- **D1 冻结：复用 SSR+vanilla JS（review_console 模板+base.css），不引 React**；G1 仅导 .env / G2 无 tenant_id / G3 首启触发 / G4 已认证=admin / G5 缺 key fail-closed

## 提示词落点审计纪律（2026-07-29 确立，防 RND-323 类返工）
- **写执行/QA 提示词钉函数落点前，先 grep 真实位置**：`grep -rn "function <name>" backend/app/web/static/console/*.js` + `grep -n "def <name>" backend/app/<module>.py`。console/*.js 经多票迭代函数会跨文件迁移（RND-323 的 `loadCurrentUser` 已从 `console-state.js` 迁到 `api-client.js`）。
- **绝不硬编码会随提交漂移的值**：尤其 `test_http_contract.py` 的 `route_count`（每次加路由都变）。正确写法=「读当前值 N，改 N+delta（本票新增路由数）」，禁止写死具体数字（如 `== 40`/`== 44`）。
- 后端函数落点可用「import 断言 / grep 功能」式前置校验替代死磕行号；行号仅作辅助，函数名才是锚。
- QA 提示词的 diff 集合要与实际改动文件一致（RND-323 曾把未改的 `console-state.js` 列进 diff，已纠）。

## 进行中的链与状态（截至 2026-07-27）
- 重构 epic RND-212 收口：12 子任务全 Done；main.py=composition root，import-boundary 测试锁定
- RND-217 已实现未提交（等 QA+用户许可）；RND-218~221 在 main 工作树未提交（221 QA PASS 16/16）；222/223/224 pending
- 搜索簇：RND-159/228/229 已 merged+deployed；RND-230（多过滤器）已解锁 Backlog；RND-240 搜索跳转 8s 性能 ticket+提示词就绪
- RND-226 先行单独做，merge 后 RND-210 从 main 切分支（提示词均已备）
- FROZEN（WeCom 企业更名阻塞，勿动）：RND-104/107/108/129/130/175
- RND-242 三项已拍板（2026-07-27 22:20）：① slug=`crowntime-wecom-archive` ②「365」→自部署白标能力（logo 上传+自有域名，新票 RND-259 Backlog，挂 RND-244 方向）③ 官网留邮箱 hs@crowntime.cn、撤地址电话。RND-242→Todo，提示词已交付（`.workbuddy/prompts/rnd-242-execution-prompt.md`+`rnd-242-qa-prompt.md`）。核心纪律：**B 层生产路径（/srv/apps/wecom-archive-365、systemd 单元名、deploy.yml、.env.example、backend/scripts）严禁改**（⚠️ 2026-07-29 更正：`.github/workflows/deploy.yml` 已核实是真实运行的 main→生产自动部署管道——测试门禁+Alembic 迁移检查+schema-drift 检测+SSH 部署+flock 防并发+失败可回滚，不是待建或被 RND-237 阻塞的东西；**RND-237 范围已收窄为仅 LICENSE/README/.gitignore/CLA/WeCom SDK 说明包装，不涉及部署，此前"由 RND-237 发布导出处理"的归因是错的，撤回**）；中文旧名 tracked 0 命中，实际改 README 标题/slug+官网字段+docs 产品名
