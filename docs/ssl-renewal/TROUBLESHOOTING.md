# SSL 证书自动续期 — 故障排查

## 诊断命令速查

```bash
# 查看 timer 状态
systemctl list-timers | grep qiniu-ssl-renew

# 查看最近日志
tail -50 /var/log/qiniu-ssl-renew/renew.log

# 手动运行 (实时输出)
sudo systemctl start qiniu-ssl-renew@media.example.com.service
journalctl -u qiniu-ssl-renew@media.example.com.service -f

# 查看本地证书有效期
openssl x509 -in /home/wecomarchive/.acme.sh/media.example.com/fullchain.cer \
    -noout -enddate -subject

# 独立运行 HTTPS 验证 (只读, 不修改任何状态) — 退出码见 ARCHITECTURE.md 第 6.2 节
./verify_https.sh media.example.com --min-days 15; echo "exit=$?"

# 用 dry-run 复现"将要做什么", 不产生副作用
DOMAIN=media.example.com ./renew.sh --dry-run

# 查看该域名的配置 (不要 cat 出 secret; 只看 key 是否存在)
sudo grep -o '^[A-Z_]*=' /etc/qiniu-ssl-renew/media.example.com.env
```

---

## 常见故障

### 1. DNS Challenge 失败

**症状:**
```
[ERROR] acme.sh --renew failed
```
日志中含 `dns_dp` / `TXT record` / `timeout`。

**排查:**
1. DNSPod Token 是否正确:
   ```bash
   grep DP_ ~/.acme.sh/account.conf | sed 's/=.*/=***/'
   ```
2. 域名 NS 是否指向 DNSPod:
   ```bash
   dig NS example.com +short
   # 应返回 *.dnspod.net
   ```
3. DNSPod Token 是否有域名管理权限 (控制台 → 密钥管理 → 查看权限)

### 2. Let's Encrypt Rate Limit

**症状:**
```
[ERROR] acme.sh --renew failed
```
日志含 `429` / `rateLimited` / `too many certificates`。

**排查:**
1. 确认重试频率: 每天最多 1 次 (由 timer 控制)
2. Let's Encrypt 限制: 50 证书/周/注册域名。单域名不可能触发
3. 检查是否有人手动重复执行 `renew.sh`
4. 等待 1 小时后 timer 自动重试

### 3. Qiniu 上传失败

**症状:**
```
[ERROR] Qiniu upload failed — HTTP ... — response: ...
```

**排查:**
1. AK/SK 是否有效, 是否来自正确的每域名配置文件:
   ```bash
   sudo test -f /etc/qiniu-ssl-renew/media.example.com.env && echo "config exists"
   sudo stat -c '%a' /etc/qiniu-ssl-renew/media.example.com.env   # 应为 600
   ```
2. `HTTP 401`/`HTTP 403` → 见下方"4.1 401/403 深度排查"，**不要只假设是 AK/SK 错误**
3. 证书格式: fullchain.cer 必须是 Nginx 格式 (PEM)
4. 网络超时/连接失败 → 检查服务器出网 (`curl -sI http://api.qiniu.com` 应可达；注意官方
   SDK 默认用 `http://`，不是 `https://`)

### 4. Qiniu 绑定失败

**症状:**
```
[ERROR] Qiniu bind failed
```

**排查:**
1. 域名是否在 Qiniu CDN 中、CDN 域名状态是否正常 (Qiniu 控制台查看)
2. `HTTP 401`/`HTTP 403` → 见下方"4.1 401/403 深度排查"
3. 业务错误码 (`qiniu_code` 字段，见 `[ERROR]` 日志行) 对照
   [Qiniu 错误码文档](https://developer.qiniu.com/kodo/3928/error-responses) 排查具体原因，
   而不是重试了事

#### 4.1 401/403 深度排查

**不要只归因于 "AK/SK 错误" 或 "权限不足"。** 本项目的 Qiniu 请求签名完全由官方 `qiniu` Python
SDK (`qiniu.Auth`) 生成（见 [ARCHITECTURE.md 第 6.1 节](ARCHITECTURE.md)），可能导致 401/403 的
原因包括（按诊断顺序排查）：

1. **对照官方 SDK 离线生成 Token，确认预期值。** 在任意能装 `pip install qiniu` 的机器上：
   ```python
   import qiniu
   auth = qiniu.Auth("<真实AK>", "<真实SK>")
   # method 为 GET 时 body/content_type 传 None；POST JSON 时 body 也不参与签名（QBox 规范）
   print(auth.token_of_request("http://api.qiniu.com/domain/media.example.com/httpsconf"))
   ```
   与 `qiniu_helper.py` 实际发出的请求做比对（可临时加 `QINIU_HELPER_DEBUG=1`，但即使开启
   debug 也**不会**打印完整 Authorization header 或 Token 明文，只会输出阶段/状态码等结构化
   信息 — 详见 `tests/test_qiniu_helper.py::test_debug_logging_still_does_not_leak_secrets`）。
2. **比较 canonical request（签名前的原始字符串）**：`<path>[?query]\n[body]`（QBox 方案，本
   项目实际使用的方案）不包含 HTTP method；这曾是旧版本手写签名的 Blocker 之一 — 如果怀疑签名
   逻辑本身，先确认调用的确实是 `qiniu.Auth.token_of_request()` 而不是任何自定义实现。
3. **检查 method / path / query / content-type / body 是否与预期一致：**
   - QBox 签名只在 `Content-Type: application/x-www-form-urlencoded` 时把 body 签进去；本项目
     发送的是 `application/json`，body（包括证书私钥）**不参与签名** —— 如果误用了
     `application/json` 之外还手动拼过签名字符串，是历史 Bug 的复现模式。
   - path 是否包含预期的 query string、是否有多余的尾部斜杠。
4. **检查 API endpoint 是否正确：** 官方 SDK `DomainManager` 默认 host 是 `http://api.qiniu.com`
   （注意是 `http://`），签名不覆盖 scheme/host——如果通过 `QINIU_API_HOST` 覆盖过, 确认没有
   打错端口/协议。
5. **检查 AK/SK 本身与权限：**
   - AK/SK 是否过期或已在 Qiniu 控制台被禁用/轮换。
   - 该 AK/SK 是否有 SSL 证书管理 + CDN 域名管理权限（子账号常见问题）。
6. **检查响应里的 `X-Reqid` 和错误码：** 用 `X-Reqid` 去 Qiniu 工单系统 (https://support.qiniu.com/)
   查询该次请求的服务端日志；响应体 JSON 里的错误信息通常比 HTTP 状态码本身更精确。

不属于以上任何一类，但仍持续 401/403 → 大概率是 Qiniu 侧 API 变更或该账号未开通对应接口权限，
需要联系 Qiniu 支持。

**其他不太可能但仍需排除的原因（本项目结构性规避，但记录以备将来修改代码时参考）：** Authorization
Token 前缀错误（本项目固定使用 `QBox ` 前缀，来自官方 SDK）、URL-safe Base64 编码错误（官方 SDK
内部使用，未自行实现）——这些都是"如果代码曾经/将来手写签名"才会出现的问题类别，当前实现下
不应该出现，如果出现说明有代码在绕过 `qiniu_helper.py`/官方 SDK，需要排查调用路径本身。

### 5. API certID 不匹配

**症状:**
```
[ERROR] certID mismatch — expected=..., actual=...
```

**原因:** 上传成功但绑定失败, 或者绑定后 Qiniu 返回的 certID 不对。

**排查:**
1. 查看 Qiniu 证书列表 (控制台 → SSL 证书 → 我的证书)
2. 检查 `actual` certID 对应的是哪个证书
3. 可能原因: 绑定 API 返回了旧的 certID → 等待下次 timer 重试

### 6. HTTPS 验证未通过 (传播延迟)

**症状:**
```
[WARN] HTTPS verification not yet passing — CDN propagation in progress
[WARN] TLS will be re-checked on next timer run
```

**说明:** 这是预期行为。Qiniu CDN 证书更新有 1-5 分钟传播延迟。`verify_https.sh` 内部有 backoff
重试 (默认 5 次)，如果全部失败会 warn 并在次日复核（不重新上传/绑定）。

**何时需要介入:**
- 连续 `TLS_MISMATCH_DAYS_LIMIT` 天 (默认 7) 不匹配 → 异常, 脚本会 die
- 手动排查, 用 `verify_https.sh` 的退出码定位具体故障类型 (而不是笼统的"不匹配")：
  ```bash
  ./verify_https.sh media.example.com --min-days 15
  echo "exit=$?"
  # 10=DNS解析失败 11=连接失败 12=握手无证书 13=域名不匹配 14=证书链失败
  # 15=已过期 16=即将过期 17=TLS1.2失败 18=HTTP不可达 19=指纹不匹配 20=超时
  # 完整表见 ARCHITECTURE.md 第 6.2 节
  ```

### 7. Webhook 告警未收到

**症状:** 出问题了但没收到告警通知。

**排查:**
1. 确认 `ALERT_WEBHOOK_URL` 是否已配置 (未配置时日志会打 `[DEGRADED]`，不是 bug):
   ```bash
   grep '^ALERT_WEBHOOK_URL=' /etc/qiniu-ssl-renew/media.example.com.env
   ```
2. 日志里找 `[NOTIFY]` 行，会明确标注 `[OK]` / `[WARN] ... non-2xx` / `[WARN] ... timed out` /
   `[WARN] ... connection error`
3. **重要**: Webhook 失败不会改变 `renew.sh` 的退出码——检查 systemd 日志的真实失败原因，
   不要误以为是 Webhook 导致了 renew 失败
4. 手动测试端点:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ALERT_WEBHOOK_URL" \
       -H 'Content-Type: application/json' -d '{"domain":"test","hostname":"test","failed_stage":"test","timestamp":"2026-01-01T00:00:00Z","error_summary":"manual test"}'
   ```

### 8. `sha256sum: command not found`

**症状:** 脚本无法启动。

**处理:**
```bash
# Alibaba Cloud Linux 3 上:
sudo yum install -y coreutils
# sha256sum 是 coreutils 的一部分
```

### 9. `jq: command not found`

```bash
sudo yum install -y jq
```

### 10. Timer 不触发

```bash
# 检查 timer 状态
systemctl status qiniu-ssl-renew@media.example.com.timer

# 查看触发时间
systemctl list-timers qiniu-ssl-renew@media.example.com.timer

# 重新加载
sudo systemctl daemon-reload
sudo systemctl restart qiniu-ssl-renew@media.example.com.timer
```

### 11. 日志目录权限

```bash
# 确保 wecomarchive 可写
sudo chown wecomarchive:wecomarchive /var/log/qiniu-ssl-renew
sudo chmod 755 /var/log/qiniu-ssl-renew
```

### 12. `EnvironmentFile` 找不到 / systemd 启动失败

**症状:**
```
qiniu-ssl-renew@media.example.com.service: Failed to load environment files
```

**排查:**
1. 确认该域名的配置文件存在且路径正确 (由 `%i` 展开):
   ```bash
   ls -la /etc/qiniu-ssl-renew/media.example.com.env
   ```
2. 确认权限 (600, 属主可读):
   ```bash
   sudo chmod 600 /etc/qiniu-ssl-renew/media.example.com.env
   ```
3. 确认文件内 `DOMAIN=` 与 systemd instance 名 (`%i`) 完全一致，否则 `renew.sh` 会在启动早期
   报 "domain argument does not match DOMAIN env var" 并以退出码 2 结束（不会误判为其他故障）。

### 13. `renew.sh --dry-run` 或 `--staging` 报错 "unknown option"

**排查:** 确认使用的是本次修复后的 `renew.sh`（`git log`/`renew.sh` 头部注释应包含 dry-run/staging
用法说明），旧版本没有这两个 flag。`renew.sh --help` 可查看当前支持的用法。
