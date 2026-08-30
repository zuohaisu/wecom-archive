# SSL 证书自动续期 — 项目 README

> 为 Qiniu CDN 域名自动续期 Let's Encrypt 证书并部署。

## 架构

详见 [docs/ssl-renewal/ARCHITECTURE.md](../docs/ssl-renewal/ARCHITECTURE.md)。

## 文件

| 文件 | 用途 |
|------|------|
| `renew.sh` | 主脚本: 续期 + 部署 + 验证编排 (单域名, per-domain Qiniu CDN 证书绑定) |
| `renew-wildcard.sh` | 通配符 `*.crowntime.cn` 续期脚本 (GH-104 Follow-up B, 从生产已验证实现原样纳入): acme.sh DNS-01 → Qiniu CDN/origin 双绑定 → nginx 证书部署+reload。与 `renew.sh` 语义不同，详见 [docs/operations/wildcard-ssl-renewal.md](../docs/operations/wildcard-ssl-renewal.md) |
| `notify.sh` | 通用 Webhook 告警 (支持任意接受 JSON POST 的端点) |
| `verify_https.sh` | 独立 HTTPS/TLS 验证 (可单独运行) |
| `install.sh` | 幂等安装 / 预检脚本 |
| `lib/common.sh` | 日志、secret 脱敏、域名校验、配置加载、超时包装 (`renew.sh` 与 `renew-wildcard.sh` 共用) |
| `lib/qiniu.sh` | 调用 `qiniu_helper.py` 的薄 shell 封装 (dry-run 感知，`renew.sh` 与 `renew-wildcard.sh` 共用) |
| `qiniu_helper.py` | Qiniu upload/bind/verify — 官方 `qiniu` Python SDK 签名，见下方"Qiniu 签名与 Secret 运行时安全" |
| `requirements.txt` / `requirements-dev.txt` | `qiniu_helper.py` 的 Python 依赖 |
| `examples/domain.env.example` | 每域名 systemd `EnvironmentFile` 配置模板 (per-domain 模板用) |
| `examples/wildcard-domain.env.example` | 通配符 `EnvironmentFile` 配置模板 (`renew-wildcard.sh` 用，仅列变量名，不含真实值) |
| `tests/` | bats + pytest 测试套件 (155 个测试) + mock 基础设施 |
| `Dockerfile` | Linux + systemd 工具验证环境 (macOS 无 systemd 时使用) |
| `../deploy/systemd/qiniu-ssl-renew@.service` | systemd service 模板 (per-domain instance) |
| `../deploy/systemd/qiniu-ssl-renew@.timer` | systemd timer 模板 |
| `../deploy/systemd/qiniu-ssl-renew-wildcard.service` | 通配符续期 systemd service (GH-104 Follow-up B，从生产实现原样纳入) |
| `../deploy/systemd/qiniu-ssl-renew-wildcard.timer` | 通配符续期 systemd timer — 每日 00:15 + 最多 15 分钟随机延迟 |

## 运行模式 — dry-run / staging / production

**三种模式互斥，`README` 与代码行为严格一致，不会把真实执行命令标记为 dry-run:**

| 模式 | 触发 | ACME CA | Qiniu 部署 | 外部写请求 | 用途 |
|------|------|---------|-----------|-----------|------|
| **dry-run** | `--dry-run` 或 `DRY_RUN=1` | 不调用 | 不调用 | **零** — 不发起任何 DNSPod/ACME/Qiniu 写请求 | 本地验证配置与计划，CI 冒烟测试 |
| **staging** | `--staging` 或 `ACME_STAGING=1` | Let's Encrypt **staging** | **跳过** (staging 证书不可信，不部署) | 仅 acme.sh DNS-01 挑战 | 验证 DNS/acme.sh 链路，不消耗正式 CA 配额 |
| **production** (默认) | 无 flag | Let's Encrypt 正式 | 正式上传+绑定 | 全部 | 生产续期 |

```bash
# dry-run: 打印计划，不产生任何副作用，退出码可预测 (0=计划成功, 2=用法/配置错误)
DRY_RUN=1 ./renew.sh media.example.com
# 或
./renew.sh media.example.com --dry-run

# staging: 验证 acme.sh + DNSPod 链路，不部署到七牛
./renew.sh media.example.com --staging

# production: 真实续期 + 部署 (需要真实凭证)
export QINIU_ACCESS_KEY=... QINIU_SECRET_KEY=...
./renew.sh media.example.com
```

dry-run 保证:
- 不调用真实 DNSPod API、不触发正式/staging ACME 签发、不调用七牛 upload/bind 接口
- 不修改任何本地状态文件 (`.deployed_fp` / `.tls_verified` / `.tls_mismatch_days`)
- 输出清楚说明"将要执行什么"（`[DRY-RUN]` 前缀），且不包含任何 secret
- 有专门的自动化测试 (`tests/02_dry_run.bats`)

## 依赖

- `acme.sh` — 已安装在 `$HOME/.acme.sh/`
- `openssl`, `curl`, `jq`, `sha256sum`, `bash`
- `python3` + 官方 `qiniu` SDK（`pip install -r ssl-renew/requirements.txt`）— 用于全部 Qiniu 签名
  请求，见下方"Qiniu 签名与 Secret 运行时安全"
- DNSPod API Token (`DP_Id` / `DP_Key` 在 `~/.acme.sh/account.conf`)
- Qiniu AK/SK — 每域名独立配置在 `/etc/qiniu-ssl-renew/<domain>.env`（见下方"多域名配置"）

开发/测试额外依赖 (通过 `make ssl-lint` / `make ssl-test` 使用): `shellcheck`, `shfmt`, `bats-core`,
`pip install -r ssl-renew/requirements-dev.txt`（含 `pytest`）。
macOS: `brew install shellcheck shfmt bats-core coreutils`。

## Qiniu 签名与 Secret 运行时安全

**签名完全委托给官方 `qiniu` Python SDK，不自行实现任何 HMAC 逻辑。** 早期版本手写的 bash QBox
签名与官方算法不一致（错误地把 HTTP method 和 JSON body 都签了进去，而 QBox 规范只在
`application/x-www-form-urlencoded` 时签 body），已被完全移除。现在：

- 上传/绑定证书直接调用官方 `qiniu.DomainManager.create_sslcert()` / `.put_httpsconf()`。
- 查询绑定状态（官方 SDK 未封装该只读接口）用官方 `qiniu.Auth.token_of_request()` 签名 +
  `requests.get()` 发起。
- Golden parity 测试（`tests/test_qiniu_helper.py`）用固定假凭证对照预先算好并固化的官方 SDK
  token，覆盖 GET 无 body / POST JSON / POST form / query string / 空 body / 特殊字符 body 六种
  输入 —— 详见 [ARCHITECTURE.md 第 6.1 节](../docs/ssl-renewal/ARCHITECTURE.md)。

**Secret 从不出现在命令行参数、stdout、stderr 或日志中：**

- AK/SK 只通过环境变量传给 `qiniu_helper.py` 子进程（父进程已导出的 shell 变量自然继承），
  从不作为 `--flag` 传递，不会出现在 `ps` / `/proc/*/cmdline` 中。
- 证书/私钥只通过 `--cert-file`/`--key-file` 传**文件路径**，内容在子进程内部读取，argv 里从不
  包含证书或私钥的实际内容。
- Authorization header 由官方 SDK 内部构造，直接设在 HTTP 请求上，不经过 shell、不写日志。
- 验证：`tests/test_qiniu_helper.py`（进程内 stdout/stderr 断言）+
  `tests/11_qiniu_helper_argv_safety.bats`（真实子进程 + `ps` 采样的操作系统级验证，覆盖成功/
  失败/异常/debug 模式）。

## 通用 Webhook 告警

`notify.sh` 不写死企业微信/飞书/邮件格式，而是一个通用 Webhook 适配层：

```bash
export ALERT_WEBHOOK_URL="https://your-endpoint.example.com/hooks/ssl-renew"
export ALERT_WEBHOOK_TIMEOUT=10   # 秒, 默认 10
```

POST 的 JSON payload:
```json
{
  "domain": "media.example.com",
  "hostname": "ali-xy-qw",
  "failed_stage": "qiniu_upload",
  "timestamp": "2026-07-10T12:00:00Z",
  "error_summary": "Qiniu upload failed — HTTP 401 ..."
}
```

- 未配置 `ALERT_WEBHOOK_URL` → 明确降级为日志（`[DEGRADED]` 标记），不是静默 no-op。
- Webhook 超时/连接失败/非 2xx → 仅记录 WARN，**不会覆盖 `renew.sh` 的原始退出码**。
- 消息体在写入日志/payload 前都会经过 secret 脱敏。

## 多域名配置

新增域名**不需要修改核心脚本**，只需新增一份配置文件 + 启用一个 systemd instance：

```bash
# 1. 复制模板
cp examples/domain.env.example /etc/qiniu-ssl-renew/api.example.com.env
# 2. 编辑 DOMAIN / QINIU_ACCESS_KEY / QINIU_SECRET_KEY / ALERT_WEBHOOK_URL 等
vi /etc/qiniu-ssl-renew/api.example.com.env
chmod 600 /etc/qiniu-ssl-renew/api.example.com.env
# 3. 首次签发 + 启用 timer (见 DEPLOYMENT_GUIDE.md)
systemctl enable --now qiniu-ssl-renew@api.example.com.timer
```

每个域名的状态文件 (`.deployed_fp` / `.tls_verified` / `.renew.lock`) 都存放在该域名自己的
`$HOME/.acme.sh/<domain>/` 目录下（或通过 `CERT_DIR`/`STATE_DIR` 显式覆盖），互不冲突。

**通配符 (`*.crowntime.cn`) 是独立的固定 flow，不属于这套"新增域名"机制**：只有一个
`renew-wildcard.sh` + `qiniu-ssl-renew-wildcard.{service,timer}` 实例，配置文件
`/etc/qiniu-ssl-renew/media.crowntime.cn.env`（见 `examples/wildcard-domain.env.example`），
不通过 `qiniu-ssl-renew@<domain>` 模板实例化。详见
[docs/operations/wildcard-ssl-renewal.md](../docs/operations/wildcard-ssl-renewal.md)。

## 本地测试 (不需要真实 secrets)

```bash
# 1. 完整自动化测试套件 (126 个 bats + 29 个 pytest = 155 个测试，全部 mock/本地服务，
#    不访问任何真实外部服务)
make ssl-test

# 2. Lint (shellcheck + shfmt + python 语法检查)
make ssl-lint

# 3. dry-run 冒烟测试 (使用文档保留域名 example.com 和临时 HOME)
make ssl-dry-run

# 4. systemd 单元文件验证 (优先用原生 systemd-analyze 或 Docker；
#    两者都不可用时降级为结构化静态检查，并明确标注这不是权威验证)
make ssl-verify-systemd
```

单独运行某个测试文件：`bats tests/06_verify_https.bats`，或
`python3 -m pytest tests/test_qiniu_helper.py -v`。

### Linux / systemd 验证环境 (macOS 无 systemd)

```bash
docker build -t ssl-renew-verify -f Dockerfile ..
docker run --rm -v "$(pwd)/..":/workspace -w /workspace ssl-renew-verify \
    make ssl-lint ssl-test ssl-verify-systemd
```

容器内可运行 `bash --version` / `shellcheck` / `shfmt` / `systemd-analyze verify` / `bats` — 开发者
无需登录生产服务器即可完成全部验收检查。

### 单独验证 verify_https.sh（人工排查用）

```bash
# 对任意已上线域名做一次只读检查 (不修改任何状态)
./verify_https.sh media.example.com --min-days 15
echo "exit=$?"   # 见 ARCHITECTURE.md 第 6.2 节退出码表
```

## 部署

首次部署指南: [docs/ssl-renewal/DEPLOYMENT_GUIDE.md](../docs/ssl-renewal/DEPLOYMENT_GUIDE.md)

故障排查: [docs/ssl-renewal/TROUBLESHOOTING.md](../docs/ssl-renewal/TROUBLESHOOTING.md)

灾难恢复: [docs/ssl-renewal/DISASTER_RECOVERY.md](../docs/ssl-renewal/DISASTER_RECOVERY.md)

## 安装脚本 (install.sh)

```bash
./install.sh              # 检查依赖 + 打印安装计划，不做任何改动
./install.sh --apply       # 实际安装 (仅限 Linux + root)
```

在 macOS 上运行 `install.sh --apply` 会明确拒绝并提示改用 Docker 验证环境或在真实 Linux 主机上执行
——不会假装完成了 systemd 安装。幂等：重复执行安全（`mkdir -p` / `install -m` 均可重复）。
