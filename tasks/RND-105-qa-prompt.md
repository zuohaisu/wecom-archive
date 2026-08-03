[Goal check] This work advances 独立验收（QA） by 重放 RND-105 的三个既有 blocker，并用代码、自动化测试、生产入口和脱敏日志证据判断是否可以关闭。

# RND-105 修复独立回归验收提示词（QA Prompt）

> 执行 Agent：**Codex，High reasoning**。本提示词用于应用修复已部署、Miss Hermes 已完成精确 Nginx 日志配置之后的 closure QA。你是独立 QA，不是开发或运维 Agent。

## ⚡ 立即执行，不要询问意图

你收到的是已经批准的独立验收任务。不要先问“是否开始”，不要让开发 Agent 自报结果替代取证；从 Preflight 开始直接执行。

只读检查无需许可。唯一允许暂停并产出 `BLOCKED` 的情况：生产版本/运维变更尚未部署、无法取得真实企业微信 post-deploy GET 证据、或生产只读访问不可用。`BLOCKED` 必须写进 verdict，不是向用户反问。

---

## 1. 任务身份

- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-105「实现企微加密回调校验」— 安全修复回归验收
- Linear URL：https://linear.app/xyzhs1897/issue/RND-105/实现企微加密回调校验
- 风险等级：**R2**；生产验证为只读
- QA Agent：Codex，High reasoning
- 输入：`tasks/RND-105-dev-prompt.md`、开发 diff/QA Summary、部署 SHA、Miss Hermes Nginx 变更记录
- 输出：`tasks/RND-105-qa-verdict.json`

## 2. 角色与权限

你可以：

- 读取本地仓库、Linear 原始 AC 和只读生产配置；
- 运行本地/隔离自动化测试；
- 通过 SSH 只读查看生产 SHA、service、Nginx effective config、journal 与 access-log 元数据；
- 对公网发出明显无效、无副作用的 GET/POST 拒绝探测；
- 仅写 `tasks/RND-105-qa-verdict.json`。

你不可以：

- 修改应用、测试、Nginx、systemd、`.env`、数据库或企业微信后台；
- reload/restart 服务；
- 清空、删除、truncate 或轮转历史日志；
- 轮换 Token/EncodingAESKey；
- 在生产读取或构造真实 Secret、伪造有效签名、发送会触发媒体/Worker 的有效 POST；
- commit、push、建分支或修改 Linear。

报告和命令输出不得包含 Token、Secret、EncodingAESKey、完整签名、完整密文、完整 query、验证文件内容或解密后的消息正文。

---

## 3. 必须先重放的历史 blocker

以下三项是绑定证据，必须先验，不能因为 happy path 200 或 `make verify` 绿色而跳过：

1. 签名正确、padding 非法曾返回 500；
2. 签名正确、envelope `msg_len` 越界曾返回 500；
3. Uvicorn journal 和 Nginx access log 曾保存完整 `msg_signature` 与 `echostr`；
4. 仓库曾缺少 callback 专项测试。

判定原则：历史日志仍包含旧记录本身不导致本轮 FAIL；必须以**部署/运维变更时间为界**检查新记录。禁止为了让扫描变绿而清理历史审计证据。

---

## 4. Preflight

### P-1 仓库与版本

```bash
git status --short --branch
git diff --stat
git diff
git ls-files --others --exclude-standard
git rev-parse HEAD
git rev-parse origin/main
git ls-remote origin refs/heads/main
git log origin/main..HEAD
```

记录：本地 HEAD、实时 remote main、生产 HEAD、生产 `origin/main`、tracked/untracked 状态。若本地与生产 SHA 不同，必须判断 RND-105 所有文件是否字节一致；不得直接用旧本地测试替代生产版本。

### P-2 生产部署与时间边界

只读确认：

- `wecom-archive-365.service` active；
- 应用监听 `127.0.0.1:8035`；
- 当前 production SHA 包含 RND-105 修复；
- `ActiveEnterTimestamp` 或部署记录；
- Nginx `nginx -T`/配置 mtime 与 Miss Hermes 记录；
- callback Token/AES Key 仅报告 SET/UNSET 与长度；
- 不输出 `.env` 全文。

若应用或 Nginx 尚未部署到本轮目标版本：`verdict=BLOCKED`，`recommended_next_state=BLOCKED_NEEDS_HUMAN`，notes=`PRODUCTION_DEPLOYMENT_PENDING`。不要把“本地代码通过”判为 closure PASS。

### P-3 代码与范围

重点读取：

```text
backend/app/routers/wecom_events.py
backend/app/main.py
backend/tests/test_wecom_events.py
backend/app/media_event_dispatch.py          # 只读边界
backend/scripts/run_archive_worker_once.py   # 只读边界
```

`git diff` 中归因于本票的修改只允许出现在 dev prompt 的三个 owned files。若发现 Archive Worker、timer、media flag、数据库、CI、Nginx仓库脚本等越界修改，判 `SCOPE_VIOLATION`。

---

## 5. 验收方法

每条子 AC 都必须记录：结果、`file:line` 或测试名、命令 exit code、生产证据时间。不得用聚合的“全部通过”代替。

### AC-1 — 真实 GET happy path 与公开路由

- 代码确认排序 SHA1、`hmac.compare_digest()`、AES-CBC、PKCS#7、envelope 和 Corp ID 校验仍完整。
- focused test 的有效 GET 必须返回 `200 text/plain` 且 body 匹配。
- 生产精确无尾斜杠 callback URL 不受登录保护、不跳转旧域名。
- 部署后必须有一次**真实企业微信 GET**的固定 accepted 日志或等价安全证据，且 HTTP 200。
- 不得在生产伪造有效签名。若用户/运维尚未完成 post-deploy 企业微信重新校验，且其它 AC 全通过，判 `BLOCKED`，notes=`PRODUCTION_CALLBACK_REVERIFY_PENDING`，不得 PASS。

### AC-2 — GET 错误分类与零攻击输入 500

重放 focused tests：

- invalid signature → 403；
- Corp ID mismatch → 403；
- invalid base64 / ciphertext / AES block → 400；
- invalid padding byte/block → 400；
- short plaintext / `msg_len` overflow → 400；
- invalid Corp/message UTF-8 → 400；
- missing params → 422；
- 全 malformed 参数集无 500。

必须确认响应 detail 为固定通用文本，无异常内容、payload 或配置值。

### AC-3 — 配置错误边界

- 缺失/非法服务端 Token/AES key 可保持通用 500；
- 响应与日志不含配置值、长度、指纹、异常字符串或 traceback；
- 代码没有把配置错误误报为客户端 400，也没有把请求错误误报为 500。

### AC-4 — POST 安全接收边界

- 合成有效签名 POST → 200 `ok`；
- invalid signature → 403、zero trigger；
- missing Encrypt → 400、zero trigger；
- malformed XML/CDATA/UTF-8 → 400、无 500；
- response 快速返回；
- Archive Worker 调用次数必须为 0。

当前允许的 RND-172 局部媒体 dispatcher 不得被描述成 Archive Worker；本票不得让 RND-107 隐式落地。

### AC-5 — 固定业务日志安全

通过 caplog/LogRecord 检查 accepted/rejected INFO：

- INFO 在正常 logger 配置下可实际发出；
- rejected reason 来自固定 allowlist；
- 日志不含 query 参数名和值、Token、AES key、Secret sentinel、签名、timestamp、nonce、`echostr`、Encrypt、Corp ID、tenant、明文、异常文本或 traceback；
- valid/rejected GET/POST 都有可追踪固定事件。

### AC-6 — Uvicorn access log 安全

自动化测试与生产新日志都必须证明：

- `/api/wecom/archive/events?...` 记录中无 `msg_signature`、`timestamp`、`nonce`、`echostr` 参数名及值；
- method、callback path、HTTP version/status 仍可见；
- OAuth callback 既有 `[REDACTED]` 回归不变；
- 普通路径不被误伤。

生产日志检查必须由脚本在远端解析并**只输出布尔值/计数/状态**；严禁先把原始 callback journal 打到终端再 grep。

### AC-7 — Nginx 精确路径日志安全

检查 effective config，而非只看运维说明：

1. 只针对精确 `/api/wecom/archive/events` location；
2. callback 使用的 log format 不包含 `$args`、`$request`、`$request_uri`，允许 `$request_method`、`$uri`、`$status`、`$request_time`、request ID；或者该精确 location 的 access log 关闭；
3. 整站其它 access log 未关闭；
4. proxy path、headers、TLS 与 health 行为未回归；
5. `nginx -t`/reload 成功证据来自 Miss Hermes 记录；QA 不执行 reload。

使用带明显 QA sentinel 的**无效** GET/POST 发出新请求，然后只读检查部署时间后的日志：

- 若采用安全 custom log format，Nginx 新记录应能看到 method/path/status，但 sentinel、参数名和 query 不存在；
- 若仅对该精确 location 使用 `access_log off`，应证明新请求没有产生 Nginx callback access record，同时 Uvicorn 的安全记录仍保留 method/path/status；
- 两种方案都必须证明整站其它路径的 access log 仍正常。

### AC-8 — 无副作用公网拒绝探测

允许执行：

- missing params；
- obvious invalid signature；
- missing `<Encrypt>`；
- 无效 POST signature。

要求：422/403/400、零 redirect、零 500、无 Worker、无媒体 sweep、无敏感日志。不要尝试“签名正确的畸形生产请求”，该项由合成自动化测试证明。

### AC-9 — 回归、范围与仓库卫生

- focused test、auth/http contract、architecture boundary、`make verify` 全绿；
- GET/POST 路由公开性不变；
- 无 Archive Worker/timer/共享锁/media flag/数据库/CI 变更；
- no commit/push/branch；
- QA结束时重新检查 tracked/untracked 状态。

---

## 6. 必跑命令

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/wecom-rnd105-qa-pycache \
  .venv/bin/python -m pytest backend/tests/test_wecom_events.py -q

PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/wecom-rnd105-qa-pycache \
  .venv/bin/python -m pytest \
  backend/tests/test_auth.py \
  backend/tests/test_password_auth.py \
  backend/tests/test_http_contract.py \
  backend/tests/test_architecture_boundary.py -q

make verify
git diff --check
git diff --cached --check
git status --short --branch
git ls-files --others --exclude-standard
git log origin/main..HEAD
```

生产只读检查命令必须自行设计为脱敏输出。禁止运行 `systemctl restart/reload`、`nginx -s reload`、真实 worker、数据库写操作或有效 callback POST。

## 7. Verdict 规则

写入 `tasks/RND-105-qa-verdict.json`，schema 使用 `tasks/_templates/qa-verdict.schema.json`。

- **PASS**：AC-1～AC-9 全部通过；生产应用与 Nginx 均部署；post-deploy 真实 GET 200；新 Uvicorn/Nginx 记录安全；无 blocker/major。`recommended_next_state=PASS`，Closure Recommendation=`CLOSE`。
- **FAIL**：任何攻击输入仍返回 500；任何新 access record 仍含 callback query；专项测试缺失/可绕过；路由/协议回归；范围或安全越界。`recommended_next_state=FIXING`，Closure Recommendation=`KEEP OPEN`。
- **BLOCKED**：代码与测试已通过，但生产部署、Nginx变更、只读生产访问或 post-deploy 真实企业微信 GET 证据尚缺。`recommended_next_state=BLOCKED_NEEDS_HUMAN`，Closure Recommendation=`KEEP OPEN`。

历史日志中的旧 query 记录不是本轮 FAIL 条件；新部署边界之后仍出现才是 FAIL。不得清理历史日志来改变判定。

## 8. Verdict 最低内容

- 本地、remote、生产 SHA 与验证时间；
- 历史 blocker 重放结果；
- AC-1～AC-9 独立 evidence；
- focused/full regression 命令和结果；
- HTTP 状态、Nginx effective config 摘要；
- post-deploy fixed accepted/rejected 安全日志摘要；
- 新日志 query-sensitive-field 计数（只允许 `0/non-zero`，不输出值）；
- Worker/media 零触发证据；
- security/scope findings；
- `PASS / FAIL / BLOCKED` 与 `CLOSE / KEEP OPEN`。

## 9. 禁止事项

- 不替开发补代码/测试，不替运维改 Nginx。
- 不修改 Linear，不将 RND-107 状态或实现混入本票。
- 不以企业微信后台“保存成功”单独替代代码、测试和日志安全验证。
- 不以 `make verify` 绿色替代畸形输入和生产 access-log 证据。
- 不输出或复制任何敏感值；不使用真实 Secret 构造测试。
- 不删除历史日志、不轮换密钥、不修改生产状态。
