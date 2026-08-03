[Goal check] This work advances 开发（Development） by 消除企微加密回调的攻击者可控 500、阻止 Uvicorn 记录敏感查询串，并用可持续自动化测试固定安全响应契约。

# RND-105 修复开发提示词（Developer Prompt）

> 执行 Agent：**Claude Code，High effort**。开始前必须完整阅读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md` 与本提示词。RND-105 当前保持 In Progress；本轮只修独立 QA 已确认的三个 blocker，不实现 RND-107。

## ⚡ 立即执行，不要询问意图

你现在收到的是一个已批准、待立即执行的修复任务。不要反问目标，不要先输出计划等待确认；完成 Preflight 后直接从 AC-1 开始实现。

唯一允许停下的情况：工作树无法安全 fast-forward、必须修改「只读文件」、或修复需要改变已批准的 HTTP/协议边界。此时输出 `BLOCKED_NEEDS_HUMAN` 和最小证据，不要 stash、reset、覆盖他人改动或自行扩大范围。

---

## 1. 任务身份

- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-105「实现企微加密回调校验」— 安全回归修复
- Linear URL：https://linear.app/xyzhs1897/issue/RND-105/实现企微加密回调校验
- 优先级：High
- 风险等级：**R2**（公开安全入口、密码学输入错误分类、访问日志脱敏）
- 执行阶段：应用代码与自动化测试
- 后续阶段：Miss Hermes / 运维 Agent 修改生产 Nginx并部署；Codex 独立回归验收
- 阻塞关系：RND-105 继续阻塞 RND-107；本票不依赖、也不实现 RND-107

## 2. Preflight（先同步，再碰代码）

### P-1 读取规则与当前实现

```bash
sed -n '1,260p' DEV_AGENT_RULES.md
sed -n '1,260p' docs/ticket-autopilot-workflow.md
sed -n '1,280p' backend/app/routers/wecom_events.py
sed -n '1,190p' backend/app/main.py
rg -n "wecom_callback|archive/events|Redact.*Callback|trigger_recent_image_download" backend/app backend/tests
```

### P-2 采集现场并 fast-forward `main`

先记录，不要假设提示词中的 SHA 仍然最新：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
git fetch origin
git pull --ff-only origin main
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

判定：

1. 工作树干净且 `HEAD == origin/main` → 继续。
2. 存在用户/他票改动但不与本票拥有文件冲突 → 保留并在 QA Summary 中分离归因；不要修改。
3. 本票拥有文件已有未知改动、pull 不能 fast-forward、或本地有未识别 ahead commit → `BLOCKED_NEEDS_HUMAN`；禁止 stash/reset/checkout 覆盖。
4. 不创建分支，不 commit，不 push。

### P-3 既有独立 QA blocker（必须逐个重放）

当前生产同代码已观察到：

- 真实企业微信加密 GET 能返回 200，happy path 已成立；
- 签名正确但 padding 非法 → 500；
- 签名正确但 envelope `msg_len` 越界 → 500；
- Uvicorn journal 与 Nginx access log 保存完整 callback query；
- 仓库没有 callback 密码学与日志专项测试；
- Token、EncodingAESKey、Archive Secret、OAuth Secret 未直接出现在日志中，因此**没有密钥轮换依据**。

本轮必须修 blocker，不得重写已经工作的 happy path。

---

## 3. 背景与当前代码边界

- `backend/app/routers/wecom_events.py` 已实现 GET 的 SHA1 排序验签、`hmac.compare_digest()`、AES-CBC、PKCS#7、WeCom envelope 与 Corp ID 校验。
- GET 当前把所有解密/解析异常映射为 500，且最终 `msg.decode("utf-8")` 位于异常保护之外；攻击者可控的 base64、ciphertext、padding、长度和 UTF-8 都可能形成 500。
- POST 当前只提取 `<Encrypt>`、验签、best-effort 触发 RND-172 的近期图片 dispatcher，然后返回 `ok`。这是 RND-105 的既有边界；**不是完整 Archive Worker**。
- `_extract_encrypt()` 对 CDATA 做 UTF-8 decode，畸形字节也属于攻击者可控输入，必须安全 400。
- `backend/app/main.py` 的 Uvicorn access filter 只处理 OAuth `/api/auth/wecom/callback?`，没有处理 `/api/wecom/archive/events?`。
- 生产 Nginx 是运维管理资产，不在仓库开发 Agent 权限内；应用开发完成后必须给出精确运维 handoff，但不得 SSH 修改生产。

### 必须保持的不变量

- `GET /api/wecom/archive/events` 与 `POST /api/wecom/archive/events` 路径、方法及无尾斜杠行为不变。
- 路由继续公开，不增加 Session、Basic Auth、CSRF 或租户登录依赖。
- 无效签名和 Corp ID mismatch 继续返回 403。
- 缺少必填 query 参数继续由 FastAPI 返回 422。
- 有效 GET 继续返回解密后的纯文本 `echostr`。
- 有效 POST 继续快速返回纯文本 `ok`。
- 不触发完整 Archive Worker，不修改同步、解密、入库或 timer。
- 不启用 `EVENT_MEDIA_DOWNLOAD_ENABLED`，不改变 RND-172 dispatcher 语义。

---

## 4. 目标（Goal）

以最小代码改动让公开回调入口对所有攻击者可控畸形输入 fail-safe：协议拒绝使用稳定 4xx、日志只保留固定安全事件和无查询串的访问元数据，并把全部边界固化为可持续运行的自动化测试。

## 5. 范围边界

### In scope

1. GET 请求控制的非法 base64、AES ciphertext、block length、padding、envelope、Corp/消息编码异常统一返回**通用且不泄密的 400**。
2. POST XML/CDATA/编码解析异常安全返回 400；无效签名 403；缺少 `<Encrypt>` 400。
3. 任何攻击者可控输入不得形成未处理异常或 500。
4. 保留服务器配置错误与攻击输入错误的边界：缺失/非法的服务端 callback Token/AES Key 可保持通用 500，但响应和日志不得包含配置值或异常文本。
5. accepted/rejected 使用固定、脱敏、可搜索的 INFO 事件；reason 只能来自代码内稳定 allowlist，不记录 query、body、密文、明文、Corp ID、tenant ID、签名、nonce 或异常字符串。
6. 扩展 Uvicorn access-log filter：`/api/wecom/archive/events?...` 必须变成不含参数名和值的安全路径，同时保持 OAuth callback 既有脱敏行为。
7. 新增 focused test file，覆盖本提示词全部 AC。
8. 输出给运维 Agent 的 Nginx handoff 检查清单；不执行运维变更。

### Out of scope

- 不实现、设计或部分实现 RND-107；不解密 POST 业务事件，不识别事件类型，不触发 `run_archive_worker_once.py`。
- 不修改 RND-108 域名、验证文件、企业可信 IP 或企业微信后台。
- 不修改 Nginx、systemd、`.env`、生产日志、生产数据库或企业微信配置。
- 不清空/删除/轮转既有 journald 或 Nginx 日志。
- 不轮换 Token、EncodingAESKey 或任何 Secret。
- 不新增队列、Celery、Redis、后台 worker、数据库表或 migration。
- 不改变媒体 dispatcher、timer、Archive Worker 或共享锁。
- 不新增路由，不改 OpenAPI 公共路径，不改 CI/CD。
- 不顺手重构 OAuth 日志、认证系统或其它 router。

### 本工单拥有的文件（只许写这些）

- `backend/app/routers/wecom_events.py` — 仅错误分类、固定安全日志及必要的安全解析 helper
- `backend/app/main.py` — 仅扩展现有 Uvicorn callback query 脱敏 filter；不得新增业务逻辑
- `backend/tests/test_wecom_events.py` — 新建，覆盖本票全部协议与日志 AC

### 本工单只读、绝不可写的文件/资产

- `backend/app/media_event_dispatch.py` — RND-172；只读回归
- `backend/app/routers/sync.py`、`backend/scripts/run_archive_worker_once.py` — RND-107/RND-106 边界；禁止接线
- `backend/app/settings.py`、`backend/app/config/**` — 配置读取契约不变
- `backend/app/routers/auth.py` — OAuth 行为只做 access-filter 回归，不改 route
- `backend/tests/test_http_contract.py` — 本票不新增 route，不应改快照
- `deploy/systemd/**`、`scripts/deploy_server.sh`、`.github/**` — 禁止修改
- `/etc/nginx/**`、生产 `.env`、journald、生产 access logs — 由 Miss Hermes / 运维 Agent 处理

若验证闸迫使修改清单外文件，停止并给出失败测试与最小授权请求。

---

## 6. 验收标准（Acceptance Criteria）

### AC-1 — 基线与同步

- **AC-1a**：开发开始前执行 fetch + `pull --ff-only`，记录同步后的 `HEAD` 与 `origin/main`。
- **AC-1b**：开发 Agent 没有 stash/reset、建分支、commit 或 push。

### AC-2 — GET happy path 保持

- **AC-2a**：使用合成 Token、43 字符 EncodingAESKey、Corp ID 和 WeCom envelope，签名正确的 GET 返回 200。
- **AC-2b**：响应为 `text/plain`，body 逐字节等于合成 `echostr` 明文。
- **AC-2c**：验签继续使用排序 SHA1 与 `hmac.compare_digest()`。

### AC-3 — GET 安全拒绝与错误分类

- **AC-3a**：无效签名返回 403。
- **AC-3b**：Corp ID mismatch 返回 403。
- **AC-3c**：非法 base64、空/截断/非 AES block ciphertext、错误 padding byte、错误 padding block 均返回 400。
- **AC-3d**：plaintext 少于 20 bytes、`msg_len` 越界、Corp ID 非 UTF-8、消息明文非 UTF-8 均返回 400。
- **AC-3e**：缺少 query 参数返回 422；无尾斜杠 URL 不发生 redirect。
- **AC-3f**：参数化遍历全部攻击者可控 malformed cases，断言没有一个返回 500。
- **AC-3g**：错误响应只含稳定通用 detail，不包含异常类名、异常文本、payload、签名、Corp ID 或配置值。

### AC-4 — 服务端配置失败边界

- **AC-4a**：Token 未配置、EncodingAESKey 未配置或服务端 key 长度/解码非法时可返回通用 500。
- **AC-4b**：AC-4a 的响应与日志不包含配置值、长度、前后缀、异常文本或 traceback。
- **AC-4c**：测试明确区分“服务端配置故障 500”和“请求输入故障 4xx”，不得用一个大而宽的 `except Exception` 混淆二者。

### AC-5 — POST 安全接收保持

- **AC-5a**：包含可解码 `<Encrypt>` 且签名正确的 POST 返回 200 `text/plain` body `ok`。
- **AC-5b**：无效 POST 签名返回 403，且 media trigger 调用次数为 0。
- **AC-5c**：缺少 `<Encrypt>` 返回 400，且 media trigger 调用次数为 0。
- **AC-5d**：畸形 XML/CDATA、非法 UTF-8 或其它攻击者可控解析异常返回 400，不返回 500。
- **AC-5e**：有效 POST 可保留现有 RND-172 best-effort dispatcher 调用，但测试必须证明 Archive Worker 调用次数为 0。
- **AC-5f**：POST 响应不等待媒体 sweep 或完整 worker；focused test 使用可控替身证明 route 立即返回。

### AC-6 — 固定安全业务日志

- **AC-6a**：有效 GET/POST 各产生固定 INFO accepted 事件。
- **AC-6b**：每类拒绝产生固定 INFO rejected 事件，reason 来自代码内 allowlist。
- **AC-6c**：caplog 递归检查日志文本，不含 Token、EncodingAESKey、Secret sentinel、完整或局部 query value、`msg_signature` 值、timestamp 值、nonce 值、`echostr`、`<Encrypt>`、Corp ID、tenant ID、明文消息、异常文本或 traceback。
- **AC-6d**：生产运行配置下该 module 的 INFO 事件实际可被日志系统采集；不得只在 `caplog.set_level()` 后才出现。

### AC-7 — Uvicorn access log 脱敏

- **AC-7a**：输入 `/api/wecom/archive/events?msg_signature=...&timestamp=...&nonce=...&echostr=...` 的 access record，经 filter 后只保留 callback path 与统一 redaction marker（或完全去除 query）。
- **AC-7b**：输出不包含四个 query 参数的名字或值，不包含原始 query delimiter 后的内容。
- **AC-7c**：method、脱敏 path、HTTP version 与 status 仍可由 access record 保留。
- **AC-7d**：既有 `/api/auth/wecom/callback?...` 脱敏回归继续通过。
- **AC-7e**：普通非敏感路径行为不变。

### AC-8 — 范围与架构回归

- **AC-8a**：GET/POST 路径、公开访问、response type 与 happy path 不变。
- **AC-8b**：`wecom_events.py` 没有 import `app.main`；`main.py` 只保留 composition/logging wiring。
- **AC-8c**：没有 Archive Worker、timer、shared lock、media feature flag、数据库 schema、Nginx、systemd、CI 或密钥改动。
- **AC-8d**：`make verify`、focused tests 与 architecture boundary 全绿。

### AC-9 — 运维 handoff（开发 Agent 只写在 QA Summary，不改生产）

QA Summary 必须包含：

1. 部署后的应用 SHA；
2. Miss Hermes 需为**精确路径** `/api/wecom/archive/events` 设置不含 `$args`、`$request`、`$request_uri` 的安全 access log format，或仅对该精确 location 关闭 access log；不得关闭整站日志；
3. Nginx 仍需保留 method、`$uri`、status、latency/request time，可选 request ID；
4. 先备份、`nginx -t`，再 reload；这些动作必须由运维 Agent 执行并单独获权；
5. 不删除历史日志，不轮换密钥；按现有保留策略评估历史记录；
6. 部署后由真实企业微信 GET 重新产生 fixed accepted 证据，禁止 QA 在生产伪造有效签名。

---

## 7. 实现指引

1. 先写/补 focused tests，让既有 500 与 access-log 泄露出现 RED。
2. 把“服务端配置读取/校验”与“请求控制的 payload 解密/解析”异常边界分开。
3. 对请求输入失败返回统一 400；不要把底层 cryptography/base64/Unicode 异常文本放进 HTTP detail 或日志。
4. 把 `msg.decode("utf-8")` 纳入请求输入的安全错误边界。
5. 对 `_extract_encrypt()` 的解码错误显式 fail-safe。
6. 扩展现有 access filter，而不是新增第二套互相竞争的 filter；保留 OAuth 回归。
7. 日志 reason 使用常量/allowlist，不记录 `str(exc)`、`repr(exc)` 或 request 数据。
8. 不为了测试方便放松签名、Corp ID 或 AES envelope 校验。

## 8. 验证命令（确定性闸）

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/wecom-rnd105-pycache \
  .venv/bin/python -m pytest backend/tests/test_wecom_events.py -q

PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/wecom-rnd105-pycache \
  .venv/bin/python -m pytest \
  backend/tests/test_auth.py \
  backend/tests/test_password_auth.py \
  backend/tests/test_http_contract.py \
  backend/tests/test_architecture_boundary.py -q

make verify
git diff --check
git diff --name-only
git status --short --branch
git log origin/main..HEAD
```

通过条件：focused tests 覆盖 AC-2～AC-7，全部命令 exit 0，且 diff 只包含拥有文件。

## 9. Definition of Done（开发阶段）

- [ ] 原两个 500 blocker 已有先红后绿测试。
- [ ] 全部攻击者可控 malformed cases 都是 4xx，零 500。
- [ ] Uvicorn callback query 不再进入 access log。
- [ ] 固定 INFO accepted/rejected 日志脱敏且可实际发出。
- [ ] POST 仍不触发 Archive Worker。
- [ ] focused tests 与 `make verify` 全绿。
- [ ] diff 只包含 3 个拥有文件。
- [ ] QA Summary 含 AC 对照、命令结果、文件清单、风险和 AC-9 运维 handoff。
- [ ] 未修改生产、未清日志、未轮换密钥、未 commit、未 push、未建分支。
- [ ] 明确写出：**开发完成不等于 RND-105 可关闭；必须等待部署、Nginx 运维变更和 Codex 生产回归。**

## 10. 风险与回滚

- 风险：过宽异常捕获可能把服务端配置故障误报为客户端 400；过窄捕获可能继续漏出攻击输入 500；access filter 若误伤普通路径会降低诊断能力。
- 回滚：只回滚本票拥有文件的增量；不得回滚历史 callback 实现、RND-172 媒体逻辑或他票改动。
- 运维回滚：由 Miss Hermes 使用 Nginx 备份恢复精确 location/log format，执行 `nginx -t` 后 reload；开发 Agent 不执行。

## 11. 硬性禁止

- 不输出、复制或写入任何真实 Token、Secret、EncodingAESKey、完整签名、完整密文或消息正文。
- 不在生产构造有效回调，不读取/修改企业微信后台。
- 不使用真实生产 Secret 编写测试；全部使用合成 sentinel。
- 不 commit、不 push、不建分支、不改 git 历史。
- 不修改 Nginx、systemd、`.env`、数据库、CI/CD 或生产日志。
- 不实现 RND-107，不把局部媒体 dispatcher 描述成 Archive Worker。
