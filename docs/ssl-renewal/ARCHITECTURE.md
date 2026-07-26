# SSL 证书自动续期 — 架构设计 (修订版 6)

> 项目: wecom-archive-365
> 域名: media.example.com
> 版本: v6 — RND-189 第三轮开发修复 (Qiniu 签名 + Secret 运行时安全)
> 状态: **Development complete — Developer acceptance pending**

## 状态阶段说明

本项目的验收流程分为五个明确阶段，避免"待审核"与"已实现"状态混淆:

| 阶段 | 含义 | 当前状态 |
|------|------|---------|
| Architecture approved | 架构设计已评审通过 | ✅ 完成 (v4 评审通过后进入实现) |
| Development complete | 代码/测试/文档已按架构实现完毕 | ✅ 完成 (本次 RND-189 第三轮修复) |
| Developer acceptance | 独立 Agent/人工在本地环境验收通过 | ⏳ 待重新验收 |
| Online deployment | 部署到生产服务器 (ali-xy-qw) | ⛔ 未开始 — 依赖 Developer acceptance 通过 |
| Online acceptance | 生产环境实际续期一次成功 + 验证 | ⛔ 未开始 |

本次修复(RND-189 第三轮)范围: 用官方 `qiniu` Python SDK 替换手写的 QBox HMAC 签名（第二轮验收
发现与官方 SDK 签名结果不一致的 Blocker）；新增 `qiniu_helper.py` 消除 Authorization/证书私钥
出现在 `curl` argv 中的泄露风险；修正 401/403 故障排查文档。此前两轮完成的范围（脚本重构、
`verify_https.sh` 拆分、Webhook 告警、dry-run/staging、systemd 加固、多域名配置、安装脚本、
`.gitignore` 加固）均保留且未回归。测试套件从 105 个 bats 测试扩展为 108 个 bats + 29 个 pytest
(137 个测试全部通过)，新增 Linux 验证 Dockerfile 中的 Python 依赖安装。**未做**: 任何生产部署、
真实证书签发、真实七牛写操作。

---

## 0. 域名角色

`media.example.com` 是 Qiniu CDN 加速域名。证书服务于 CDN 边缘节点的 TLS 握手，本文档（SSL 自动续期子系统）与该域名如何被访问无关——域名访问模式的权威说明见
`docs/ops/media_storage_ops.md`。

截至本文档最后更新时，实际访问模式为：Bucket 始终保持私有；历史上（RND-174）该域名仅用于后端 SDK 内部签名请求，客户端从不直连。RND-187（本地开发已完成，独立验收待定，**尚未部署生产**）为已完成登录 + tenant + 媒体归属校验的浏览器新增了短期、单对象 Signed URL 直连能力——RND-187 上线后，该域名会成为已授权浏览器可直接访问的 CDN 域名，但仍然：Bucket 保持私有；浏览器只拿到时效极短（默认 15 分钟，60~3600 秒可配）且仅对单个 object 有效的 Signed URL；AK/SK 从不下发给客户端；未完成认证与 tenant 校验的请求不得签发 URL。这一变化不影响本文档描述的证书续期/绑定流程。

---

## 1. 整体架构

```
DNSPod → acme.sh (dns_dp) → Let's Encrypt → deploy_to_qiniu() → verify_deployment()
                                                                        │
                                                              ┌─────────┴─────────┐
                                                           API OK              API FAIL
                                                              │                   │
                                                     TLS verify with            die
                                                     backoff
                                                         │
                                                  ┌──────┴──────┐
                                                OK            FAIL
                                                 │              │
                                           .deployed_fp    .deployed_fp
                                           .tls_verified   (仅 .deployed_fp)
                                                              │
                                                    次日复核 TLS (重新验)
```

### 运行模式

`renew.sh` 支持三种互斥运行模式，通过 CLI flag 或环境变量选择（README.md 有完整对照表）：

| 模式 | 触发方式 | 行为 |
|------|---------|------|
| production (默认) | 无 flag | 正式 Let's Encrypt CA + 真实七牛部署 |
| staging | `--staging` / `ACME_STAGING=1` | Let's Encrypt staging CA，acme.sh 续期后**跳过全部七牛部署** |
| dry-run | `--dry-run` / `DRY_RUN=1` | 不发起任何外部写请求（DNSPod/ACME/七牛 upload/bind 全部跳过），仅打印计划 |

### 部署边界

用户 `wecomarchive`，应用目录 `/srv/apps/wecom-archive-365/current/ssl-renew/`，systemd 文件在 `deploy/systemd/`。
每个域名是一个独立 systemd instance (`qiniu-ssl-renew@<domain>`)，凭证/配置来自
`/etc/qiniu-ssl-renew/<domain>.env`（`EnvironmentFile=` 按 `%i` 展开），核心脚本不因新增域名而改动。

---

## 2. acme.sh 选择

纯 Shell | 内置 DNSPod (`dns_dp`) | 内置 Qiniu deploy hook | 47K stars。

---

## 3. 三种标识

| 名称 | 计算 | 用途 |
|------|------|------|
| `local_fp` | `sha256(fullchain.cer)` | 判断本地文件是否变了 |
| `certID` | Qiniu `POST /sslcert` 返回 | 精确验证域名绑定的是本次上传的证书 |
| `tls_fingerprint` | `openssl x509 -fingerprint -sha256 -noout` (PEM 直传, 不经 DER) | 验证线上叶子证书 = 本地叶子证书 |

---

## 4. 双文件部署状态

| 文件 | 含义 | 写入时机 |
|------|------|---------|
| `.deployed_fp` | 本地证书已上传+绑定到 Qiniu (API certID 已确认) | `verify_certID_on_domain()` 通过后 |
| `.tls_verified` | HTTPS 验证已通过 (`verify_https.sh`, 见第 6 节) | `verify_https.sh` exit 0 后 |

**决策逻辑**:

```
local_fp == .deployed_fp?
    │
    ├── NO → 部署 + 验证 (完整流程)
    │
    └── YES → .tls_verified 存在?
                 │
                 ├── YES → 全部完成, exit 0
                 │
                 └── NO → TLS 上次未通过
                          → 仅执行 verify_https.sh (不重新上传/绑定)
                          → 通过: 写 .tls_verified
                          → 不通过: warn + escalate (累计失败天数, 阈值 TLS_MISMATCH_DAYS_LIMIT)
```

这样解决了两个问题:
- **不重复上传**: API 已验证 certID 正确时不重新 deploy
- **TLS 最终收敛可被复核**: 即使首次 TLS 不匹配, 次日会重新验证 (不触发新上传)

---

## 5. 代码结构

实现按职责拆分为独立文件，避免单文件膨胀，并支持逐文件单元测试:

```
ssl-renew/
├── renew.sh              主流程编排 (Step 1-5b), 解析 --dry-run/--staging
├── notify.sh              通用 Webhook 告警 (见第 6.3 节)
├── verify_https.sh         独立 HTTPS/TLS 验证 (见第 6.2 节)
├── qiniu_helper.py          Qiniu 签名 + upload/bind/verify — 官方 Python SDK (见第 6.1 节)
├── install.sh              幂等安装/预检脚本
├── requirements.txt          qiniu_helper.py 运行时依赖 (官方 qiniu SDK)
├── requirements-dev.txt      + pytest, 仅测试需要
├── lib/
│   ├── common.sh          日志, secret 过滤, 域名校验, 配置加载, 超时包装
│   └── qiniu.sh            调用 qiniu_helper.py 的薄 shell 封装 (dry-run 感知)
├── examples/
│   └── domain.env.example  每域名 systemd EnvironmentFile 模板
└── tests/                  bats + pytest 测试套件 (137 tests) + mock 基础设施
```

`renew.sh` 主流程 (伪代码，实际实现见 `renew.sh`):

```
解析 --dry-run / --staging → load_config (可选 CONFIG_FILE) → validate_domain
→ 校验 Qiniu 凭证 (dry-run 跳过) → 获取 flock/PID 锁 (幂等保护)

if dry-run:
    打印计划 (读取现有状态文件, 不发起任何网络请求) → exit 0

acme.sh --renew [--staging] → 若 staging: log 跳过部署 → exit 0

local_fp = sha256(fullchain.cer)
if local_fp == deployed_fp:
    if .tls_verified 存在: exit 0 (幂等: 完全跳过, 不调用 qiniu_helper.py)
    else: 仅调用 verify_https.sh (不重新 upload/bind) → 按结果更新 mismatch 计数
else:
    deploy_to_qiniu → bind_cert_to_domain → verify_certID_on_domain (必须通过, 否则 die)
    → 写 .deployed_fp, 清除旧 .tls_verified/.tls_mismatch_days
    → verify_https.sh (失败为 warn, 不 die — 见第 8 节场景 D)
```

---

## 6. 关键子系统

### 6.1 Qiniu 签名与请求 (`qiniu_helper.py` + `lib/qiniu.sh`)

**签名完全委托给官方 `qiniu` Python SDK (`qiniu.Auth` / `qiniu.DomainManager`)，本项目不实现、
也不重新实现任何 HMAC 签名逻辑。**

历史背景 (RND-189 第三轮修复): 早期版本在 bash 中手写了一个 QBox HMAC-SHA1 签名函数
(`qiniu_access_token()`)，与官方算法有两处不一致，会在真实环境下产生 401/403:

1. 签名字符串里错误地包含了 HTTP method（QBox 签名从不签 method，只签
   `path[?query]\n[body]`）。
2. 无论 Content-Type 是什么都把 body 拼进签名，而 QBox 规范只在 `Content-Type:
   application/x-www-form-urlencoded` 时签 body — 本项目实际发送的是 `application/json`，
   这意味着 body **永远不应该参与签名**，但旧实现总是把证书私钥内容也签了进去。

修复方式: 新增 `qiniu_helper.py`，直接调用官方 SDK:

- 上传证书 / 绑定证书 → `qiniu.DomainManager(auth).create_sslcert(...)` /
  `.put_httpsconf(...)` — 这是官方 SDK 对这两个 API 的**直接封装**，签名、请求构造、响应解析
  全部是官方代码，零自定义逻辑。
- 查询绑定状态 (`GET /domain/{d}/httpsconf`，官方 SDK 未封装这个只读接口) → 用官方
  `qiniu.Auth.token_of_request(url)` 生成签名，再用 `requests.get()` 发起请求 — 签名仍然是
  官方函数生成的，只是传输层是我们自己调的 `requests`。

`lib/qiniu.sh` 的 `deploy_to_qiniu()` / `bind_cert_to_domain()` / `verify_certID_on_domain()`
现在只是薄封装：dry-run 时不调用 helper；否则 `python3 qiniu_helper.py <upload|bind|verify> ...`，
解析其 JSON 输出，转换为原有的日志/错误格式（保持向后兼容的消息文案）。

**Golden parity 测试** (`ssl-renew/tests/test_qiniu_helper.py`): 用固定的假 AK/SK 直接调用
`qiniu.Auth.token_of_request()`，对 GET 无 body / POST JSON body / POST form body / 带
query string 的 path / 空 body / 含特殊字符 body 六类输入，断言生成的 token 与预先计算并固化
(pin) 的期望值完全一致 —— 其中 JSON body 的用例专门断言"改变 body 内容不改变 token"，这正是
回归防线：如果未来有人重新引入自定义签名或改错 content-type 判断逻辑，这个测试会失败。

API 汇总:

| 操作 | 方法 | 路径 | 用到的值 | 签名来源 |
|------|------|------|---------|---------|
| 上传证书 | POST | `http://api.qiniu.com/sslcert` | `{name, common_name, ca, pri}` | `DomainManager.create_sslcert` |
| 绑定证书 | PUT | `http://api.qiniu.com/domain/{d}/httpsconf` | `{certId, forceHttps, http2Enable}` | `DomainManager.put_httpsconf` |
| 查询 HTTPS 配置 | GET | `http://api.qiniu.com/domain/{d}/httpsconf` | 返回 `certId` | `Auth.token_of_request` |

（host 用 `http://` 是官方 SDK `DomainManager` 的默认值，签名本身不覆盖 host/scheme。）

#### Runtime Secret Safety — argv/stdout/stderr/log 均不泄露 Secret

- **AK/SK**: 只通过环境变量 (`QINIU_ACCESS_KEY`/`QINIU_SECRET_KEY`，或
  `SAVED_QINIU_AK`/`SAVED_QINIU_SK`) 传给 `qiniu_helper.py` 子进程 —— 环境变量由父进程
  (`lib/qiniu.sh`) 已导出的 shell 变量自然继承，**从不作为命令行参数传递**，不会出现在
  `ps`/`/proc/*/cmdline` 中。
- **证书 / 私钥内容**: `qiniu_helper.py` 的 `--cert-file`/`--key-file` 只接受**文件路径**，内容
  在子进程内部读取；argv 里只有路径字符串，从不包含证书或私钥的实际内容。
- **Authorization Header**: 由官方 SDK 内部构造并直接设置在 HTTP 请求头上，不经过 shell、不
  经过命令行、不写入任何日志。`qiniu_helper.py` 自身的 `redact()` 函数对任何可能打印的文本
  （错误信息、响应体片段）做二次脱敏，即使响应里意外回显了敏感字段也会被替换为 `[REDACTED]`。
- **异常路径**: 网络异常/超时不使用 Python 异常的默认字符串表示，而是构造只包含"阶段 + 异常
  类型名"的安全消息，避免任何底层库把请求对象内容带进异常文本。
- **验证**: `ssl-renew/tests/test_qiniu_helper.py` 在进程内断言 stdout/stderr 不含 Secret；
  `ssl-renew/tests/11_qiniu_helper_argv_safety.bats` 用真实子进程 + `ps` 采样，从操作系统层面
  证明运行中的 `qiniu_helper.py` 进程的 argv 里不含 AK/SK/私钥内容（包括开启
  `PYTHONVERBOSE`/`QINIU_HELPER_DEBUG` 等调试开关时）。

### 6.2 HTTPS 验证 (`verify_https.sh`)

独立脚本，`renew.sh` 通过子进程调用。检查项与退出码（每种故障返回不同非零码，便于自动化与
监控区分故障类型）:

| 退出码 | 含义 |
|--------|------|
| 0 | 全部检查通过 |
| 2 | 用法/参数错误 |
| 10 | DNS 解析失败 |
| 11 | TCP/TLS 连接失败 (拒绝/不可达) |
| 12 | TLS 握手未返回证书 (SNI/握手失败) |
| 13 | 证书域名 (CN/SAN) 不匹配 |
| 14 | 证书链验证失败 |
| 15 | 证书已过期 |
| 16 | 证书剩余有效期低于 `--min-days` 阈值 |
| 17 | TLS 1.2 握手失败 |
| 18 | HTTP 响应不可取得 (401/403 视为成功，证明 TLS+路由正常) |
| 19 | 证书指纹不匹配 (可选检查) |
| 20 | 重试耗尽后超时 |

支持 `--host`（连接到指定地址但保持 SNI/域名不变，用于测试指向本地 mock server）、
`--retries`/`--retry-delay`/`--timeout`、`--cert-file`/`--fingerprint`（可选指纹比对）。

### 6.3 通用 Webhook 告警 (`notify.sh`)

不写死企业微信/飞书/邮件格式；通过 `ALERT_WEBHOOK_URL` 配置任意接受 JSON POST 的端点。
Payload: `{domain, hostname, failed_stage, timestamp, error_summary}`。

- 未配置 `ALERT_WEBHOOK_URL` → 明确标记为 `[DEGRADED]` 降级为日志，不是静默 no-op。
- Webhook 请求失败(超时/连接失败/非 2xx) 只记录 WARN，**永远不会改变调用方 (`renew.sh`)
  的原始退出码** — `warn()`/`die()` 都用 `|| true` 调用 `notify.sh`，且 `notify.sh` 自身也总是 `exit 0`。
- 超时通过 `ALERT_WEBHOOK_TIMEOUT`（默认 10s）控制，同时作为 curl 的 connect-timeout 和 max-time。
- Secret 脱敏：消息体在写入日志/payload 前都经过 `filter_secrets`。

### 6.4 Secrets 与脱敏 (`lib/common.sh::filter_secrets`)

```
来源 1: ~/.acme.sh/account.conf (600)         DP_Id, DP_Key, SAVED_QINIU_AK/_SK
来源 2: /etc/qiniu-ssl-renew/<domain>.env (600) QINIU_ACCESS_KEY/_SECRET_KEY, ALERT_WEBHOOK_URL

fallback 链: SAVED_QINIU_AK → QINIU_ACCESS_KEY (同理 SK)
```

`filter_secrets` 覆盖: `KEY=value` 形式、JSON `"KEY":"value"` 形式、QBox Authorization header、
URL 中内嵌的 `user:pass@host` 凭证。私钥不进 repo（`.gitignore` 显式排除 `*.key`/`*.pem` 等，见
`.gitignore` 中 ssl-renew 相关段落）。

---

## 7. 场景全集

### A: 一切正常
```
acme.sh skip → local_fp == deployed_fp → .tls_verified 存在 → exit 0 (不调用 curl)
```

### B: 新证书自动续期 + 部署 + HTTPS 验证通过
```
acme.sh → 新证书 → deploy → bind → API verify → write .deployed_fp
→ verify_https.sh OK → write .tls_verified → exit 0
```

### C: 上次部署失败
```
acme.sh skip → local_fp != deployed_fp → deploy → ... → exit 0
```

### D: HTTPS 传播延迟 (当天未通过)
```
deploy + API OK → write .deployed_fp
→ verify_https.sh 失败 (非 chain/expired 等硬故障, 如 DNS/连接类) → warn → 无 .tls_verified
→ exit 0 (非 fatal — API 已确认 certID 正确, 只是 CDN 传播/路由未就绪)

次日:
  local_fp == .deployed_fp → .tls_verified 不存在 → 仅 re-check HTTPS (不重新上传)
  → OK → touch .tls_verified → exit 0
```

### E: HTTPS 验证持续 N 天不通过 (`TLS_MISMATCH_DAYS_LIMIT`, 默认 7)
```
deploy + API OK (day 1) → verify_https.sh fail → .tls_mismatch_days=1
day 2..N-1: re-check each day → still fail → 累加
day N: mismatch_days >= 限制 → die "manual investigation required"
```

### F: staging 模式
```
--staging/ACME_STAGING=1 → acme.sh --renew --staging → 跳过全部 Qiniu 部署 → exit 0
(不消耗正式 CA 配额, 不生成生产可用证书, 不触发七牛生产部署)
```

### G: dry-run 模式
```
--dry-run/DRY_RUN=1 → 读取现有状态文件(只读) → 打印完整计划 → exit 0
(不调用 DNSPod/ACME/Qiniu upload/bind/verify, 不写任何状态文件, 输出不含 secret)
```

---

## 8. 故障矩阵

| 故障 | 影响 | 处理 |
|------|------|------|
| DNS 挑战失败 | 本地证书未刷新 | acme.sh 下次 timer 重试 |
| LE 限流 | 同上 | 下次 timer |
| Qiniu 上传失败 (含超时/401/403/业务错误码) | 未上传 | die → notify → local_fp≠deployed_fp → 下次重试 deploy |
| Qiniu 绑定失败 | CDN 仍是旧 certID | die → notify → 下次重试 |
| API certID 不匹配 | 绑定确实失败 | die → notify → 下次重试 (重新 deploy) |
| HTTPS 验证失败 (非 day-N) | API OK, CDN/路由未就绪 | warn → notify → 次日复核 (不重新 deploy) |
| HTTPS 验证连续 N 天失败 | 需人工介入 | die → notify → 人工排查 |
| Webhook 不可达/超时/非 2xx | 告警未送达 | 仅 WARN 记录，不影响 renew.sh 退出码 |

---

## 9. 部署拓扑

```
repo:
├── ssl-renew/{renew.sh,notify.sh,verify_https.sh,install.sh,lib/,examples/,tests/}
├── deploy/systemd/qiniu-ssl-renew@{.service,.timer}
└── docs/ssl-renewal/{ARCHITECTURE,DEPLOYMENT_GUIDE,TROUBLESHOOTING,DISASTER_RECOVERY}.md

operator (每域名一份, 互不冲突):
├── /etc/qiniu-ssl-renew/<domain>.env                 (600, 该域名的全部凭证/配置)
├── /home/wecomarchive/.acme.sh/account.conf           (600, DNSPod token, 全局共享)
├── /home/wecomarchive/.acme.sh/<domain>/{fullchain.cer,.key,.deployed_fp,.tls_verified,.renew.lock}
└── /var/log/qiniu-ssl-renew/renew.log
```

---

## 10. 验收标准

- [x] `local_fp == deployed_fp && .tls_verified` → 完全跳过, 不调用 qiniu_helper.py
- [x] `local_fp != deployed_fp` → 完整 deploy + verify
- [x] `local_fp == deployed_fp && ! .tls_verified` → 仅验 HTTPS (不重新上传)
- [x] API certID 精确比对
- [x] `verify_https.sh` 提供细分退出码, 覆盖 DNS/连接/域名/链/过期/TLS1.2/HTTP/指纹/超时
- [x] HTTPS 验证失败 → warn → 次日复核 → N 天 die
- [x] `set -euo pipefail` 不被可预期失败误触发 (`|| true` / 显式 if 捕获)
- [x] Secrets 不泄露 (JSON/KV/Header/URL 内嵌凭证均脱敏)
- [x] dry-run 不产生任何外部写请求, 可预测退出码
- [x] staging 模式跳过生产部署
- [x] Webhook 告警失败不影响主流程退出码
- [x] 多域名状态互不冲突 (per-instance EnvironmentFile + 按域名命名的状态目录)
- [x] Qiniu 请求签名由官方 SDK 生成，golden parity 测试对照固化的期望 token
- [x] Authorization / 证书私钥内容不出现在任何子进程的 argv 中（`ps` 级验证）
- [x] stdout / stderr / renew.sh 日志在成功、失败、异常、debug 模式下均不含 Secret
- [x] 137 个测试全部通过 (108 bats + 29 pytest，见 `ssl-renew/tests/`, `make ssl-test`)

以上均已在开发环境 (macOS + Linux Docker) 通过自动化测试验证；生产环境实际验证见
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) 与 Online acceptance 阶段。
