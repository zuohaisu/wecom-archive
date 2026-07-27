# 365 企微会话存档（Crowntime WeCom Archive）· 开源前综合审计报告

**日期**：2026-07-27
**工作流**：综合审计（工作流 1 代码审查 + 工作流 5 技术债评估 + 开源就绪专项）
**参与成员**：Cody（代码审查师）/ Archi（系统架构师）/ Rex（SRE 工程师）/ Tessa（测试专家）/ Docu（技术文档师）

---

## 📌 TL;DR（执行摘要）

- **整体结论**：应用**代码层安全基线扎实**（认证 fail-closed、企微回调验签、解密隔离、防路径穿越、全参数化查询均到位），但**开源适配层存在多处发布阻断项**——当前状态判定为「**未就绪 / Not Ready**」，不应直接开源。
- **严重度分布**：🔴发布阻断 7 项 / 🟠高 10 项 / 🟡中 14 项 / 🟢低 12 项
- **阻塞**：7 项发布阻断（LICENSE+法务文件缺、public 快照内部信息未裁剪、RND-244 配置中心缺失致 app_secret 明文+fail-open、decrypted_payload 死索引+矛盾文档、商标资产未剥离、测试不可复现、无 PR CI 门禁）
- **最关键信号**：不修配置中心/fail-open 与 app_secret 明文存储，开源后凡能拿到 DB 备份者可直接取得 WeCom app_secret；不补 LICENSE 与内部裁剪则构成法律与信息泄漏风险。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🔴 不通过（开源就绪度 Not Ready） |
| 阻塞项数量 | 7 项发布阻断 |
| 关键行动项 | 9 条（见下方行动清单，含 7 阻断 + 2 高危） |
| 建议下一步 | 先把 7 项阻断归入 RND-232 开源发布的 must-fix 清单，再处理 🟠 高危加固项；科迪/阿奇均确认核心代码可复用，工作量集中在「开源适配」而非架构重构 |

---

## 🔴 一、发布阻断项（必须满足方可开源）

| # | 阻断项 | 类别 | 涉及文件 | 说明 | 来源 |
|---|--------|------|---------|------|------|
| B1 | 缺 LICENSE 及 SECURITY/CONTRIBUTING/CODE_OF_CONDUCT | 开源合规 | 仓库根 | 无许可证时代码法律上「保留所有权利」，无法合法开源；GitHub 亦会标记「未配置安全策略」 | Docu F1/F3/F4/F5, Archi F1 |
| B2 | public 快照内部信息未裁剪 | 内部信息泄漏 | `AGENTS.md`、`DEV_AGENT_RULES.md`、`docs/AGENTS.md`、`docs/RND-208/202/207-*`、`docs/system-defect-review-2026-07-24.md`、`docs/trademark-checklist-zuozheng.md`（含个人名）、`DESIGN-linear.app.md`、`docs/ai/*`、`docs/ops/*`、`docs/research/*`、`docs/adr/*`、`design/RND-229-230-*` | 上述均被 git 跟踪，会随净快照外泄负责人实名、内部 Linear 流程、缺陷复盘、第三方字体逆向稿 | Docu F2/F6/F7 |
| B3 | RND-244 配置中心（Fernet + fail-closed）在代码中不存在；`settings.py` 为 fail-open | 安全/密钥管理 | `backend/app/settings.py:36-114`、`backend/app/db/models.py:56,83` | 全仓无 Fernet 实现；`database_url`/`wecom_callback_token` 等缺失时返回空串（fail-open）；`TenantWecomConfig.app_secret` 以明文 `Text` 持久化（模型注释自承 "Phase 3 must encrypt at rest"）。与「缺 key 时 fail-closed」约束直接冲突 | Cody B1, Archi F4, Rex(专项核查) |
| B4 | `decrypted_payload` 死 GIN 索引 + 与 SF-1 矛盾的 docstring | 正确性/schema | `backend/app/db/models.py:256-257,280-283`；`backend/alembic/versions/0001_initial_schema.py` | 该列恒为 NULL 却建了 GIN 索引（每次写入徒增维护），docstring 仍写 "success — decrypted_payload is populated"，会诱导贡献者「补写」明文造成回归。⚠️ 注意：`decrypted_payload` 本身确为恒 NULL（见第八节澄清），问题在索引与文档 | Cody B2 |
| B5 | 商标资产未从净快照剥离 + 三套名称并存 | 商标/品牌 | `backend/app/main.py:87`、`README.md`、品牌 SVG（含注册商标第17528858号）、仓库名「365企微会话存档」 | 公开快照携带注册商标 logo 与「康冠时代」字样，与 IP 方案冲突；三套名称（Crowntime / 康冠时代 / 365）令外部贡献者困惑 | Archi F3, Docu F9 |
| B6 | 测试不可复现：缺 dev 依赖清单 + README 未写如何跑测试 | 测试/可复现 | `backend/requirements.txt`、`Makefile`、`README.md` | `requirements.txt` 仅运行时依赖，pytest/ruff 未声明；README 通篇未提 `make test`，按 README 操作必卡在 `ModuleNotFoundError: pytest` | Tessa #1 |
| B7 | CI 仅 push main 触发，无 PR 测试门禁 | CI/协作 | `.github/workflows/deploy.yml` | 唯一 CI 是 Deploy to Production，外部贡献者 PR 不触发任何测试/质量门禁，回归易漏 | Tessa #2 |

---

## 🟠 二、安全发现（高）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议修复 | 来源 |
|---|--------|------|---------|---------|---------|------|
| H1 | 🟠高 | 性能/资源 | `decrypt_isolation.py:149-191`、`decrypt_worker.py:466-511` | 解密隔离对每一行都 spawn 全新子进程并重载 SDK，大 backlog 时吞吐受 spawn 速率限制 | 提供「批量隔离」/常驻进程池复用；文档标注吞吐特征 | Cody H1, Archi F9 |
| H2 | 🟠高 | 安全/DoS | `backend/app/auth.py:130-143` | `verify_password` 对 PBKDF2 迭代次数无上限校验，被投毒为极大值可拖垮登录 worker（且无速率限制） | 校验 `1 <= iterations <= 上限`；登录接口加基础限流/锁户 | Cody H2 |
| H3 | 🟠高 | 性能/可靠性 | `backend/app/db/session.py:11-18` | `pool_size=2, max_overflow=2` 仅 4 连接且无 `pool_pre_ping`，高并发排队、DB 重启后首请求可能 500 | 调大 pool_size（10-20）+ `pool_pre_ping=True` | Cody H3 |
| H4 | 🟠高 | 部署安全 | `deploy/systemd/`（仅 timer 单元）、`docs/DEPLOYMENT.md:98`、`README.md:114` | 缺随仓库提供的主 API systemd 单元 + nginx/TLS 模板，陌生人易裸跑 `0.0.0.0:8035` 无 TLS 暴露 PII 管理台 | 补加固版参考单元（ProtectSystem=strict、绑定 127.0.0.1）+ nginx TLS 片段（HSTS/X-Frame-Options DENY）或 docker-compose | Rex F1 |
| H5 | 🟠高 | 部署安全 | `auth.py:389,633`、`auth.py:320`、`(.env.example:10)` | `APP_ENV` 默认 development → 会话 cookie 不置 `Secure` | 单元/文档强制 `APP_ENV=production`；或按 TLS 终止独立开关判定 | Rex F2 |
| H6 | 🟠高 | 供应链 | `backend/requirements.txt:9` | `httpx>=0.24.0` 仅下界、无上限；仓库无 pip-audit/Dependabot。已核对 pinned 版本（fastapi/uvicorn/sqlalchemy/cryptography）均在 CVE 修复版之上，**未检出关键 CVE 暴露** | httpx 改精确 pin 或 `~=`；CI 加 `pip-audit`/Dependabot | Cody M4 |
| H7 | 🟠高 | 测试/安全 | `test_decrypt_structured_content.py:200-207`、`test_decrypt_worker_service.py` | SF-1 对 `run_decrypt_once` 仅有源码字符串断言（"decrypted_payload" not in source），改 `setattr`/被调函数内写会漏检；成功解密后未运行时断言 `record.decrypted_payload is None` | 增加运行时断言：解密成功后持久化行 `decrypted_payload is None` | Tessa #3 |
| H8 | 🟠高 | 架构/治理 | `test_architecture_boundary.py:70-92` | 护栏白名单 `_FLAT_SERVICE_MODULES` 未登记模块落入 "other" 不受反向依赖约束，外部贡献者易静默违规 | 改为基于路径的通用判定或把「未登记模块」设为 CI 报错 | Tessa #4, Archi F6 |
| H9 | 🟠高 | 文档质量 | `README.md:5`、`docs/ARCHITECTURE.md:9,16,172,281` | 文档仍自述「internal / admin-only / not a public-facing」，与开源定位矛盾 | 改写为「self-hosted 自托管审阅系统」，移除内部化措辞 | Archi F5, Docu F10/F11 |
| H10 | 🟠高 | 内部信息 | `scripts/deploy_server.sh:38`、`.github/workflows/deploy.yml:245,256`、`deploy/systemd/*`、`docs/DEPLOYMENT.md`、`docs/*runbook.md` | 部署脚本/CI/Runbook 硬编码内部路径 `/srv/apps/wecom-archive-365/`、运维用户 `wecomarchive`、GitHub 账号 `zuohaisu` | 内部路径改占位符、GitHub 地址改 public 仓、用户名泛化（或在 public 快照移除生产部分） | Docu F8 |

---

## 🟡 三、中危发现

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议 | 来源 |
|---|--------|------|---------|---------|------|------|
| M1 | 🟡中 | 正确性 | `routers/conversations.py:548-551` | `get_conversation_detail` 未命中时回退 `buckets[0]`，极端情况下展示错误会话参与者 | 未命中显式 404 | Cody M1 |
| M2 | 🟡中 | 可维护性/安全 | `routers/conversations.py:444-448` | `WEARCHIVE_LEGACY_TIMELINE` 隐藏回滚开关启用分歧代码路径，无鉴权/告警 | 开源前删除 legacy 路径与开关 | Cody M2 |
| M3 | 🟡中 | 安全/纵深防御 | `sdk/wecom_sdk.py:24-28` | `ctypes.CDLL(lib_path)` 加载任意路径 .so 无目录约束，配置被污染时可 RCE | lib_path 限定可信目录白名单+前缀校验 | Cody M3 |
| M4 | 🟡中 | 部署安全 | `routers/auth.py:290-394` | `/api/auth/password/login` 无暴力破解防护 | 代理层 `limit_req`/`fail2ban` 或应用层失败计数 | Rex F5 |
| M5 | 🟡中 | 部署安全 | `deploy/systemd/*.service`（EnvironmentFile=.../.env） | `.env` 未在文档/仓库强制 600 权限，默认 644 时本机任意用户可读全部密钥 | 文档显著要求 `chmod 600 backend/.env` 且 owner=运行用户 | Rex F6 |
| M6 | 🟡中 | 部署安全 | `backend/app/main.py` | 无 HSTS/X-Frame-Options/CSP/Referrer-Policy（依赖代理） | 在 nginx 模板内置安全响应头 | Rex F7 |
| M7 | 🟡中 | 可观测性 | `Makefile`、`pyproject.toml` | 无 `pytest-cov`、无覆盖率报告/门槛 | 引入 `pytest-cov`，设关键安全模块最低行覆盖门禁 | Tessa #5 |
| M8 | 🟡中 | 可维护性 | `tests/test_reachability_audit.py`（fixture 寄生） | 无 conftest，fixture 定义在测试模块被他处 import，违反常规、F811 豁免掩盖重定义 | 迁移到 `tests/conftest.py` | Tessa #6 |
| M9 | 🟡中 | 配置/安全 | `app/settings.py`、`test_rnd_223_settings.py` | settings 承载密钥字段仅 thin wrapper 测试触达，缺通用契约（缺失/空白/大小写）测试 | 加 `get_*_settings` 契约测试 + secret 不出现于 `__repr__`/日志 | Tessa #7 |
| M10 | 🟡中 | 方言一致性 | `tests/fakes.py`、离线测试 | 离线 SQLite（JSONB 用 TEXT）与生产 PG JSONB 方言分叉，大量 JSONB 查询仅在 SQLite 验证 | 扩大 PG 测试段覆盖 JSONB 查询 | Tessa #8 |
| M11 | 🟡中 | API 设计 | `routers/conversations.py:298`、`messages.py:109`、`search.py:177` | 公开 API 无 `/api/v1` 版本化前缀 | 引入 `/api/v1` 前缀并写 ADR | Archi F7 |
| M12 | 🟡中 | 可移植性 | `db/models.py:19`（JSONB 导入） | 模型强依赖 Postgres，与「SQLite 兼容测试」声明冲突，外部贡献者本地 SQLite 建表失败 | 明确支持矩阵（仅 PG）或加 JSON 抽象垫片 | Archi F8 |
| M13 | 🟡中 | 扩展性文档 | `scripts/*_once.py` | 工作者为一次性脚本，扩展靠外部 cron/systemd，文档未显式说明扩展边界 | ARCHITECTURE 标注「水平扩展=外部编排/多进程」 | Archi F11 |
| M14 | 🟡中 | 品牌一致性 | `.env.example:2,30,237,243...` | 模板标题仍旧品牌「365 WeCom Archive」且注释含内部路径 | 改「Crowntime WeCom Archive（康冠时代）」+ 泛化路径 | Docu F9 |

---

## 🟢 四、低危 / 已确认良好项（摘要）

- **代码层安全良好项**（Cody）：WeCom 回调 SHA1 排序签名+AES-CBC+corp_id 校验、认证 fail-closed、全参数化查询+ILIKE 转义、媒体文件 `resolve()+relative_to` 防穿越、解密隔离超时/崩溃回收完整、配置无硬编码真实密钥/内部 IP、无 `debug=True`/宽松 CORS/`shell=True`。
- **部署层良好项**（Rex）：fail-closed 认证/回调/存储/DB 全面落地；无 `CORSMiddleware`/宽松跨域；OAuth `code`/`state` access log 已 redact；`.env` 真实密钥已被 gitignore 隔离；`/health*` 不泄露 PII。
- **架构良好项**（Archi）：分层方向清晰、`main.py` 组合根纯净（实测反向依赖 0 违规）、存储 provider 抽象合理、`.so` 已 gitignore、README 已有腾讯商标免责声明。
- **测试良好项**（Tessa）：101 测试文件/2325 用例收集干净、安全关键子集 123 用例全绿、基本离线可跑（SQLite+FakeWecomSdk+Qiniu mock）。
- **文档良好项**（Docu）：`docs/API.md` 质量高且无泄漏、`README` 已有「非腾讯官方」免责声明、`design/brand/` 合规完整。
- **待办低优**：登出接口 CSRF（已被 samesite=lax 缓解）、`uvicorn.access` 仅脱敏 OAuth 回调（建议反代侧同样脱敏）、OAuth 回退硬编码 localhost（建议从 Host/BASE_URL 推导）、Qiniu provider 建议补 S3 示例、缺 `pyproject.toml [project]` 元数据、`ProxyHeadersMiddleware` 文档说明。
- **合规披露要点**（非缺陷）：`content_text`（解密明文）按设计持久化并对外服务，是归档/检索核心能力；开源需确保 README/合规文档明确「系统会持久化并对外提供解密会话原文」，建议默认更严格访问控制/审计日志以符合 PIPL/个保法（Cody L4）。

---

## ⚠️ 五、SF-1 数据最小化 — 重要澄清（避免误判）

审查中出现一处成员结论冲突，主理人已裁定：

- **阿奇（Archi F2）**认为「代码实际在解密成功后把解密内容落库，违反 SF-1」。
- **科迪（Cody B2）**grep 全仓确认「无任何业务代码给 `record.decrypted_payload` 赋值，reparse 只写 `structured_content`」；项目工作记忆亦锁定 `decrypted_payload 恒 NULL`。
- **裁定**：`decrypted_payload` 确为恒 NULL，**SF-1 未被违反**。阿奇所言「解密内容落库」实为 `content_text`/`structured_content` 的持久化——这是会话归档/检索产品的**设计本意**（否则无法检索），属合规披露范畴而非代码缺陷。
- **真正需修的问题**（已列入 B4）：① 模型 docstring 与 SF-1 矛盾（称 success 时 populated）；② 为恒 NULL 的列建了死 GIN 索引；③ 应加 `CHECK (decrypted_payload IS NULL)` 从 DB 层面锁死，并补运行时断言（见 H7）。

---

## 📐 六、建议写入的 ADR（架构决策记录）

1. **ADR · 许可证、商标与 CLA 策略** — 记录许可证选型（Apache-2.0/AGPL-3.0 + 商业许可双轨）、商标保留与商标政策、CLA 要求（回应 B1/B5）。
2. **ADR · 数据最小化与解密内容留存边界（SF-1 真实边界）** — 明确 `decrypted_payload` 恒 NULL、`content_text`/`structured_content` 按策略留存、重解析走隔离子进程的设计契约（回应 B4/第五节）。
3. **ADR · 公开 API 版本化策略** — `/api/v1` 前缀、向后兼容与弃用规则（回应 M11）。
4. **ADR · 产品命名与品牌抽象策略** — 单一可配置产品名、中性占位 logo、商标作可配置品牌保留（回应 B5）。
5. **（追加）ADR · 架构护栏从白名单升级为路径通用判定** — 回应 H8。

---

## 🛡️ 七、给开源用户的部署安全检查清单（Go/No-Go，来自 Rex）

上线前必须全为 **Go**；任一为 **No-Go** 即禁止暴露公网：

- [ ] `backend/.env` 由 `.env.example` 复制生成，`chmod 600`、owner=运行用户（M5）
- [ ] `APP_ENV=production` 已设置（否则 cookie 非 Secure，H5）
- [ ] `AUTH_MODE` 已显式选定：推荐 `wecom`；若 `password` 必生成 `ADMIN_PASSWORD_HASH`、禁用 `admin` 默认用户名（H5/M4）
- [ ] 已部署反向代理（nginx）终止 TLS，全站 HTTPS+HSTS；**应用仅监听 `127.0.0.1:8035`，绝不 `0.0.0.0`**（H4/M6）
- [ ] 已提供并启用主 API systemd unit（非 root、ProtectSystem=strict、NoNewPrivileges、UMask=0077）（H4）
- [ ] PostgreSQL 强口令且非默认 `change-me`；`DATABASE_URL` 不进前端/日志
- [ ] `/api/wecom/archive/events` 已在代理层限制为企微回调来源 IP（Rex F4）
- [ ] `WECOM_CALLBACK_TOKEN` / `WECOM_CALLBACK_ENCODING_AES_KEY` 已配置
- [ ] SSL 续期 per-domain env `mode 600`、`acme.sh account.conf` 权限受限
- [ ] **No-Go 触发线**：`0.0.0.0` 无 TLS ／ 默认 `change-me` 口令 ／ `.env` 权限 644 ／ `AUTH_MODE=password` 且 `ADMIN_PASSWORD_HASH` 为空 → 禁止上线

---

## ✅ 八、行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | 落定 LICENSE + SECURITY.md + CONTRIBUTING.md + CODE_OF_CONDUCT.md（建议 Apache-2.0 + CLA） | Docu / 主理人 | P0（阻断） | 开源前 |
| 2 | 编制 public 净快照「文档白/黑名单」，从快照剔除 AGENTS/DEV_AGENT_RULES/RND 工单/缺陷复盘/DESIGN-linear/AI 文档/部署路径与账号 | Docu / Rex | P0（阻断） | 开源前 |
| 3 | 落地 RND-244 配置中心：Fernet 加密 `app_secret` 等敏感配置落库；关键项缺失时显式抛错（fail-closed），消除 `settings.py` 空串默认 | Cody / Archi | P0（阻断） | 开源前 |
| 4 | 清理 `decrypted_payload`：修正矛盾 docstring、删除死 GIN 索引、加 `CHECK (IS NULL)` 锁死、补运行时断言 | Cody / Tessa | P0（阻断） | 开源前 |
| 5 | 剥离商标资产 + 统一单一可配置产品名（三套名称收敛） | Archi / Docu | P0（阻断） | 开源前 |
| 6 | 补 `requirements-dev.txt`（pytest/ruff）+ `make setup`/`make test` 一键可复现 + README「Running tests」段 | Tessa | P0（阻断） | 开源前 |
| 7 | 新增 `ci.yml` 在 `pull_request` 触发 lint→typecheck→test 门禁，与 deploy 复用同套命令 | Tessa / Rex | P0（阻断） | 开源前 |
| 8 | 补开箱即用安全部署默认：主 API systemd 单元 + nginx/TLS 模板 + 强制 `APP_ENV=production` + `.env` 600 权限文档 | Rex | P1（高） | 发布首版 |
| 9 | 安全加固：`verify_password` 迭代上限、DB 连接池调大+pre-ping、httpx 锁定、登录限流、`.so` 路径白名单 | Cody | P1（高） | 发布后迭代 |
| 10 | 解密批量隔离/进程池复用（H1）、架构护栏路径通用化（H8）、API `/api/v1` 版本化（M11） | Archi / Cody | P2（中） | 后续迭代 |

---

## ⚠️ 九、待完善 / 已知局限

- 本次为**静态审查 + 护栏脚本实测**，未做动态渗透测试或依赖漏洞扫描（仅人工核对 pinned 版本 CVE 状态）。建议开源前跑一次 `pip-audit` + 轻量 DAST。
- 成员结论基于当前工作树（含未提交改动 RND-217/218-221/222-224 等），最终审计应基于**待发布的净快照**再核验一遍。
- `content_text` 明文留存为设计本意，但 PIPL/个保法合规口径需法务最终确认并在文档明示。
- 大模块（structured_message_parser 1389 行等）为无形上手墙，未强拆但需补模块级 ARCHITECTURE 子图。

---

## 📚 十、数据来源 & 成员产出索引

- **Cody（代码审查师）原始产出**：安全/性能/正确性审查 —— 2 项发布阻断（B3 配置中心缺失/fail-open + app_secret 明文、B4 decrypted_payload 死索引）、🟠 H1/H2/H3/H6、🟡 M1/M2/M3、🟢 L1-L4 与正面项。
- **Archi（系统架构师）原始产出**：架构合理性审查 —— 🔴 F1 缺 LICENSE、F2 SF-1 声明冲突（见第五节澄清）、F3 商标未剥离；🟠 F4 明文 app_secret、F5 文档内部化、F6 护栏可绕过；🟡 F7-F11；🟢 F12-F15；4 条 ADR 建议。
- **Rex（SRE 工程师）原始产出**：部署/运营安全审查 —— 🔴 F1 缺安全部署默认；🟠 F2/F3/F4；🟡 F5/F6/F7；🟢 F8-F12；fail-open 专项核查结论（应用内无 fail-open）；Go/No-Go 部署清单。
- **Tessa（测试专家）原始产出**：测试覆盖评估 —— 🔴 #1 缺 dev 依赖/README 无测试指引、#2 无 PR CI；🟠 #3 SF-1 运行时护栏、#4 护栏白名单绕过；🟡 #5-#8；🟢 #9/#10；就绪度评价与摩擦点总结。
- **Docu（技术文档师）原始产出**：文档就绪审查 —— 🔴 F1 缺 LICENSE、F2 员工/流程泄漏、F3-F7 法务文件缺失+内部文档外泄；🟠 F8-F12；🟢 F13-F15；开源文档发布清单。
- **主理人（甄宇航）综合裁定**：去重合并 5 路结论、按严重度排序、澄清 SF-1 冲突（第五节）、生成 ADR 清单与 Go/No-Go 清单。

---

> 本报告由工程保障团队 AI 协作生成，关键决策（许可证选型、商标剥离、PIPL 合规口径、配置中心实现）请由人类工程负责人与法务复核。
