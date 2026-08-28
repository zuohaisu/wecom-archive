# 受控非生产部署与安全配置通道

RND-392 为第三方企业授权和自助接入提供一个**唯一**的受控非生产实例。它不是生产的
canary、备用机或数据副本；不得复用生产主机、数据库、CorpID、归档凭据、支付商户、
Qiniu bucket、SMTP 或生产密钥。

## 版本化边界

| 资产 | 路径 | 作用 |
| --- | --- | --- |
| 手动部署工作流 | `.github/workflows/deploy-nonprod.yml` | 只能从 `main` 的精确 SHA 发起；必须通过 GitHub `non-production` Environment 的保护规则。 |
| 部署包装器 | `scripts/deploy_nonprod.sh` | 固定非生产目录、服务、锁、localhost 健康端口和静态目录；清空继承环境后才调用通用部署逻辑。 |
| 配置策略检查 | `scripts/validate_nonprod_config.sh` | 对固定 EnvironmentFile 的所有者、`0600` 权限和非生产安全策略失败关闭。 |
| Web 服务单元 | `deploy/systemd/wecom-archive-365-nonprod.service` | 以独立低权限用户在 `127.0.0.1:18035` 运行，并在启动前执行配置策略检查。 |

生产部署继续只使用 `deploy.yml` 和 `scripts/deploy_server.sh`；本流程不会改写生产目录、
`wecom-archive-365.service`、生产锁或生产静态站点目录。

## 固定隔离边界

| 项目 | 非生产固定值 |
| --- | --- |
| Linux 用户 / 组 | `wecomarchive-nonprod` |
| checkout | `/srv/apps/wecom-archive-365-nonprod/current` |
| 运行时配置 | `/etc/wecom-archive-365/nonprod.env` |
| 数据库名 | `wecom_archive_nonprod` |
| 服务名 | `wecom-archive-365-nonprod.service` |
| 内部健康检查 | `http://127.0.0.1:18035/health/ready` |
| 共享状态、媒体与静态目录 | `/srv/apps/wecom-archive-365-nonprod/shared/` |

不得通过环境变量、符号链接或 systemd drop-in 将其中任一项指向生产资源。非生产不安装
`wecom-archive-worker`、media、export、billing 或其他定时/路径触发 worker；T0 阶段只
运行 Web 服务和第三方授权回调所需路径。

## 安全配置通道

运行时密钥**不进入 Git、GitHub Issue、PR、GitHub Actions 日志或命令行**。唯一批准的通道是
由 `wecomarchive-nonprod` 拥有且权限严格为 `0600` 的
`/etc/wecom-archive-365/nonprod.env`。创建或轮换必须在受控主机上由获批运维人员使用
组织批准的秘密管理系统完成；不得通过 `scp`、shell history、`echo KEY=value`、截图或
工单评论传递值。

该文件为 data-only `KEY=value` 格式，必须至少包含：

- `NONPROD_DEPLOYMENT=1`、`APP_ENV=staging`、`AUTH_MODE=password`；
- 专用 `DATABASE_URL`（数据库必须为 `wecom_archive_nonprod`）和专用
  `FIELD_ENCRYPTION_KEY`、`SETTINGS_ENCRYPTION_KEY`；
- 非生产 password-mode 管理员的 `ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH`，以及相同的
  `ADMIN_DOMAIN` / `ARCHIVE_DOMAIN`；
- 测试服务商应用的五个 `WECOM_THIRD_PARTY_*` 配置值，且
  `WECOM_THIRD_PARTY_CALLBACK_URL` 精确为
  `https://${ARCHIVE_DOMAIN}/api/auth/wecom/third-party/callback`；
- `WECHAT_PAY_ENABLED=false`、`MEDIA_STORAGE_PROVIDER=local`、
  `KEY_PROVIDER=local_file` 和固定非生产 `STORAGE_LOCAL_PATH`。

在写入 `FIELD_ENCRYPTION_KEY` 前，必须先执行
[第三方授权运维说明的只读核查](wecom-third-party-authorization.md#field_encryption_key-配置前只读核查)；
任何加密行计数非零即停止并由受控密钥系统确认既有 key。策略检查还会拒绝：任意生产
archive callback/secret/private-key/OAuth 值、CorpID、Qiniu、SMTP、支付凭据、非专用
数据库名、错误所有者或非 `0600` 权限。它只给出固定的失败结果，不打印文件路径或任何值。
配置检查通过不等于密钥正确；应用自身仍对无效 Fernet key 或不完整第三方配置失败关闭。

## GitHub 保护面

由仓库管理员创建 **GitHub Environment `non-production`**，并配置：

1. 至少一名不同于触发人的 required reviewer；禁止自行审批；只允许受保护的 `main` 分支。
2. 仅在该 Environment 内保存 `NONPROD_DEPLOY_HOST`、`NONPROD_DEPLOY_USER`、
   `NONPROD_DEPLOY_SSH_KEY`、`NONPROD_DEPLOY_PORT`。这些仅用于 SSH 传输，**不是**应用
   的运行时密钥。
3. 禁止在 repository-level secrets、workflow inputs 或 GitHub variables 中保存
   `DATABASE_URL`、Fernet key、WeCom secret/token/AES key、password hash 或任何运行时
   配置值。
4. 用最小权限 SSH key：只能登录 `wecomarchive-nonprod`，不能 sudo 到生产用户或读取
   生产目录；在服务器侧限制该 key 的来源和用途。

工作流只接受从 `main` 手动选择的精确 `github.sha`，对非生产部署单独串行化，并在远端
checkout 前获取非生产锁。它不响应 push，也不使用生产 secrets。

## 首次部署与验证

运维先准备隔离主机、专用 PostgreSQL 角色/数据库、低权限用户、checkout、venv、反向
代理和上述 systemd 单元；然后在受控通道写入 EnvironmentFile。配置文件和服务单元变更后
依次执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-365-nonprod.service
sudo systemctl status wecom-archive-365-nonprod.service --no-pager
curl -fsS http://127.0.0.1:18035/health/ready
```

以上命令不应显示环境变量或读取配置文件内容。若服务的 `ExecStartPre` 失败，修复权限或
策略项后重试；不要通过放宽验证、复制生产 `.env` 或暂时注入生产值绕过。

在 GitHub Environment 审批后，使用 Actions 的 **CD - Deploy to Non-Production** 手动
工作流部署 `main` 的指定 SHA。成功标准为 GitHub job 成功、内部和公开 `/health` 均通过、
`systemctl` 显示服务运行，以及日志不含凭据或身份资料。再按
[企业微信第三方企业授权运维说明](wecom-third-party-authorization.md#非生产端到端验收)
执行测试企业 E2E。

## 回滚与事件处理

部署包装器复用已有的迁移、revision、readiness 和 code rollback 保护，但状态只位于
非生产根目录。绝不自动执行数据库 downgrade。配置策略失败、公开健康检查失败、意外
worker 被启用、任何生产标识/凭据出现，或日志可能泄露秘密时：立即停止非生产服务和部署，
撤销相关测试凭据，通知安全负责人；不要把疑似值复制到工单用于排障。
