[Goal check] This work advances RND-261 production-cutover preparation by producing a human-executable, evidenced domain-change runbook.

# RND-261 Ops 工作提示词：`qwhhcd.crowntime.cn` → `archive.crowntime.cn`

- Linear: https://linear.app/xyzhs1897/issue/RND-261/二级域名变更qwhhcdcrowntimecn-archivecrowntimecn
- 风险等级：**R3（生产 DNS / TLS / 部署 / OAuth）**
- 执行边界：Ops agent **不得自行登录生产、修改 DNS/Nginx/WeCom/证书、重启服务或读取/输出任何密钥**。依照 `DEV_AGENT_RULES.md`，所有生产写操作由 Haisu（或获授权的人工运维）执行；agent 只可逐步提示、记录证据并在每个 gate 等待人工确认。

## 目标
在不影响归档服务、登录流程或公司官网的前提下，将归档服务的规范公网域名切换为 `archive.crowntime.cn`，并保留可验证、可回滚的证据链。

## 现状
- 仓库中的部署脚本以生产 `backend/.env` 的 `ARCHIVE_DOMAIN` 为准；`.env.example` 不含真实域名。
- `scripts/deploy_server.sh` 的默认 public-health URL 由 `ARCHIVE_DOMAIN` 构造。
- 当前仓库没有受控的生产 Nginx、DNS 或 WeCom 控制台配置；不得猜测它们的路径、供应商或现有值。
- `static_site/company_homepage/` 是官网，必须继续仅服务 `crowntime.cn` 与 `www.crowntime.cn`，不能被归档站切换影响。

## 前置人工确认（缺一即停止，标记 `BLOCKED_NEEDS_HUMAN`）
1. 新域名的 DNS 记录类型和值（A/AAAA/CNAME）及目标 IP/入口已由域名管理员确认。
2. 当前归档入口的 TLS 终止位置（Nginx、负载均衡或 CDN）和证书签发/续期负责人已确认。
3. WeCom OAuth 当前使用的可信域名及回调 URL 已确认；仅当它当前确实使用旧域名时才变更 `ADMIN_DOMAIN` 和 WeCom 控制台配置。
4. Haisu 决定旧域名在切换后是：
   - 暂时 301/308 重定向至新域名（给出截止日期），或
   - 直接下线。
   未作决定时不得擅自配置重定向或保留旧入口。
5. 已约定维护窗口、观察时长及可联系的回滚负责人。

## 人工执行步骤（agent 逐项提示并收集脱敏结果）

### Gate 0：变更前取证与回滚准备
由人工运维完成：
1. 记录当前 DNS、入口配置、服务状态、当前生效证书 SAN/到期日，以及当前 `ARCHIVE_DOMAIN` / `ADMIN_DOMAIN` 的**域名值**。不得把 `.env` 全文、令牌、密钥或连接串贴入记录。
2. 创建受权限保护的生产 Nginx/入口配置和 `backend/.env` 备份；记录备份位置与校验值，不将备份提交仓库。
3. 在维护窗口前验证旧入口健康：
   ```bash
   curl --fail --silent --show-error https://qwhhcd.crowntime.cn/health/ready
   ```
4. 将结果（时间、操作者、HTTP 状态）写入 Linear 评论或受控运维记录。失败则停止切换。

### Gate 1：新域名可达与 TLS 就绪
由人工运维完成：
1. 创建并传播 `archive.crowntime.cn` 的 DNS 记录，目标与现有归档入口一致。
2. 在 TLS 终止层为 `archive.crowntime.cn` 签发并部署有效证书；证书 SAN 必须覆盖该域名。不得替换、删除或影响官网 `crowntime.cn` / `www.crowntime.cn` 的证书与 server block。
3. 在 DNS/证书已生效后验证：
   ```bash
   dig +short archive.crowntime.cn
   curl --fail --silent --show-error --head https://archive.crowntime.cn/health/ready
   openssl s_client -connect archive.crowntime.cn:443 -servername archive.crowntime.cn </dev/null 2>/dev/null \
     | openssl x509 -noout -subject -ext subjectAltName -dates
   ```
4. 确认 HTTP 证书验证成功、SAN 含新域名、健康端点返回 2xx；任一项失败则停止，不进入切换。

### Gate 2：应用与 OAuth 切换
由人工运维完成：
1. 在生产 `backend/.env` 中仅将 `ARCHIVE_DOMAIN` 设为：
   ```dotenv
   ARCHIVE_DOMAIN=archive.crowntime.cn
   ```
   保留其余配置原样；不要将该文件复制到仓库或聊天记录。
2. 仅在 Gate 0 已证实 OAuth 正使用旧域名时：
   - 在 WeCom 管理后台将可信域名/授权回调地址更新为新域名；
   - 将生产 `ADMIN_DOMAIN` 改为 `archive.crowntime.cn`；
   - 按现有受控方式重启/重载应用。
   如果 `ADMIN_DOMAIN` 是不同的管理域名，保持它不变，并在记录中注明。
3. 按现有生产 runbook 重载入口服务和应用；不得临时改 CI/CD、部署脚本或 systemd 单元。

### Gate 3：业务验收与旧域名策略
由人工运维完成：
1. 用无痕浏览器或独立会话访问 `https://archive.crowntime.cn/`，完成登录页加载、OAuth 登录（若适用）、归档列表读取及媒体预览各一次。不得使用或导出生产聊天内容作为证据，只记录成功/失败、时间和 HTTP 状态。
2. 验证：
   ```bash
   curl --fail --silent --show-error https://archive.crowntime.cn/health
   curl --fail --silent --show-error https://archive.crowntime.cn/health/ready
   ```
3. 严格按 Gate 0 的人工决策执行旧域名策略：若选择重定向，验证它仅定向到新归档域名；若选择下线，验证旧域名不再指向归档应用。
4. 再次验证 `https://crowntime.cn/` 与 `https://www.crowntime.cn/` 仍提供官网，而不是归档应用。
5. 在约定观察窗口内检查入口错误率、应用日志中的 host/OAuth 错误和健康检查；只记录聚合指标与脱敏错误摘要。

## 验收标准
- AC-1：`archive.crowntime.cn` DNS 可解析，HTTPS 证书链有效，证书 SAN 覆盖新域名。
- AC-2：新域名的 `/health` 与 `/health/ready` 均返回 2xx。
- AC-3：归档页面与登录流程在新域名可用；若 OAuth 使用该域名，WeCom 回调成功。
- AC-4：`ARCHIVE_DOMAIN` 的生产值为 `archive.crowntime.cn`；`ADMIN_DOMAIN` 仅在需要时更新。
- AC-5：旧域名行为符合 Haisu 在 Gate 0 的明确决策。
- AC-6：主站 `crowntime.cn` / `www.crowntime.cn` 未受影响。
- AC-7：生产密钥、完整 `.env`、生产聊天内容及证书私钥未出现在仓库、Linear 评论或 agent 输出中。

## 回滚（仅人工执行）
若新域名健康检查、TLS、登录/OAuth 或核心归档读取失败：
1. 停止后续变更并记录失败时间与脱敏错误。
2. 恢复 Gate 0 备份的入口/Nginx 配置和 `backend/.env` 域名值，按既有运维流程重载服务。
3. 在 WeCom 已变更时恢复原可信域名/回调配置。
4. 验证旧域名 `/health/ready` 和登录流程恢复；在 Linear 标记 `BLOCKED_NEEDS_HUMAN`，附脱敏证据和下一步建议。

## Ops agent 输出格式
每个 gate 只输出：`PASS` / `FAIL` / `BLOCKED_NEEDS_HUMAN`、时间、操作者、执行过的非敏感命令、HTTP 状态和脱敏错误摘要。不得声称执行了任何生产写操作；不得 commit、push 或修改仓库配置。
