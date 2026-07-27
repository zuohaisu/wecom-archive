# RND-193 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-193）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。
> 父任务编排见 `rnd-160-execution-plan.md` / `rnd-160-execution-prompt.md`；验收见 `rnd-193-qa-prompt.md`。
> 注意：本单大量改动在**服务器侧、不在仓库**（journald / logrotate / postgres / 备份脚本），需经用户在生产机上落地；本 brief 的仓库侧交付是「可版本化的配置 + 脚本 + 文档」。

---

## 一、任务（一句话）

建立日志、磁盘使用与备份增长治理机制，使 2 核 2GB 服务器的容量变化**可观察、可预警、可清理、可恢复**，不删生产归档消息/媒体（除非明确批准且有可验证备份）。

---

## 二、现状与落点（已读源码 + 仓库扫描）

### 日志面
- 应用日志：后端各模块用 `logging.getLogger(__name__)`（`backend/app/main.py:19` 等），**仓库内未发现 `FileHandler`/`RotatingFileHandler`/`TimedRotatingFileHandler`**（`grep` 确认）→ 极可能走 stdout 由 systemd 捕获进 **journald**。解密访问日志过滤器 `main.py:46`（不泄露 OAuth code）。
- 服务器侧还有：Nginx 访问/错误日志、PostgreSQL 日志、systemd journal —— 均不在仓库。
- **缺口**：无 `logrotate`/journald `SystemMaxUse` 上限治理（磁盘耗尽风险，RND-190 瓶颈之一）。

### 磁盘面
- 媒体根：`backend/app/settings.py:84` `storage_local_path`（默认空，生产在 `.env` 设）；本地 provider 读它（`backend/app/media_storage.py:344`）。媒体（图片/语音/视频/文件/缩略图）是主要增长源。
- 其它增长源：PostgreSQL 数据目录、journald 仓库、应用 stdout 日志、备份目录（见下）、`shared/run/` 锁文件。
- **缺口**：无磁盘/inode 阈值与告警；临时文件/失败下载 `.part` 残留/过期测试对象无定期清理。

### 备份面
- **仓库内无任何备份脚本**（`grep backup/pg_dump` 仅命中测试文件）→ 备份目前完全在服务器侧手工/未版本化。
- **缺口**：备份范围/频率/保留/恢复演练未文档化；单机本地备份不被当作完整灾备（issue 非目标明确）。

---

## 三、阶段一：复现 + 测量（RED，必须可追溯到 RND-190）

1. 在生产机统计（命令写入交付说明，便于复现）：
   - 各目录占用与**日增速**：应用日志、journal(`/var/log/journal`)、PostgreSQL 数据、媒体根(`storage_local_path`)、备份目录、nginx 日志。
   - `df -i` 看 inode 使用率（小文件多的媒体/缩略图易吃 inode）。
   - 当前 journald `SystemMaxUse`、是否有 logrotate 覆盖应用/nginx/postgres。
2. 确认备份现状：是否存在定时 `pg_dump`？保留几天？媒体是否备份？有无**至少一次**恢复演练记录？
3. 把 RED 数字（各目录占用/增速、inode%、备份 RPO/RTO 现状）写进实现说明，标注对应 RND-190 哪条瓶颈。

---

## 四、阶段二：实现（GREEN，按风险排序，最小变更）

### 1. 日志轮转与上限（落服务器 + 仓库留痕）
- **journald**：在服务器 `/etc/systemd/journald.conf` 设 `SystemMaxUse=`（如 500M–1G，按 2c2g 取值）、`SystemKeepFree=`、`MaxRetentionSec=`；`systemctl restart systemd-journald`。**先备份原 conf**。
- **logrotate**：为 Nginx / PostgreSQL / 任何文件日志新增或核对 `/etc/logrotate.d/wecom-archive`（按大小+保留周期轮转、压缩、`copytruncate` 或 `postrotate` 重载）。**先备份原配置**。
- 把生产采用的 journald/logrotate 片段**收入仓库文档**（`docs/operations/2c2g-runbook.md`，见 RND-160 计划），便于版本化与复现。应用本身若确需落盘日志，再加 `RotatingFileHandler`（带 `maxBytes`/`backupCount`），但优先用 journald，避免双写。

### 2. 磁盘 / inode 阈值与告警（脚本 + 文档）
- 新增 `scripts/disk_usage_check.sh`（或 `.py`）：检查磁盘 %、inode %、内存 %、swap、PG 连接数；超阈值打印 `[WARN]` 并可接通知（复用现有告警通道；**不**硬编码 webhook，读环境变量）。
- 注册为 systemd timer（如每 15–30 分钟）或 cron；保留「安全清理命令」与「不可清理数据边界」清单（归档消息/媒体/私钥/.env 绝不清理）。

### 3. 备份与恢复（仓库脚本 + 文档 + 演练）
- 新增 `scripts/backup_once.sh`（或 `.py`）：`pg_dump` 加密（参照 `rnd-160` 计划里提到的加密思路，但**本仓库示例脚本里的 webhook/密钥必须用环境变量，禁止硬编码**）+ 媒体目录打包/同步（rsync 到第二存储或对象存储）。
- 明确**范围/频率/保留**：DB 每日 + 保留 N 天；媒体按变更增量；保留策略写入文档。
- **至少一次受控恢复演练**（测试库 restore + 抽样校验），把步骤写入 `docs/operations/2c2g-runbook.md`；若当前无法演练，须**明确写出缺口**而非假装完成。

### 4. 临时文件 / 残留清理（顺手）
- 确认媒体下载 `.part` 失败即删（已有）；检查 `shared/run/` 锁文件无长期残留；过期测试对象清理命令写入文档（不自动删生产数据）。

约束：
- ❌ 不定义客户数据删除 / 合规留存政策（超出范围）。
- ❌ 不删生产归档消息或媒体（除非明确批准 + 可验证备份）。
- ❌ 不把单机本地备份当完整灾备（文档中明示）。
- ✅ 所有服务器侧配置改动有「保存前副本 + 回滚命令」。
- ✅ 不泄露密钥/聊天内容/完整签名 URL（备份加密、日志脱敏）。

---

## 五、阶段三：验证（GREEN + 回归 + 演练）

1. **功能验证**：
   - logrotate/journald 配置 `dry-run` 或观察一轮轮转生效、磁盘不再无界增长。
   - `disk_usage_check` 在超阈值时正确告警、在正常时静默。
   - `backup_once` 产出可恢复产物；**恢复演练**成功（测试库 restore 后抽样校验行数/媒体可访问）。
2. **回归**（仓库侧脚本须有测试）：
   - 若有 `scripts/disk_usage_check` / `backup_once` 逻辑，补单测或 `.bats`（参照 `scripts/tests/deploy_server.bats` 风格）。
   - `make verify` 不得因本单新增脚本而红（新增脚本本身不参与后端测试则忽略，但不得破坏既有）。
3. 若演练发现无法恢复 → 标为「明确缺口」写入文档与交付说明，不掩盖。

---

## 六、硬约束（违反即判失败）

- ❌ 不删生产归档消息/媒体（无明确批准 + 可验证备份）。
- ❌ 不把单机本地备份当完整灾备（文档明示）。
- ❌ 硬编码密钥/webhook/签名 URL 进仓库脚本。
- ❌ 不执行 git commit / push。
- ✅ 服务器侧配置改动三件套（备份前副本 + 回滚命令 + 验证命令）；备份加密 + 日志脱敏；至少一次受控恢复演练或明确缺口。

---

## 七、收尾（交付物）

向用户交付：
1. RED 基线数字（各目录占用/增速、inode%、备份 RPO/RTO 现状）+ 对应 RND-190 瓶颈条目。
2. GREEN 数字（同上）+ 前后对比。
3. 仓库侧交付（预期）：`scripts/disk_usage_check.sh`(或.py)、`scripts/backup_once.sh`(或.py)、`docs/operations/2c2g-runbook.md`（日志/journald/logrotate 片段、备份范围/频率/保留/恢复步骤、磁盘/inode 阈值与告警路径、清理边界清单）。
4. 服务器侧配置改动记录：journald/logrotate 片段 + 各自「原配置备份路径 + 回滚命令」。
5. 恢复演练报告（成功步骤 / 或明确缺口）。
6. 注明：未提交，待用户 Review 后自行 commit；服务器侧改动由用户在生产机落地。
