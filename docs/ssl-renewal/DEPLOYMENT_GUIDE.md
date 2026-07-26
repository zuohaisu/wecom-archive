# SSL 证书自动续期 — 部署指南

> 本指南对应生产部署阶段 (Online deployment)，尚未执行 — 见
> [ARCHITECTURE.md](ARCHITECTURE.md) 状态阶段说明。执行前请先完成 Developer acceptance。

## 前置条件

### 服务器
- 阿里云 ECS (ali-xy-qw), Alibaba Cloud Linux 3
- 用户: `wecomarchive`
- 应用已部署在 `/srv/apps/wecom-archive-365/current/`

### 外部服务
- **DNSPod**: 域名 DNS 托管, 已创建 API Token (ID + Token)
- **Qiniu**: CDN 域名 `media.example.com` 已配置, 已创建 AK/SK
- **Let's Encrypt**: 需注册邮箱 (例如 `admin@example.com`)

---

## 0. 部署前本地验收 (在开发机 / CI 上完成，不需要登录生产服务器)

```bash
cd ssl-renew
make ssl-lint            # shellcheck + shfmt
make ssl-test            # 105 个 bats 测试，全部 mock，不访问真实外部服务
make ssl-dry-run          # dry-run 冒烟测试
make ssl-verify-systemd   # 优先用 Docker 里的 systemd-analyze verify
```

全部通过后再继续下面的生产部署步骤。

## 1. 安装 acme.sh

```bash
# SSH 到服务器, 以 wecomarchive 用户执行
ssh ali-xy-qw
sudo su - wecomarchive

# 安装 acme.sh
curl https://get.acme.sh | sh -s email=admin@example.com

# 设置默认 CA 为 Let's Encrypt
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt

# 验证
~/.acme.sh/acme.sh --version
```

## 1.5 安装官方 Qiniu Python SDK

`qiniu_helper.py` 用官方 SDK 做全部 Qiniu 请求签名（不自行实现 HMAC）：

```bash
python3 -m pip install --user -r /srv/apps/wecom-archive-365/current/ssl-renew/requirements.txt

# 验证
python3 -c "import qiniu; print(qiniu.__version__)"
```

`install.sh`（下一步）会自动检查这一项，缺失时会明确报错并给出上面这条安装命令。

## 2. 运行 install.sh 完成预检 + 目录/权限初始化

```bash
cd /srv/apps/wecom-archive-365/current/ssl-renew

# 先只检查 (不改动任何东西)
./install.sh

# 确认无误后实际执行 (需要 root; 创建 /etc/qiniu-ssl-renew, /var/log/qiniu-ssl-renew,
# 安装 systemd unit 文件, daemon-reload)
sudo ./install.sh --apply
```

`install.sh` 是幂等的：重复执行安全，可在每次部署后重新运行确认状态一致。

## 3. 配置 Secrets

### 3.1 DNSPod API Token (全局共享，所有域名走同一个 DNSPod 账号)

编辑 `~/.acme.sh/account.conf`, 添加:

```bash
export DP_Id="你的 DNSPod Token ID"
export DP_Key="你的 DNSPod Token"
```

### 3.2 每域名独立配置文件

**不再依赖 backend/.env 中的全局 Qiniu 凭证。** 每个域名有自己的配置文件，新增域名不需要
修改任何核心脚本：

```bash
sudo cp examples/domain.env.example /etc/qiniu-ssl-renew/media.example.com.env
sudo vi /etc/qiniu-ssl-renew/media.example.com.env
```

必填内容 (`DOMAIN` 必须与文件名/systemd instance 名一致，脚本会校验):

```bash
DOMAIN=media.example.com
QINIU_ACCESS_KEY=你的AK
QINIU_SECRET_KEY=你的SK
# 可选: ALERT_WEBHOOK_URL=https://your-endpoint/hooks/ssl-renew
```

```bash
sudo chmod 600 /etc/qiniu-ssl-renew/media.example.com.env
sudo chown wecomarchive:wecomarchive /etc/qiniu-ssl-renew/media.example.com.env
```

## 4. 首次证书颁发

```bash
# 以 wecomarchive 用户执行
# 仅颁发证书 (不部署, 先验证 DNS 挑战能否通过)
~/.acme.sh/acme.sh --issue --dns dns_dp -d media.example.com

# 成功后检查
ls ~/.acme.sh/media.example.com/
# 应包含: fullchain.cer, media.example.com.key
```

建议先用 staging CA 验证一遍 DNS-01 链路，不消耗正式 CA 配额:

```bash
cd /srv/apps/wecom-archive-365/current/ssl-renew
DOMAIN=media.example.com bash renew.sh --staging
# 确认 acme.sh --renew ... --staging 成功后，再执行下面的正式 dry-run / 部署
```

## 5. 部署前最后一次 dry-run（针对真实凭证/真实域名，但零外部写请求）

```bash
DOMAIN=media.example.com bash renew.sh --dry-run
```

确认输出的计划符合预期（会读取 `/etc/qiniu-ssl-renew/media.example.com.env`，打印将要执行的
upload/bind/verify 步骤，但不会真正调用）。

## 6. 部署并验证

```bash
cd /srv/apps/wecom-archive-365/current/ssl-renew
DOMAIN=media.example.com bash renew.sh
```

预期输出:
```
[INFO] ====== SSL renewal started (mode=production) ======
[INFO] Running .../acme.sh --renew
[INFO] acme.sh --renew completed
[INFO] local_fp=...
[INFO] local_fp != deployed_fp (...) 
[INFO] proceeding to deploy ...
[INFO] certificate uploaded to Qiniu — certID=...
[INFO] certID=... bound to media.example.com
[INFO] API certID=... confirmed
[INFO] HTTPS verification passed
[OK]  deployment complete
```

## 7. 安装 systemd Timer

`install.sh --apply`（第 2 步）已经把 unit 文件复制到 `/etc/systemd/system/` 并执行了
`daemon-reload`。只需启用对应域名的 timer：

```bash
sudo systemctl enable --now qiniu-ssl-renew@media.example.com.timer

# 验证
sudo systemctl status qiniu-ssl-renew@media.example.com.timer
sudo systemctl list-timers | grep qiniu-ssl-renew
```

## 8. 配置日志轮转

```bash
sudo tee /etc/logrotate.d/qiniu-ssl-renew <<'EOF'
/var/log/qiniu-ssl-renew/renew.log {
    daily
    rotate 90
    compress
    missingok
    notifempty
}
EOF
```

## 9. 验证自动化

```bash
# 手动触发一次, 确认 timer 能正常工作
sudo systemctl start qiniu-ssl-renew@media.example.com.service

# 查看日志
sudo journalctl -u qiniu-ssl-renew@media.example.com.service --no-pager -n 30
tail -30 /var/log/qiniu-ssl-renew/renew.log
```

## 10. 添加新域名

**不修改核心脚本，不复制代码** — 只新增配置文件和 systemd instance:

```bash
# 1. 新增该域名的配置文件
sudo cp examples/domain.env.example /etc/qiniu-ssl-renew/api.example.com.env
sudo vi /etc/qiniu-ssl-renew/api.example.com.env   # DOMAIN=api.example.com, 该域名的 AK/SK...
sudo chmod 600 /etc/qiniu-ssl-renew/api.example.com.env

# 2. 颁发首张证书
~/.acme.sh/acme.sh --issue --dns dns_dp -d api.example.com

# 3. (建议) dry-run 确认配置无误
DOMAIN=api.example.com bash /srv/apps/wecom-archive-365/current/ssl-renew/renew.sh --dry-run

# 4. 启用 timer (状态文件按域名自动隔离，不会与其他域名冲突)
sudo systemctl enable --now qiniu-ssl-renew@api.example.com.timer

# 5. 首次部署
sudo systemctl start qiniu-ssl-renew@api.example.com.service
```

---

## 环境变量速查

| 变量 | 来源 | 说明 |
|------|------|------|
| `DP_Id` / `DP_Key` | `~/.acme.sh/account.conf` | DNSPod API Token (全局共享) |
| `DOMAIN` | `/etc/qiniu-ssl-renew/<domain>.env` | 必须与文件名/systemd instance 一致 |
| `QINIU_ACCESS_KEY` / `QINIU_SECRET_KEY` | `/etc/qiniu-ssl-renew/<domain>.env` | 该域名的七牛凭证 (每域名独立) |
| `ALERT_WEBHOOK_URL` | `/etc/qiniu-ssl-renew/<domain>.env` (可选) | 通用 Webhook 告警端点 |
| `ALERT_WEBHOOK_TIMEOUT` | 同上 (可选, 默认 10) | Webhook 请求超时秒数 |
| `CERT_DIR` / `STATE_DIR` | 同上 (可选) | 覆盖默认的 `~/.acme.sh/<domain>` 路径 |
| `TLS_MIN_DAYS` | 同上 (可选, 默认 15) | `verify_https.sh --min-days` |
| `TLS_MISMATCH_DAYS_LIMIT` | 同上 (可选, 默认 7) | HTTPS 验证连续失败多少天后 die |
| `ACME_SH` | 同上 (可选) | acme.sh 路径, 默认 `~/.acme.sh/acme.sh` |
| `DRY_RUN` | CLI `--dry-run` 或 env | 见 README.md 运行模式表 |
| `ACME_STAGING` | CLI `--staging` 或 env | 见 README.md 运行模式表 |
