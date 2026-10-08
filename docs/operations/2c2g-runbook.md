# 2c2g 生产运维手册 — 日志 / 磁盘 / 备份治理 (RND-193)

> 范围：2 核 2GB 阿里云 ECS 生产服务器（`ssh ali-xy-qw`，见项目 SSH 访问记录）上的日志轮转、
> 磁盘/inode 容量监控、数据库与媒体备份/恢复。对应历史 RND-193（父任务 RND-160）；GitHub Issues 是当前工单系统。
> 本文档記錄的是**已在生产验证过的实际状态**，不是设计草案。

---

## 1. RED 基线（2026-07-27 测量，RND-190 口径）

| 项目 | 数值 | 说明 |
|---|---|---|
| 磁盘 | 12G/40G 已用 (31%), 27G 可用 | `/dev/vda3` |
| inode | 103528/2608144 已用 (4%) | |
| 内存 | 1.8G 总, 249M 已用, 1.2G buff/cache, 1.3G available | |
| swap | 4G 总, 61M 已用 (1.5%) | |
| PG 连接数 | 7/100 | |
| PostgreSQL 数据目录 | 197MB (数据库本身 140MB) | `/var/lib/pgsql/data` |
| PostgreSQL 日志 | 36KB 总 | 见 §2.3，自身已有界 |
| journal | 525.5M（`journalctl --disk-usage`） | 见 §2.1，已有 500M 上限治理（先于本次改动存在） |
| 本地媒体（`STORAGE_LOCAL_PATH`） | 33MB / 122 文件 | 新媒体已改写入七牛，见 §5.1 |
| 备份现状（改动前） | **仅 1 份手工快照**（`pre-rnd-111-2026-06-28-234547.sql`, 38KB, 已一个月陈旧） | crontab（root + wecomarchive）无任何定时备份任务；从未做过恢复演练 |
| `/tmp` | tmpfs, 945M 上限（约为 1.8G 内存的一半） | 影响备份脚本的临时文件落地位置，见 §5.4 |

**结论**：日志轮转（journald/nginx/PostgreSQL）在改动前**已经有效治理**，真正的缺口是磁盘/inode/内存/PG连接的主动监控，以及数据库+媒体的定期加密备份与恢复演练——这两项是本次 RND-193 新增的内容。

---

## 2. 日志轮转与保留（已治理，本次未修改配置）

### 2.1 systemd journald

生产已有 `/etc/systemd/journald.conf.d/size-limit.conf`（创建于 2026-06-26，早于本次改动）：

```ini
[Journal]
SystemMaxUse=500M
SystemKeepFree=2G
MaxRetentionSec=14day
```

验证：
```bash
grep -E "SystemMaxUse|MaxRetentionSec" /etc/systemd/journald.conf.d/size-limit.conf
journalctl --disk-usage
```

**本次未改动**——现有上限对 2c2g 已合理，无需调整。若未来需要调整，回滚方式：删除该 drop-in 文件或恢复其历史内容后 `systemctl restart systemd-journald`。

### 2.2 Nginx

OS 默认 `/etc/logrotate.d/nginx`（非本项目定制）已覆盖：daily / rotate 10 / compress / `USR1` 重开日志文件，无需为本项目单独新增 logrotate 规则。

### 2.3 PostgreSQL

`postgresql.conf` 现有设置已自成闭环，**不需要**（也不应该）再叠加 logrotate 规则：

```
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%a.log'   -- %a = 星期几，7 天循环覆盖文件名
log_rotation_age = 1d
log_truncate_on_rotation = on         -- 循环写回同名文件时先截断
```

效果：最多同时存在 7 个按星期命名的日志文件，每周同一天的文件会被截断重写，总量长期稳定在几十 KB 量级（实测 36KB）。若在此基础上再加 logrotate 规则，会产生双重轮转、意义不明的重复归档文件，属于不应该做的改动。

### 2.4 应用日志

后端走 stdout → systemd journald（未使用 `RotatingFileHandler` 落盘），已被 §2.1 的 journald 上限覆盖，无需额外处理。

---

## 3. 磁盘 / inode / 内存 / PG 连接监控（新增）

脚本：[`scripts/disk_usage_check.sh`](../../scripts/disk_usage_check.sh)

### 3.1 检查项与默认阈值

| 检查项 | 计算方式 | 默认阈值（env 可覆盖） |
|---|---|---|
| 磁盘 | `df -P /` 的 Use% | `DISK_WARN_PCT=80` |
| inode | `df -iP /` 的 IUse% | `INODE_WARN_PCT=80` |
| 内存压力 | `100 * (1 - MemAvailable/MemTotal)`（不是简单的“已用”，因为 buff/cache 可回收） | `MEM_WARN_PCT=90` |
| swap | `100 * (SwapTotal-SwapFree)/SwapTotal` | `SWAP_WARN_PCT=50` |
| PostgreSQL 连接数 | `count(*) FROM pg_stat_activity` / `max_connections` | `PG_CONN_WARN_PCT=80`；`DATABASE_URL` 未设置时该项直接跳过（非失败） |

任何一项超标即通过项目**既有**的 `ssl-renew/notify.sh`（`ALERT_WEBHOOK_URL` 环境变量约定，未配置时自动降级为仅记录日志，不会报错）发一次告警，脚本本身 exit 1（因此 `systemctl status` 本身也是超标的第二个可见信号，这是有意设计，不是 bug）。PostgreSQL 不可达时报 `DEGRADED`（不是 `WARN`），不会误触发告警。

### 3.2 调度

`deploy/systemd/wecom-disk-usage-check.{service,timer}`，`OnCalendar=*:9/15`（每 15 分钟，在 09/24/39/54 分触发）——刻意错开 RND-192 已有的两个定时器（worker `*:0/5`，media `*:2/5`），任何时刻都不会与它们撞点。

验证（生产已确认）：
```bash
systemctl list-timers wecom-disk-usage-check.timer
journalctl -u wecom-disk-usage-check.service -n 20
```

### 3.3 告警链路验证（生产已实测）

2026-07-27 用临时 `DISK_WARN_PCT=1` 手动跑过一次（不改动已安装的 timer 配置），确认 WARN → notify.sh → `[DEGRADED] ALERT_WEBHOOK_URL not configured` 的降级路径按预期工作。**当前尚未配置 `ALERT_WEBHOOK_URL`**——告警目前只落 journal，不会真正推送到任何外部渠道。如需接入真实告警，在 `backend/.env` 或专用 env 文件中设置 `ALERT_WEBHOOK_URL`（不要硬编码进脚本或仓库）。

### 3.4 卸载/回滚

```bash
systemctl disable --now wecom-disk-usage-check.timer
rm /etc/systemd/system/wecom-disk-usage-check.{service,timer}
systemctl daemon-reload
```

---

## 4. 安全清理命令与不可清理边界

| 目录/文件 | 状态（2026-07-27 实测） | 处理方式 |
|---|---|---|
| `shared/run/*.lock` | 3 个 0 字节 flock 文件，永久保持 0 字节 | **不清理**——这是正常的锁文件行为，不是增长源 |
| 媒体下载 `.part` 残留 | 实测 0 个 | 已有失败即删逻辑（RND-192 验证过），无需新增清理 |
| `shared/backups/*.gpg` | 由 `backup_once.sh` 自身按 `BACKUP_RETENTION_DAYS`（默认 7 天）清理 | 仅清理自己命名模式 `wecom_archive-*.gpg` 的文件，从不触碰其他文件 |
| `shared/backups/.tmp/` | 备份脚本的加密前暂存目录，`trap` 保证每次运行后清空 | 若发现残留文件（正常运行不应发生），可手动 `rm -rf` 后确认下次运行正常 |

**绝不清理**（无论磁盘多紧张）：
- `archive_messages` / `archive_message_recipients` 等生产归档数据（除非有明确批准 + 可验证备份）
- `shared/media`、七牛中的媒体对象
- `shared/private_keys`、`shared/keys`、`backend/.env`、`shared/backup.env`
- `shared/backups/pre-rnd-111-2026-06-28-234547.sql`（历史人工快照，是否清理需人工判断，不属于自动清理范围）

---

## 5. 备份 / 恢复（新增）

脚本：[`scripts/backup_once.sh`](../../scripts/backup_once.sh)

### 5.1 备份范围

| 内容 | 是否备份 | 说明 |
|---|---|---|
| PostgreSQL 全库（`pg_dump -Fc`） | ✅ | 唯一数据副本来源 |
| 本地历史媒体（`STORAGE_LOCAL_PATH`，`storage_backend=local`） | ✅（tar + gzip） | 这是唯一存在于本地磁盘的媒体副本 |
| 七牛 Kodo 媒体（`storage_backend=qiniu_kodo`，当前新媒体的写入后端） | ❌ 刻意不备份 | 已经存于七牛自己的持久化对象存储中，不是"唯一副本"，重复备份没有意义 |

### 5.2 加密

- 每份产物（DB dump、媒体 tar）在写入 `BACKUP_DIR` 前必须经 GPG 对称加密（`--cipher-algo AES256`），从未落地过明文文件到 `BACKUP_DIR`。
- 密码通过 `--passphrase-fd 0` 传入，从不出现在 argv / `ps` 输出中；`BACKUP_GPG_PASSPHRASE` 未设置时脚本直接拒绝运行，不存在"先写明文再补加密"的中间态。
- **密码存放**：`shared/backup.env`（600 权限，`wecomarchive:wecomarchive`），与 `backend/.env` **分开存放**——这样主应用进程读取 `backend/.env` 时不会连带拿到备份密码。密码本身已在生成时经由本次会话一次性展示给用户，要求其保存到密码管理器；**本文档不记录密码明文**。丢失该密码 = 已有备份永久不可恢复。

### 5.3 暂存目录（重要——避免吃掉宝贵内存）

生产服务器的 `/tmp` 是 **tmpfs**（RAM 支持，945M 上限，约为 1.8GB 内存的一半）。若备份脚本像常见做法一样把加密前的明文暂存到 `$TMPDIR`/`/tmp`，会在备份运行期间直接和应用/worker 抢内存，且理论上可能撞到 tmpfs 容量上限。因此 `backup_once.sh` 把暂存目录固定为 `BACKUP_DIR/.tmp`（磁盘上，而非内存），由 `trap` 保证每次运行结束后清空，不依赖调用方是否记得设置 `TMPDIR`。

### 5.4 调度与保留

`deploy/systemd/wecom-backup.{service,timer}`，每天 `03:17` CST（业务低峰期，不与任何其他定时器抢时间片）。保留策略：`BACKUP_RETENTION_DAYS=7`（默认），仅在**新备份成功之后**才执行旧文件清理——失败的一次运行永远不会导致失去现有备份。

验证（生产已确认）：
```bash
systemctl list-timers wecom-backup.timer
journalctl -u wecom-backup.service -n 40
ls -la /srv/apps/wecom-archive-365/shared/backups/
```

### 5.5 GNUPGHOME

`wecom-backup.service` 用 `ProtectHome=yes` 加固（隐藏真实 `$HOME`），因此 GPG 默认的 `~/.gnupg` 无法创建。unit 文件显式设置 `Environment=GNUPGHOME=/srv/apps/wecom-archive-365/shared/gnupg`（700 权限，`wecomarchive:wecomarchive`，2026-07-27 创建），已在生产验证可正常创建 keybox 并完成加解密。

### 5.6 恢复流程（已在生产完整演练一次，见 §6）

```bash
# 1. 解密（PASSPHRASE 从 shared/backup.env 读取，绝不回显/记录）
PASSPHRASE=$(grep '^BACKUP_GPG_PASSPHRASE=' /srv/apps/wecom-archive-365/shared/backup.env | cut -d= -f2-)
sudo -u wecomarchive bash -c "GNUPGHOME=/srv/apps/wecom-archive-365/shared/gnupg printf '%s' '$PASSPHRASE' | \
  gpg --batch --yes --passphrase-fd 0 --pinentry-mode loopback -d '<备份文件>.dump.gpg' > /srv/apps/wecom-archive-365/shared/backups/.tmp/restored.dump"

# 2. 恢复到一个新的库（绝不直接恢复进生产库！）
sudo -u postgres createdb <临时库名>
sudo -u wecomarchive cat /srv/apps/wecom-archive-365/shared/backups/.tmp/restored.dump | \
  sudo -u postgres pg_restore -d <临时库名> --no-owner --no-privileges

# 3. 抽样校验（行数 + 内容 checksum，见 §6 的具体查询）

# 4. 清理
sudo -u postgres dropdb <临时库名>
rm -rf /srv/apps/wecom-archive-365/shared/backups/.tmp/restored.dump
```

媒体 tar 的解密同理，用 `tar tz`/`tar xz` 代替 `pg_restore`。

### 5.7 重要边界声明

**单机本地备份 ≠ 完整灾备方案。** 当前所有备份产物都只存放在同一台服务器的同一块磁盘上（`shared/backups/`）——服务器本身、磁盘、或该 ECS 实例整体发生故障时，这些备份会与生产数据**同时丢失**。这不是本次任务的目标范围（RND-193 non-goal 明确排除"完整灾备"），但必须在此明确写出，避免误以为现状已经等同于异地容灾。若需要真正的灾备，后续需要把加密后的产物同步到第二个物理位置（例如另一台服务器、对象存储的独立 bucket/区域），当前脚本尚未实现这一步。

---

## 6. 恢复演练结果（2026-07-27，生产已完整执行）

| 步骤 | 结果 |
|---|---|
| 首次真实备份 | ✅ 成功。DB dump 31MB（加密后），媒体 tar 29MB（加密后），`backup_once complete (db=1 media=1)` |
| 解密 | ✅ 成功，还原出 32MB 明文 dump |
| 恢复到临时库 `wecom_archive_restore_test` | ✅ `pg_restore` exit 0，无 error |
| 行数抽样校验 | ✅ `archive_message_recipients` 372610、`archive_messages` 3919、`media_files` 334、`admin_sessions` 69 —— 与生产库完全一致 |
| 内容 checksum 校验 | ✅ `archive_messages` 的 `msgid‖tenant_id‖msgtime‖decrypt_status` 拼接后 md5 在生产库与恢复库中完全相同（`40269fd55f7fe47b30ab7a4c1ca30b4a`） |
| 清理 | ✅ 临时库已 drop，解密产生的明文文件已删除，生产库 `wecom_archive` 全程只读访问、未被修改 |

**结论：恢复流程验证通过，不是纸面流程。** 无遗留缺口。

---

## 7. 已知缺口 / 后续待办

- **`ALERT_WEBHOOK_URL` 尚未配置**——磁盘/inode/内存/PG连接告警目前只写入 journal，没有推送到任何外部渠道（Slack/钉钉/短信等）。需要时直接复用 `ssl-renew/notify.sh` 的既有约定配置该变量即可，脚本本身无需改动。
- **单机本地备份不是完整灾备**——见 §5.7。
- **磁盘/日志增长速率目前是单点测量，不是长期趋势**——`disk_usage_check.sh` 从 2026-07-27 起才开始每 15 分钟运行一次；几周后可以基于 journal 历史记录回看真实增长曲线，本文档的 RED 数字仅代表启用监控前的单次快照。

---

## 8. GH-105：异地恢复配置包（recovery-config bundle）

### 8.1 这一节要解决什么

§5.7 已经写明："单机本地备份 ≠ 完整灾备方案"——`backup_once.sh` 产出的 DB/媒体加密备份，本身也只存放在同一台阿里云 ECS 的同一块磁盘上。GH-105 三 failure-domain 审计进一步确认：`BACKUP_GPG_PASSPHRASE` 已经在运维本人的 KeePassXC 中有独立副本（因此"备份无法解密"这个 P0 已消除），但**恢复一台全新服务器所需要的 config / 密钥材料本身没有任何异地副本**——这些内容不在 Git 里（`.gitignore` 排除 `.env`/`*.pem`），不在任何 provider 控制台里，也不在密码管理器里。阿里云整机丢失 = 这些文件永久丢失，即使 DB/媒体密文备份本身完好，也无法在新主机上还原出一个可运行的应用（`FIELD_ENCRYPTION_KEY` 丢失会让 `key_versions` 表里每一个 `kms_envelope` 存储的租户私钥永久不可解密——见 `backend/app/crypto.py`、`backend/app/key_provider.py`、`docs/key-hosting-and-tenant-isolation.md`）。

`scripts/dr_config_bundle.sh` 补的就是这一个缺口：把这些 recovery-critical 文件本身（不是 DB/媒体数据）打包、加密、生成 checksum/manifest，交给 Ops 拉到腾讯云新加坡 + 本地 Mac 两个异地目的地。它完全独立于 `backup_once.sh`，不复用 `BACKUP_GPG_PASSPHRASE`，不改动现有备份主链。

### 8.2 Recovery-critical inventory（显式 allowlist，不递归打包 `shared/`）

| 路径 | Required? | 判断依据 |
|---|---|---|
| `backend/.env` | **必需** | 运行时环境机密的来源：DB 凭据、`FIELD_ENCRYPTION_KEY`（`key_versions` 字段加密根密钥）、支付/短信等第三方凭据。归档 secret 属于加密的 tenant-scoped 配置，不再从全局 `WECOM_ARCHIVE_SECRET` 读取。不在 Git、provider 控制台或密码管理器里，无法重建。 |
| `shared/keys/` | **必需** | 磁盘上的 RSA 私钥材料（如 `private_key_v1.pem`）。WeCom 无法"重新签发"丢失的私钥，只能轮换出一把新的——那是一次生产事故，不是一次恢复。 |
| `shared/private_keys/` | 可选 | §4 "绝不清理"清单里与 `shared/keys` 并列出现的历史路径命名；不确定当前是否仍在使用，存在则一并打包，不存在不影响恢复（不 fail-closed）。 |
| `shared/backup.env` | 可选 | 只含 `BACKUP_GPG_PASSPHRASE`——该密码已在 KeePassXC 有独立副本，**不是**本 bundle 要补的缺口；作为纵深防御选择性打入密文内部（bundle 目的地本身只落密文，不会因此在异地暴露明文）。 |
| `shared/certs/` | 可选 | 通配符 `*.crowntime.cn` 证书可通过 acme.sh 重新签发（见 `docs/operations/scheduled-workload-manifest.md`），不是"唯一副本"；打入 bundle 纯粹是为了缩短 RTO，不是恢复的硬依赖。 |
| `shared/deploy_state/` | 可选 | `last_known_good_sha` 等内容可以从新 checkout 的 `main` HEAD 直接推导，不是不可重建状态；打入 bundle 只是为了让恢复后的自动回滚目标保持连续。 |

**明确排除、不进 inventory：** `shared/gnupg`（GNUPGHOME，只是 gpg 对称加密的会话/信任状态，不含密钥材料，`mkdir -p && chmod 700` 即可重建，无需备份）。

Required 项缺失时脚本 fail closed（非零退出，不写出任何 bundle），且只打印"缺了哪个 label"，从不打印文件内容。Optional 项缺失时正常跳过，不影响 bundle 生成。

### 8.3 生成 bundle

```bash
export RECOVERY_PASSPHRASE='...'   # 独立密钥，不复用 BACKUP_GPG_PASSPHRASE，只从环境变量读取
cd /srv/apps/wecom-archive-365/current
bash scripts/dr_config_bundle.sh
```

产出（默认写入 `shared/dr_bundles/`，与 `backup_once.sh` 的 `shared/backups/` 是两个独立目录，互不干扰各自的保留策略）：

- `wecom_archive-recovery-config-<UTC时间戳>.tar.gz.gpg` —— 唯一的明文落地就是这个文件的加密结果；staging 目录全程 `umask 077` + `mktemp -d` + `chmod 700`，成功/失败都通过 `trap` 清理，从不在磁盘上留下明文 tar。
- `....tar.gz.gpg.sha256` —— 标准 `sha256sum`/`sha256sum -c` 兼容格式，校验的是密文本身。
- `....manifest.json` —— `bundle_version`、`bundle_type`、`artifact`、`sha256`、`size_bytes`、`created_at`，以及每一项 inventory 的 `required`/`included` 状态。**不含任何密钥值、env 值，也不含 passphrase**。

`RECOVERY_PASSPHRASE` 全程只经 `--passphrase-fd 0` 管道传给 gpg，从不出现在 argv/`ps`/日志/manifest 中——与 `backup_once.sh` 对 `BACKUP_GPG_PASSPHRASE` 的处理方式完全一致（同一个已验证安全的模式）。

默认不做任何保留策略清理（不硬编码 Aliyun/腾讯云/Mac 各自的保留期——那是 Ops policy）；如需要，显式设置 `BUNDLE_RETENTION_DAYS` 才会清理这台机器自己产出的旧 bundle。

### 8.4 异地拉取 + 校验（`dr_pull.sh`，在目的地运行）

腾讯云新加坡与本地 Mac 两个目的地需要完全相同的"先校验、通过才能成为新的 known-good，校验失败绝不覆盖已有 known-good"逻辑，这部分容易出错，且两个目的地要复用同一套——因此提供了这一个小工具，而不是每个目的地各写一份 shell 一次性脚本。它只负责这一个 bundle 的拉取校验，**不覆盖** `backup_once.sh` 的 DB/媒体 `*.gpg`（那些目前没有配套 checksum 文件，属于 Ops 可以直接用 `rsync`/`sha256sum` 手动处理的范围，见 §5.6）。

```bash
# 在目的地（腾讯云主机 或 Mac）上运行，从生产拉取
export PULL_SOURCE='wecomarchive@ali-xy-qw:/srv/apps/wecom-archive-365/shared/dr_bundles/'
export PULL_DEST_DIR='/path/to/local/verified/tree'
bash scripts/dr_pull.sh
```

不变量（已通过 `scripts/tests/dr_pull.bats` 覆盖）：

- 校验失败 → 该文件不进入 `PULL_DEST_DIR`，之前已验证的旧副本原样保留，**从不被覆盖或删除**。
- 缺 `.sha256` 伴随文件 → 视为无法校验，同样不 promote。
- 重复执行 → 已校验且未变化的文件直接 SKIP，不是错误。
- 生产侧 `dr_config_bundle.sh` 正在写 `.tmp/` 明文暂存目录时也在跑 pull → `.tmp` 被显式排除在 rsync 之外，永远不会被拉取或 promote。

Ops 后续负责：把这条命令接到腾讯云和 Mac 各自的调度（cron/launchd/systemd timer 均可），以及两地各自的保留策略——这些不属于本次 repo 改动范围。

### 8.5 Scenario — 阿里云整机丢失后的完整恢复顺序

1. **置备替代主机**（腾讯云或任意新 ECS/VPS）——Ops 操作，本文档不覆盖具体云厂商步骤。
2. **恢复仓库/应用**：`git clone` 本仓库到新主机的 `/srv/apps/wecom-archive-365/current`（参照 `docs/DEPLOYMENT.md` §2 Bootstrap）。
3. **取回加密的 DB/媒体备份**：从腾讯云或 Mac 上已同步的 `shared/backups/*.gpg` 副本（Ops 异地同步产物，不是本次改动范围）。
4. **取回加密的 recovery-config bundle**：从 `dr_pull.sh` 已校验的 `PULL_DEST_DIR` 中取最新的 `wecom_archive-recovery-config-*.tar.gz.gpg`。
5. **从运维本人的密码管理器取回 `RECOVERY_PASSPHRASE`**（以及需要时的 `BACKUP_GPG_PASSPHRASE`）——两把密码都只存在于人的记忆/密码管理器中，任何脚本都不会替你读取。
6. **解密 config bundle**：
   ```bash
   printf '%s' "$RECOVERY_PASSPHRASE" | \
     gpg --batch --yes --passphrase-fd 0 --pinentry-mode loopback \
       -d wecom_archive-recovery-config-<TS>.tar.gz.gpg > /tmp/recovery-config.tar.gz
   tar xzf /tmp/recovery-config.tar.gz -C /tmp/
   shred -u /tmp/recovery-config.tar.gz 2>/dev/null || rm -f /tmp/recovery-config.tar.gz
   ```
7. **恢复 required/optional config/密钥材料到正确位置，并设置正确权限**（见 §8.6 权限表）：
   ```bash
   cp /tmp/wecom_archive-recovery-config-<TS>/backend/.env \
     /srv/apps/wecom-archive-365/current/backend/.env
   cp -a /tmp/wecom_archive-recovery-config-<TS>/shared/keys/. \
     /srv/apps/wecom-archive-365/shared/keys/
   # shared/private_keys、shared/backup.env、shared/certs、shared/deploy_state
   # 同理，仅在 bundle 中实际存在时才需要恢复
   chown -R wecomarchive:wecomarchive /srv/apps/wecom-archive-365/shared /srv/apps/wecom-archive-365/current/backend/.env
   rm -rf /tmp/wecom_archive-recovery-config-<TS>
   ```
8. **恢复 PostgreSQL**：解密 §5.6 取回的 DB dump，`pg_restore` 到新建的生产库（不是先恢复到临时库——新主机上没有"生产库"这回事，直接建目标库）。
9. **恢复/重新挂接媒体**：本地历史媒体走 §5.6 同样的 `tar xz` 流程；七牛 Kodo 媒体本来就在七牛的持久化对象存储里，不需要恢复，只需确认新主机的七牛凭据（已随 `backend/.env` 一起恢复）可用。
10. **按需重签发外部依赖**：TLS 证书如果 bundle 里没有 `shared/certs/`（或已过期），走 acme.sh 正常签发流程；DNS 指向新主机 IP。
11. **启动应用**：按 `docs/DEPLOYMENT.md` §4-6 走 systemd 安装/`deploy_server.sh` 首次部署流程。
12. **跑健康检查/冒烟测试**：`/health`、`/health/ready`，以及一次真实的登录+归档查询链路，确认 `FIELD_ENCRYPTION_KEY` 恢复正确（能成功解密至少一个租户的 `key_versions` 记录）。

### 8.6 恢复后的权限/所有者（不新造一套，复用现状已验证的约定）

| 文件/目录 | 所有者 | 权限 | 依据 |
|---|---|---|---|
| `backend/.env` | `wecomarchive:wecomarchive` | `600` | 与 `shared/backup.env` 同级机密文件的现状约定（§5.2） |
| `shared/keys/`、`shared/private_keys/`（及内部 `.pem`） | `wecomarchive:wecomarchive` | 目录 `700`，文件 `600` | 私钥材料，不应对 group/other 可读——比 `shared/certs/` 更严格，因为这里没有 nginx 之类的第二个读取者需要照顾 |
| `shared/backup.env` | `wecomarchive:wecomarchive` | `600` | 现状文档约定（§5.2），原样恢复 |
| `shared/certs/` | `wecomarchive:wecomarchive` | 目录 `750`，文件 `640` | 沿用 `docs/ops/rnd-261-domain-cutover-runbook.md` 已验证过的约定（nginx master 以 root 身份仍可读） |
| `shared/deploy_state/` | `wecomarchive:wecomarchive` | 目录默认 `755`，文件 `644` | 非机密内容（只是一个 SHA），沿用 `deploy_server.sh` 现有写入行为 |

**恢复后绝不能是 world-readable。** 任何一步 `cp`/`tar xzf` 之后，如果最终权限比上表更宽，视为恢复流程本身的缺陷，需要在下一次恢复演练中修正。

### 8.7 Ops 交接清单（本次 repo 改动不包含，需要 Ops 后续完成）

- [ ] 腾讯云新加坡主机上配置 `dr_pull.sh` 的调度（cron/systemd timer）与 `PULL_SOURCE`/`PULL_DEST_DIR`
- [ ] 本地 Mac + 外接盘上同样配置 `dr_pull.sh` 的调度
- [ ] 首次真实异地拷贝：确认两个目的地都能成功拉到至少一份 `recovery-config` bundle 并通过校验
- [ ] 两个目的地各自的保留策略（`BUNDLE_RETENTION_DAYS` 是否启用、保留多久）——本文档不代 Ops 做这个决定
- [ ] `shared/keys/`、`shared/private_keys/`（如存在）、`shared/certs/` 的实际内容需要 Ops 在生产上核实一遍——本次改动基于文档/代码交叉验证定义了 inventory，未登录生产逐项核对目录实际内容
- [ ] 一次完整的异地恢复演练（比照 §6 对 DB/媒体做过的那次，这次针对 recovery-config bundle：真实解密 → 真实恢复到一台干净主机 → 真实启动应用）
- [ ] 告警投递：`dr_config_bundle.sh`/`dr_pull.sh` 失败时的 `notify.sh`/`ALERT_WEBHOOK_URL` 投递路径需要 Ops 验证真的能送达（本次改动只保证失败时调用了 `notify.sh` 并且非零退出，不保证 webhook 已配置——见 §3.3 同样的已知缺口）

## 9. GitHub SSH alias 与 clone 属主约定（GH-177）

> 本节记录 2026-10-07 收尾核查及 GH-177 的只读配置核对结果。只记录已核实事实；本节不授权修改 SSH、systemd 或环境文件配置。SSH 配置行号是核对时的位置，后续编辑可能改变行号。

### 9.1 Git remote 与 SSH host alias

生产和非生产 clone 的 `origin` 均为：

```text
git@github.com-wecom-archive:zuohaisu/wecom-archive.git
```

`github.com-wecom-archive` 是各部署用户 `~/.ssh/config` 中的 Host alias，不是应替换为裸 `github.com` 的普通主机名。使用该 alias 才会应用对应部署用户的 SSH 身份配置；不要把 remote 改成 `git@github.com:...`，否则会绕过此 alias 配置。已核对的 stanza 如下（仅记录路径与 SSH 参数，不含私钥内容）：

生产用户 `wecomarchive`，`/home/wecomarchive/.ssh/config`，核对时第 6–10 行：

```sshconfig
Host github.com-wecom-archive
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_repo_deploy
  IdentitiesOnly yes
```

非生产用户 `wecomarchive-nonprod`，`/home/wecomarchive-nonprod/.ssh/config`，核对时第 1–6 行：

```sshconfig
Host github.com-wecom-archive
  HostName github.com
  User git
  IdentityFile /home/wecomarchive-nonprod/.ssh/github_repo_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

非生产 stanza 的 `StrictHostKeyChecking accept-new` 是核对时实际存在的第 6 行，未包含在 GH-177 原先记录的第 1–5 行范围内；此处补记现状，不代表本次变更或建议修改该设置。生产和非生产的 `IdentityFile` 原始写法不同，应按各自用户的配置原样保留。

### 9.2 按 clone 属主执行 Git 操作

- 生产 clone 位于 `/srv/apps/wecom-archive-365/current`，对应部署用户为 `wecomarchive`。
- 非生产 clone 位于 `/srv/apps/wecom-archive-365-nonprod/current`，属主为 `wecomarchive-nonprod`，目录权限为 `750`。核查时以 `wecomarchive` 身份对该目录执行 Git 操作返回 `rc=128 Permission denied`；操作非生产 clone 时应使用 `wecomarchive-nonprod` 身份，不要复用生产用户。

### 9.3 systemd 实际读取的 EnvironmentFile

| 环境 | 配置来源 | 实际 EnvironmentFile |
|---|---|---|
| 生产 | `/etc/systemd/system/wecom-archive-365.service.d/env.conf` drop-in | 生产 clone 内的 `backend/.env`（`/srv/apps/wecom-archive-365/current/backend/.env`） |
| 非生产 | `wecom-archive-365-nonprod.service` | `/etc/wecom-archive-365/nonprod.env` |

两套单元读取的环境文件位置不同；排查或变更时不要假设它们共用同一个文件。本 GH-177 文档更新未修改任何 unit、drop-in 或环境文件。

### 9.4 旧仓库地址残留扫描与 fetch 核对记录

2026-10-07 的收尾核查记录：

- 在 2 个 systemd unit、drop-in 与关联 EnvironmentFile、`root`/`wecomarchive`/`wecomarchive-nonprod` 三个 crontab、`/etc/sudoers.d/` 的 10 个文件及经降噪后的 17 个备份/部署脚本中，扫描旧仓库名 `wecom-archive-365.git` 和旧 GitHub 地址模式 `github.com*:zuohaisu/wecom-archive-365`，命中均为 **0**。
- 生产 `backend/.env` 只以脱敏计数方式扫描，上述模式命中 **0**；未记录或公开其中的环境变量值。
- 非生产 clone 的 `git fetch origin main` 返回 `rc=0`；核查当时 `origin/main` 为 `53ec37f`，与生产 clone 和 GitHub `main` 一致。该 SHA 是当日核查快照，不代表当前 `main`。

以上是有日期的核查记录，不代替未来迁移或改名后的新一轮残留扫描。
