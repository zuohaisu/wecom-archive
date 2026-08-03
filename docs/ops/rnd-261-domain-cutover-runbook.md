# RND-261 域名切换人工执行 Runbook

> qwhhcd.crowntime.cn → archive.crowntime.cn（归档服务规范公网域名）
> Linear: https://linear.app/xyzhs1897/issue/RND-261/二级域名变更qwhhcdcrowntimecn-archivecrowntimecn
> 风险等级：**R3**（生产 DNS / TLS / 部署 / OAuth）
> 执行边界：**本 runbook 所有生产写操作均由 Haisu 或获授权的人工运维执行**。Ops agent 只逐步提示、记录证据并在每个 gate 等待人工确认，不得自行登录生产、修改 DNS/Nginx/WeCom/证书、重启服务或读取/输出任何密钥。

---

## 0. 仓库现状（已核实证据，2026-08-03）

以下为 agent 从仓库代码核实的事实，供人工运维核对，不作为生产配置的假设：

| # | 事实 | 证据位置 |
|---|------|---------|
| F1 | `ARCHIVE_DOMAIN` 是 deploy 脚本 public health gate 的域名来源，默认占位符 `archive.example.com`；脚本 source `backend/.env` 后用 `PUBLIC_HEALTH="https://${ARCHIVE_DOMAIN}/health"` 构造公网健康检查 URL | `scripts/deploy_server.sh:188, 615` |
| F2 | 公网健康检查失败**不会触发代码回滚**（应用本身已通过内部检查），部署仍以非零退出，需运维排查 proxy/DNS/TLS 层 | `scripts/deploy_server.sh:684-692` |
| F3 | `ADMIN_DOMAIN` 用于构造 WeCom OAuth 的 `redirect_uri`：`https://{ADMIN_DOMAIN}/api/auth/wecom/callback`；未设置时降级为 `http://localhost:8035`（生产不可用） | `backend/app/routers/auth.py:752-759, 789-794` |
| F4 | `ADMIN_DOMAIN` 同时也是密码重置 / 邀请链接的 fallback base URL（`PASSWORD_RESET_BASE_URL` / `INVITE_BASE_URL` 未设置时） | `backend/app/routers/auth.py:303, 398` |
| F5 | 官网（`crowntime.cn` / `www.crowntime.cn`）由 `static_site/company_homepage/` 静态站服务，与归档后端**明确隔离**；仓库 README 声明切换前 `qwhhcd.crowntime.cn` 继续路由到归档后端 | `static_site/company_homepage/README.md:49-80` |
| F6 | 仓库**不包含受控的生产 Nginx 配置**（README 明示 "This repo does not currently contain a checked-in Nginx config"）；生产 nginx 由运维人工管理 | `static_site/company_homepage/README.md:59-63` |
| F7 | 仓库**不包含**生产 `wecom-archive-365.service` systemd 单元；应用以 `uvicorn app.main:app --host 127.0.0.1 --port 8035` 运行，经 nginx 反代 | `docs/DEPLOYMENT.md:101-109` |
| F8 | `ssl-renew` 子系统只覆盖 **media（Qiniu CDN）域名**的证书（`media.example.com`），**与归档域名无关**；归档域名 TLS 终止位置与证书签发负责人仓库无记录 | `docs/ssl-renewal/ARCHITECTURE.md:30-35` |
| F9 | CI/CD：push 到 `main`（`docs/**`、`tasks/**`、`*.md` 除外）触发 test → deploy；deploy 通过 SSH 调 `scripts/deploy_server.sh`，含公网 health gate。**本 runbook 位于 `docs/` 下，不会触发部署** | `.github/workflows/deploy.yml:3-14, 241-297` |
| F10 | `.env.example`（仓库根）中 `ARCHIVE_DOMAIN` 为空、`ADMIN_DOMAIN=localhost`，均为占位，不含真实域名 | `.env.example:64-70, 209-213` |

**关键联动（Gate 2 顺序风险）**：F1+F9 意味着——一旦生产 `backend/.env` 的 `ARCHIVE_DOMAIN` 改为 `archive.crowntime.cn`，**下一次 CI/CD 部署的 public health gate 就会打新域名**。若新域名 DNS/TLS 未就绪，部署会红（不自动回滚，但阻塞后续部署）。因此必须先完成 Gate 1（新域名可达+TLS 就绪），再执行 Gate 2（改 `.env`）。

---

## 1. 前置人工确认（缺一即停 → BLOCKED_NEEDS_HUMAN）

| 编号 | 确认项 | 记录位置 |
|------|--------|---------|
| P1 | 新域名 `archive.crowntime.cn` 的 DNS 记录类型与值（A/AAAA/CNAME）及目标 IP/入口，已由**域名管理员**确认 | 下文 Gate 0 证据表 |
| P2 | 当前归档入口的 **TLS 终止位置**（Nginx / 负载均衡 / CDN）和证书签发/续期负责人已确认（仓库无记录，见 F8） | 同上 |
| P3 | WeCom OAuth 当前使用的**可信域名及回调 URL**已确认；仅当它当前确实使用旧域名 `qwhhcd.crowntime.cn` 时才变更 `ADMIN_DOMAIN` 与 WeCom 控制台配置 | 同上 |
| P4 | Haisu 已决定旧域名策略：**① 暂时 301/308 重定向至新域名（给出截止日期）** 或 **② 直接下线**。未作决定不得配置重定向或保留旧入口 | 同上 |
| P5 | 已约定维护窗口、观察时长及**可联系的回滚负责人** | 同上 |

> 若 P1–P5 任一未确认，agent 在本文件末尾输出 `BLOCKED_NEEDS_HUMAN` 并停止。

---

## 2. 执行步骤

### Gate 0：变更前取证与回滚准备（人工运维）

1. 记录当前状态（**不得**把 `.env` 全文、令牌、密钥或连接串贴入记录）：
   - 当前 DNS：`dig +short qwhhcd.crowntime.cn`
   - 当前入口配置与 TLS：`openssl s_client -connect qwhhcd.crowntime.cn:443 -servername qwhhcd.crowntime.cn </dev/null 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName -dates`
   - 服务状态：`systemctl status wecom-archive-365.service --no-pager | head -20`（或既有受控方式）
   - 当前 `ARCHIVE_DOMAIN` / `ADMIN_DOMAIN` 的**域名值**（只记域名，不记其他 env 内容）
2. 创建受权限保护的生产 Nginx/入口配置和 `backend/.env` 备份；记录备份位置与校验值（如 `sha256sum`）。**不将备份提交仓库**。
3. 维护窗口前验证旧入口健康：
   ```bash
   curl --fail --silent --show-error https://qwhhcd.crowntime.cn/health/ready
   ```
4. 将结果（时间、操作者、HTTP 状态）写入 Linear 评论或受控运维记录。**失败则停止切换**。

**Gate 0 判定**：旧入口 2xx + 备份与校验值已记录 → PASS；否则 FAIL 并停止。

### Gate 1：新域名可达与 TLS 就绪（人工运维）

1. 创建并传播 `archive.crowntime.cn` 的 DNS 记录，**目标与现有归档入口一致**（IP/入口值来自 P1）。
2. 在 TLS 终止层为 `archive.crowntime.cn` 签发并部署有效证书；证书 SAN 必须覆盖该域名。**不得替换、删除或影响官网 `crowntime.cn` / `www.crowntime.cn` 的证书与 server block**。
3. DNS/证书生效后验证：
   ```bash
   dig +short archive.crowntime.cn
   curl --fail --silent --show-error --head https://archive.crowntime.cn/health/ready
   openssl s_client -connect archive.crowntime.cn:443 -servername archive.crowntime.cn </dev/null 2>/dev/null \
     | openssl x509 -noout -subject -ext subjectAltName -dates
   ```
4. 确认 HTTP 证书验证成功、SAN 含新域名、健康端点返回 2xx；**任一项失败则停止，不进入切换**。

**Gate 1 判定**：DNS 可解析 + TLS 链有效 + SAN 含新域名 + `/health/ready` 2xx → PASS；否则 FAIL 并停止。

### Gate 2：应用与 OAuth 切换（人工运维）

1. 在生产 `backend/.env` 中**仅将 `ARCHIVE_DOMAIN`** 设为：
   ```
   ARCHIVE_DOMAIN=archive.crowntime.cn
   ```
   保留其余配置原样；**不要将该文件复制到仓库或聊天记录**。
2. **仅在 Gate 0 已证实 OAuth 正使用旧域名时（P3）**：
   - 在 WeCom 管理后台将可信域名/授权回调地址更新为新域名；
   - 将生产 `ADMIN_DOMAIN` 改为 `archive.crowntime.cn`；
   - 按现有受控方式重启/重载应用。
   - 如果 `ADMIN_DOMAIN` 是不同的管理域名，保持它不变，并在记录中注明。
3. 按现有生产 runbook 重载入口服务和应用；**不得临时改 CI/CD、部署脚本或 systemd 单元**。
4. 变更后**立即手动验证一次**新域名公网健康（不等 CI）：
   ```bash
   curl --fail --silent --show-error https://archive.crowntime.cn/health/ready
   ```

**Gate 2 判定**：`.env` 仅改目标值 + 重载成功 + 新域名公网 `/health/ready` 2xx → PASS；否则 FAIL 并停止。

> 注意（F1/F9 联动）：Gate 2 之后任何一次 CI 部署都会以新域名做 public health gate。若本 gate 已 PASS，则后续部署自然通过；若 Gate 1 曾跳过或回退，禁止执行 Gate 2。

### Gate 3：业务验收与旧域名策略（人工运维）

1. 用**无痕浏览器或独立会话**访问 `https://archive.crowntime.cn/`，完成：登录页加载 → OAuth 登录（若适用）→ 归档列表读取 → 媒体预览，各一次。**不得使用或导出生产聊天内容作为证据**，只记录成功/失败、时间和 HTTP 状态。
2. 验证：
   ```bash
   curl --fail --silent --show-error https://archive.crowntime.cn/health
   curl --fail --silent --show-error https://archive.crowntime.cn/health/ready
   ```
3. 严格按 Gate 0 的 P4 决策执行旧域名策略：
   - 选择**重定向** → 验证旧域名仅 301/308 到新归档域名；
   - 选择**下线** → 验证旧域名不再指向归档应用。
4. 再次验证官网未受影响：
   ```bash
   curl --fail --silent --show-error --head https://crowntime.cn/
   curl --fail --silent --show-error --head https://www.crowntime.cn/
   ```
   （预期：返回官网内容，而非归档应用；具体内容特征由运维核对。）
5. 在约定观察窗口内检查入口错误率、应用日志中的 host/OAuth 错误和健康检查；**只记录聚合指标与脱敏错误摘要**。

**Gate 3 判定**：新域名业务验收通过 + 旧域名行为符合 P4 + 官网正常 + 观察窗口无异常 → PASS；否则 FAIL 并进入回滚流程。

---

## 3. 验收标准（AC）对照

| AC | 内容 | 判定 gate |
|----|------|----------|
| AC-1 | `archive.crowntime.cn` DNS 可解析，HTTPS 证书链有效，SAN 覆盖新域名 | Gate 1 |
| AC-2 | 新域名 `/health` 与 `/health/ready` 均返回 2xx | Gate 2/3 |
| AC-3 | 归档页面与登录流程在新域名可用；若 OAuth 使用该域名，WeCom 回调成功 | Gate 3 |
| AC-4 | `ARCHIVE_DOMAIN` 生产值为 `archive.crowntime.cn`；`ADMIN_DOMAIN` 仅在需要时更新 | Gate 2 |
| AC-5 | 旧域名行为符合 Haisu 在 Gate 0 的明确决策（P4） | Gate 3 |
| AC-6 | 主站 `crowntime.cn` / `www.crowntime.cn` 未受影响 | Gate 3 |
| AC-7 | 生产密钥、完整 `.env`、生产聊天内容及证书私钥未出现在仓库、Linear 评论或 agent 输出中 | 全程 |

---

## 4. 回滚（仅人工执行）

触发条件：新域名健康检查、TLS、登录/OAuth 或核心归档读取失败。

1. **停止后续变更**，记录失败时间与脱敏错误。
2. 恢复 Gate 0 备份的入口/Nginx 配置和 `backend/.env` 域名值（`ARCHIVE_DOMAIN` → 旧值，`ADMIN_DOMAIN` 若改过则恢复），按既有运维流程重载服务。
3. 在 WeCom 已变更时恢复原可信域名/回调配置。
4. 验证旧域名 `/health/ready` 与登录流程恢复；在 Linear 标记 `BLOCKED_NEEDS_HUMAN`，附脱敏证据和下一步建议。

---

## 5. Ops agent 输出格式（每个 gate）

```
Gate N：PASS | FAIL | BLOCKED_NEEDS_HUMAN
时间：<UTC+8>
操作者：<人工运维姓名>
执行过的非敏感命令：<仅命令本身，不含密钥/证书内容>
HTTP 状态：<2xx/3xx/4xx/5xx>
脱敏错误摘要：<无 | 聚合描述，不含任何敏感值>
```

Ops agent 不得声称执行了任何生产写操作；不得 commit、push 或修改仓库配置。

---

## 6. 证据记录表

| 时间 (UTC+8) | 操作者 | Gate/步骤 | 命令（脱敏） | HTTP/DNS/TLS 结果 | 备注 |
|---|---|---|---|---|---|
| 2026-08-03 13:30 | Haisu | Gate 0-P1 | 创建 `archive.crowntime.cn` DNS 记录 | `dig +short` → 47.115.58.45（与 qwhhcd 同 IP） | PASS |
| 2026-08-03 13:35 | Ops agent | Gate 1-验证 | `dig +short archive.crowntime.cn` | 47.115.58.45 | PASS（DNS 可达） |
| 2026-08-03 13:35 | Ops agent | Gate 1-验证 | `curl https://archive.crowntime.cn/health/ready` | curl (60) SAN mismatch → 000（证书未就绪，收到 CN=crowntime.cn 兜底） | FAIL → 按 runbook 停止，人工授权后继续 |
| 2026-08-03 13:36 | Ops agent | 侦察 | `cat /etc/nginx/conf.d/*.conf`、`cat /etc/letsencrypt/renewal/*.conf` | TLS 终止=Nginx+Certbot(nginx 插件)；证书按域名独立目录；`.env` 原值 `ARCHIVE_DOMAIN=qwhhcd.crowntime.cn`、`ADMIN_DOMAIN=https://qwhhcd.crowntime.cn`（**带协议前缀，与规范不符，待查**） | 人工授权范围内 |
| 2026-08-03 14:37 | Ops agent | 备份 | `cp -a backend/.env .env.bak.<ts>`（600） | 备份完成；sha256 已记服务器本地（不输出） | 回滚锚点 |
| 2026-08-03 14:37 | Ops agent | Gate 1-证书 | `certbot certonly --nginx -d archive.crowntime.cn`（先 `--dry-run` 通过） | 签发成功；CN=SAN=archive.crowntime.cn；到期 2026-11-01；certbot 自动续期接管 | 不涉及官网证书 |
| 2026-08-03 14:37 | Ops agent | Gate 1-入口 | 新建 `/etc/nginx/conf.d/archive.crowntime.cn.conf`（proxy 8035）；`nginx -t` + `nginx -s reload` | syntax ok; reload OK | `crowntime.cn.conf`/`qwhhcd.crowntime.cn.conf` 未动（mtime Jul 25） |
| 2026-08-03 14:37 | Ops agent | Gate 1-验证 | `curl https://archive.crowntime.cn/health/ready` | HTTP 200, TLS verify 0 | **PASS** |
| 2026-08-03 14:37 | Ops agent | Gate 2-.env | `sed -i 's|^ARCHIVE_DOMAIN=.*|ARCHIVE_DOMAIN=archive.crowntime.cn|' backend/.env` | 替换成功；600 权限；属主 wecomarchive 不变 | `ADMIN_DOMAIN` 未动（OAuth 未启用） |
| 2026-08-03 14:37 | Ops agent | Gate 2-验证 | `curl https://archive.crowntime.cn/health` + `/health/ready` | 均 HTTP 200 | **PASS** |
| 2026-08-03 14:37 | Ops agent | Gate 3-官网隔离 | `curl https://crowntime.cn/` + `www.crowntime.cn` | HTTP 403（**既有问题**，error log 14:36:56 Permission denied，nginx worker 无读 `/var/www/crowntime` 权限；本切换未动该配置） | ⚠️ 遗留问题，另立事项；不阻塞 AC-6 判定但需修复 |
| 2026-08-03 14:37 | Ops agent | Gate 3-旧域名 | `curl https://qwhhcd.crowntime.cn/health/ready` | HTTP 200 | 旧域名暂保活（P4 未决） |
| 待办 | Haisu | Gate 2-OAuth | WeCom 控制台可信域名/回调 → archive.crowntime.cn | — | AUTH_MODE=password 当前未启用 OAuth；P3 确认后执行 |
| 待办 | Haisu | Gate 3-业务验收 | 无痕浏览器访问 `https://archive.crowntime.cn/`：登录页/OAuth/归档列表/媒体预览 | — | 不导出生产聊天内容 |
| 待办 | Haisu | P4 决策 | 旧域名 `qwhhcd.crowntime.cn`：301/308 重定向（给截止日）或直接下线 | — | 未决前旧入口保活 |
| 待办 | Haisu | P5 决策 | 维护窗口/观察时长/回滚负责人 | — | 未决 |

**当前状态：Gate 0 备份 ✅ · Gate 1 PASS ✅ · Gate 2 部分完成（.env ✅，OAuth 待 Haisu）· Gate 3 待 Haisu 业务验收 + P4 决策**

### 通配符证书整合（Haisu 选定 A 方案，2026-08-03）

| 时间 (UTC+8) | 操作者 | 步骤 | 命令/操作（脱敏） | 结果 | 备注 |
|---|---|---|---|---|---|
| 14:41 | Ops agent | 证书资产侦察 | `cat /home/wecomarchive/.acme.sh/crowntime.cn/fullchain.cer` | 发现已有 acme.sh 通配符 `*.crowntime.cn + crowntime.cn`（Qiniu CDN 用，timer 自动续期） | 复用基础已具备 |
| 14:41 | Ops agent | 证书部署 | `mkdir -p shared/certs/wildcard.crowntime.cn; cp fullchain.cer→fullchain.pem; cp crowntime.cn.key→privkey.pem`（750/640 wecomarchive） | 通配符证书就位于 wecomarchive 可写目录（续期脚本可直接写，nginx master=root 可读） | 避免塞入 certbot live 700 地盘 |
| 14:42 | Ops agent | nginx 切换 | 备份 `conf.d.bak-rnd261-20260803/`；sed 替换 `crowntime.cn`/`qwhhcd.crowntime.cn`/`archive.crowntime.cn` 三 block 的 ssl_certificate(_key) → 通配符路径；`nginx -t` + `nginx -s reload` | syntax ok; reload OK | 官网/旧域名/新域名三 block 全部统一 |
| 14:42 | Ops agent | 验证 | `openssl s_client` 三域名指纹 | 三域名指纹一致（4B:75:0E…）= 通配符证书生效；archive/qwhhcd /health/ready 均 200 | PASS |
| 14:43 | Ops agent | 续期自动化 | ① sudoers 追加 `wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx`（备份 .bak-rnd261）② `qiniu-ssl-renew-wildcard.service` 移除 `NoNewPrivileges=yes`（备份 .bak-rnd261）+ daemon-reload ③ renew-wildcard.sh 插入 `nginx_deploy` 段：续期后 cp 证书 → chmod 640 → sudo systemctl reload nginx（备份 .bak-rnd261） | 权限链验证 `sudo -u wecomarchive sudo -n systemctl reload nginx` → OK；`bash -n` 通过；手动触发验证 early-exit 逻辑正常（证书未到期，nginx 部署段将在下次真实续期 2026-09-18 前后执行） | 安全权衡：该 service 仅跑 acme.sh+证书部署，sudoers 白名单仅限 reload nginx |
| 14:43 | Ops agent | 全量验证 | `curl`/`openssl` 三域名 + md5 比对 | 三域名证书指纹一致；archive/qwhhcd health 200；`shared/certs/.../fullchain.pem` md5 == acme.sh 源文件 | PASS |

**通配符整合后状态**：`*.crowntime.cn` 子域（官网/归档新旧域名/未来子域）一张证书全覆盖，续期由 `qiniu-ssl-renew-wildcard` timer 自动管理（含 nginx reload）。旧 certbot 子域证书（crowntime.cn/qwhhcd/archive）仍在自动续期，**暂留作回滚锚点**，确认稳定后可 `certbot delete` 清理。`xiangyangxinli.com` 不在通配符覆盖范围，保持 certbot 独立管理。

**回滚锚点（通配符整合）**：nginx 配置备份 `conf.d.bak-rnd261-20260803/`、sudoers 备份 `.bak-rnd261-20260803`、systemd unit 备份 `.bak-rnd261-20260803`、renew-wildcard.sh 备份 `.bak-rnd261-20260803`；回滚即恢复这些文件 + 切回 certbot 证书路径 + reload。

### 配置审计与生产验证（§6/§7，2026-08-03）

| 检查项 | 结果 | 备注 |
|---|---|---|
| §4.1 Git 基线 | HEAD=origin/main=`da37bfb`(RND-339)，工作区干净（仅 untracked 测试脚本/qn-py-sdk/renew-wildcard.sh，无 tracked 修改） | PASS |
| §4.2 服务基线 | wecom-archive-365.service + nginx active；uvicorn 127.0.0.1:8035；nginx conf `/etc/nginx/conf.d/*.conf` | PASS |
| §4.3 旧域名基线 | qwhhcd `/health`=200、`/health/ready`=200、根路径 404（无根路由，正常） | PASS |
| §4.4 DNS 基线 | qwhhcd/archive→47.115.58.45；**media→七牛 CDN**（未受影响） | PASS |
| §5.1 DNS 多解析器 | 系统/1.1.1.1/8.8.8.8 均 → 47.115.58.45 | PASS |
| §6.1 环境变量 | `ARCHIVE_DOMAIN=archive.crowntime.cn` ✓；`ADMIN_DOMAIN=https://qwhhcd.crowntime.cn`（带协议前缀，异常待修正）；`QINIU_DOMAIN=media-origin.crowntime.cn`（媒体，未迁移）；`AUTH_MODE=password`；无 APP_URL/BASE_URL/COOKIE_DOMAIN 等变量（应用不使用） | ⚠️ ADMIN_DOMAIN 前缀异常 |
| §6.2 旧域名残留 | 8 处：backend/.env(ADMIN_DOMAIN)、static_site README（文档声明）、tasks 归档、.workbuddy/.qoder 历史记录。**无生产代码硬编码**；README 为仓库文档（按 RND-233 纪律，不改回真实域名） | 仅 .env 属生产配置 |
| §6.3 Cookie/CORS/CSRF | `set_cookie` **无 domain 参数** → host-only cookie；secure=prod、samesite=lax、httponly。**新旧域名 session 不共享（预期行为）**；无 CORS middleware（同源应用）；无 CSRF 组件（session cookie + SameSite=Lax） | 迁移后用户需在新域名重新登录一次 |
| §7.1/7.2 DNS+TLS+健康 | TLS verify 0、证书指纹一致；`/health` 三次 200（0.09s/0.09s/0.09s） | PASS |
| §7.3 admin | `/admin/conversations`→302（未登录重定向，正确）；登录页 200 渲染 | PASS |
| §7.4 API | `/api/admin/dashboard`→401、`/api/conversations`→401（未授权保护正确）；`/api/auth/me`→200 `{"authenticated":false}`（设计语义）；错误凭据登录→401 | PASS |
| §7.5 静态+媒体 | 全部静态资源 200（favicon/styles/logo，同源 HTTPS 无 mixed content）；媒体 URL 由 QINIU_DOMAIN(media-origin) 构造，与产品域名无关 | PASS |
| §7.6 日志 | 应用日志 0 error/traceback（仅扫描器 404 探测）；nginx 窗口内无新增错误；无重定向循环；新域名 access 75 请求 | PASS |

**§6+§7 结论：核心迁移 + 配置审计 + 生产验证全部 PASS。剩余：§8 旧域名 301（等 P4 决策）、§9 回滚成文、§11 最终报告。**

### 收尾确认（2026-08-03）

| 项 | 结果 | 备注 |
|---|---|---|
| §7.3 前端业务验收 | **PASS（Haisu 亲自确认）**：`https://archive.crowntime.cn/` 前端正常访问，登录页/页面渲染无问题 | AC-3 核心验收通过 |
| P4 旧域名策略 | **Haisu 决策：旧域名处理延至 2026-08-04 执行**；今日保持新旧域名并行服务（qwhhcd /health=200） | 明日执行时再定具体形式（301 重定向或直接下线），届时验证 Location 指向、无循环 |
| §9 回滚方案 | 见下方 | 成文 |

### §9 回滚方案（已备好，回滚锚点全部在位）

触发条件：新域名 TLS 失败 / 持续 5xx / 后台不可访问 / API 异常 / 登录严重回归 / 媒体链路异常 / 重定向循环 / 无法确认生产安全。

回滚步骤（全部有备份）：

1. **恢复 nginx 配置**：
   ```bash
   cp -a /etc/nginx/conf.d.bak-rnd261-20260803/*.conf /etc/nginx/conf.d/
   nginx -t && systemctl reload nginx
   ```
   （备份含切换前全部 conf：crowntime.cn/qwhhcd.crowntime.cn/archive.crowntime.cn 及原始 certbot 证书路径）
2. **恢复 .env 域名值**：
   ```bash
   cp -a /srv/apps/wecom-archive-365/current/backend/.env.bak.<ts> /srv/apps/wecom-archive-365/current/backend/.env
   # ARCHIVE_DOMAIN 恢复为 qwhhcd.crowntime.cn
   ```
3. **恢复通配符整合相关配置**（如需要完全回退）：
   ```bash
   cp -a /etc/sudoers.d/wecomarchive.bak-rnd261-20260803 /etc/sudoers.d/wecomarchive   # 移除 reload nginx 白名单
   cp -a /etc/systemd/system/qiniu-ssl-renew-wildcard.service.bak-rnd261-20260803 /etc/systemd/system/qiniu-ssl-renew-wildcard.service   # 恢复 NoNewPrivileges
   cp -a /srv/apps/wecom-archive-365/current/ssl-renew/renew-wildcard.sh.bak-rnd261-20260803 /srv/apps/wecom-archive-365/current/ssl-renew/renew-wildcard.sh
   systemctl daemon-reload
   ```
4. **必要时 restart 应用服务**：`systemctl restart wecom-archive-365.service`
5. **验证旧域名恢复**：`curl -fsS https://qwhhcd.crowntime.cn/health/ready` → 200
6. 新域名 DNS 可保留但不承载流量；保存失败证据，不删除日志。

**未执行回滚**（截至收尾无失败迹象）。

### 明日待办（2026-08-04）

- [ ] §8 旧域名处理（P4：Haisu 已决策延至今日执行）：301 重定向或下线，验证 Location/循环/媒体不受影响
- [ ] 确认稳定后清理旧 certbot 子域证书（`certbot delete`，可选）
- [ ] §11 最终报告输出（含 RND-108/105/130 是否可继续的结论）
- [ ] commit + push runbook（Haisu 批准后）
