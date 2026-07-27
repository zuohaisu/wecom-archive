# 2c2g 生产运维手册 — 日志 / 磁盘 / 备份治理 (RND-193)

> 范围：2 核 2GB 阿里云 ECS 生产服务器（`ssh ali-xy-qw`，见项目 SSH 访问记录）上的日志轮转、
> 磁盘/inode 容量监控、数据库与媒体备份/恢复。对应 Linear RND-193（父任务 RND-160）。
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
