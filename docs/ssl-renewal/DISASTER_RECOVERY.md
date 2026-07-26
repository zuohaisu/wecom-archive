# SSL 证书自动续期 — 灾难恢复指南

## 灾难场景

### 场景 1: acme.sh 账号损坏 / 证书丢失

**症状:** `renew.sh` 报 `fullchain.cer not found` 或 acme.sh 无法续期。

**恢复:**

```bash
# 1. 重新颁发证书 (无需重新安装 acme.sh)
~/.acme.sh/acme.sh --issue --dns dns_dp -d media.example.com

# 2. 清除旧状态文件
rm -f ~/.acme.sh/media.example.com/.deployed_fp
rm -f ~/.acme.sh/media.example.com/.tls_verified
rm -f ~/.acme.sh/media.example.com/.tls_mismatch_days

# 3. 重新部署
/srv/apps/wecom-archive-365/current/ssl-renew/renew.sh media.example.com
```

### 场景 2: Qiniu CDN 证书被误删 / 域名解除绑定

**症状:** 用户反馈 HTTPS 不可用, API 返回 certID 为空。

**恢复:**

```bash
# 1. 确认本地证书完好
ls ~/.acme.sh/media.example.com/fullchain.cer

# 2. 清除部署状态 (强制重新上传)
rm -f ~/.acme.sh/media.example.com/.deployed_fp
rm -f ~/.acme.sh/media.example.com/.tls_verified

# 3. 重新部署
/srv/apps/wecom-archive-365/current/ssl-renew/renew.sh media.example.com
```

### 场景 3: 证书绑定到错误的 certID (Qiniu 控制台误操作)

**症状:** 线上 TLS 指纹不匹配, 但 renew.sh 的 API 验证通过 (certID 在 Qiniu 上是某个旧证书)。

**恢复:**

所有查询都必须走官方 SDK 签名（不得手动拼 `Authorization: QBox ...` 传给 `curl -H`——那样
Authorization header 会明文出现在 `curl` 的命令行参数里，被同机任意用户通过 `ps` 看到）。

```bash
# 1. 通过 qiniu_helper.py 查看当前绑定的 certId（不经过 curl，不暴露 Authorization）
cd /srv/apps/wecom-archive-365/current/ssl-renew
QINIU_ACCESS_KEY=... QINIU_SECRET_KEY=... \
    python3 qiniu_helper.py verify --domain media.example.com --expected-cert-id "" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["certId"])'

# 2. 查看 Qiniu 证书列表, 找到正确的证书 —— 官方 SDK 未封装这个只读接口，用
#    qiniu.Auth 做签名（零自定义签名逻辑），在 python3 交互式解释器里执行，
#    AK/SK 从环境变量读取，Authorization 全程留在进程内存里，不进 argv/日志:
python3 <<'EOF'
import os, qiniu, requests
auth = qiniu.Auth(os.environ["QINIU_ACCESS_KEY"], os.environ["QINIU_SECRET_KEY"])
url = "http://api.qiniu.com/sslcert"
token = auth.token_of_request(url)
resp = requests.get(url, headers={"Authorization": f"QBox {token}"}, timeout=15)
for cert in resp.json().get("certs", []):
    print(cert["certid"], cert["name"], cert["common_name"])
EOF

# 3. 强制重新上传 + 绑定 (通过 renew.sh，同样走 qiniu_helper.py + 官方 SDK 签名)
rm -f ~/.acme.sh/media.example.com/.deployed_fp
/srv/apps/wecom-archive-365/current/ssl-renew/renew.sh media.example.com
```

### 场景 4: DNSPod Token 泄露 / 轮换

**症状:** DNSPod DNS 挑战持续失败。

**恢复:**

```bash
# 1. 在 DNSPod 控制台创建新 Token
#    https://console.dnspod.cn/account/token/token

# 2. 更新 acme.sh 配置
#    编辑 ~/.acme.sh/account.conf
export DP_Id="新 Token ID"
export DP_Key="新 Token"

# 3. 测试
~/.acme.sh/acme.sh --renew --dns dns_dp -d media.example.com
```

### 场景 5: 服务器完全重建 / 操作系统重装

**恢复顺序:**

```bash
# 1. 重新安装依赖
sudo yum install -y openssl curl jq coreutils
curl https://get.acme.sh | sh -s email=admin@example.com

# 2. 恢复 secrets
#    编辑 ~/.acme.sh/account.conf (DP_Id, DP_Key — 全局共享)
#    对每个域名恢复 /etc/qiniu-ssl-renew/<domain>.env (chmod 600), 内容见
#    ssl-renew/examples/domain.env.example

# 3. 重新颁发证书
~/.acme.sh/acme.sh --issue --dns dns_dp -d media.example.com

# 4. 用 install.sh 重建目录结构 + systemd unit (幂等, 可直接 --apply)
cd /srv/apps/wecom-archive-365/current/ssl-renew
sudo ./install.sh --apply

# 5. dry-run 确认配置无误, 再正式部署
DOMAIN=media.example.com ./renew.sh --dry-run
DOMAIN=media.example.com ./renew.sh

# 6. 启用 timer (对每个需要恢复的域名重复)
sudo systemctl enable --now qiniu-ssl-renew@media.example.com.timer
```

---

## 回滚策略

### 回滚证书 (回到旧的 certID)

如果新证书有问题, 可以回滚到旧证书。**不要用 `curl -H "Authorization: QBox ..."` 手动拼请求** ——
Authorization token 和请求体会明文出现在 `curl` 进程的命令行参数里，同机任意用户 `ps` 可见。
统一走 `qiniu_helper.py`（官方 SDK 签名，AK/SK 只从环境变量读取，从不出现在 argv 中）：

```bash
cd /srv/apps/wecom-archive-365/current/ssl-renew

# 1. 从 Qiniu 证书列表找到旧 certID
#    控制台: https://portal.qiniu.com/certificate/ssl
#    或参考本文档"场景 3"里的只读查询代码块

# 2. 重新绑定旧证书
QINIU_ACCESS_KEY=... QINIU_SECRET_KEY=... \
    python3 qiniu_helper.py bind --domain media.example.com --cert-id <old_certID>
```

### 手动上传证书 (绕过 acme.sh)

如果 acme.sh 本身出问题, 可以手动上传已生成的证书。同样用 `qiniu_helper.py`
——它只接受证书/私钥的**文件路径**，内容在子进程内部读取，不会像手写 `curl -d "$BODY"`
那样把整个证书私钥 JSON 放进命令行参数里：

```bash
cd /srv/apps/wecom-archive-365/current/ssl-renew
CERT_DIR=~/.acme.sh/media.example.com

QINIU_ACCESS_KEY=... QINIU_SECRET_KEY=... \
    python3 qiniu_helper.py upload \
    --domain media.example.com \
    --cert-file "$CERT_DIR/fullchain.cer" \
    --key-file "$CERT_DIR/media.example.com.key"
# 输出 {"ok":true,"certID":"..."} — 记下 certID，用上面"回滚证书"的 bind 命令绑定它
```

---

## 联系方式

- Qiniu 工单: https://support.qiniu.com/
- DNSPod 工单: https://console.dnspod.cn/workorder/
- Let's Encrypt 状态: https://letsencrypt.status.io/
