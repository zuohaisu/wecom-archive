# RND-192 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-192）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。
> 父任务编排见 `rnd-160-execution-plan.md` / `rnd-160-execution-prompt.md`；验收见 `rnd-192-qa-prompt.md`。

---

## 一、任务（一句话）

基于 RND-190 基线，优化 **Worker（归档同步 + 解密）** 与 **媒体下载** 任务的批量、并发、调度与重试，使后台任务在 2 核 2GB 下稳定运行、峰值可控、不显著挤压前台查阅，**不改变同步/解密/媒体语义，不降低数据完整性**。

---

## 二、现状与落点（已读源码，附文件:行号）

### 调度面（最关键的 2c2g 杠杆）
- `deploy/systemd/wecom-archive-worker.timer:5` → `OnCalendar=*:0/5`（**每 5 分钟**跑 worker）。
- `deploy/systemd/wecom-archive-media-download.timer:5-6` → `OnBootSec=5min` + `OnUnitActiveSec=5min`（**每 5 分钟**跑媒体下载）。
- 两者**频率相同、锁不同**（worker 锁 `wecom-archive-worker.lock`；媒体锁 `wecom-media-download.lock`）→ 两个高成本任务可能**同时触发**，在 2c2g 上叠加 CPU/内存峰值。这是首要治理点。

### 批量 / 限流旋钮
- **Sync**：`scripts/sync_wecom_archive_once.py:134` 读 `WECOM_CHAT_LIMIT`（默认 500，单次 `GetChatData` 拉取上限），核心循环在 `app/services/sync_worker.py`（`:182`/`:186` 分批 `commit`）。
- **Decrypt**：`scripts/decrypt_wecom_messages_once.py` 经 `app/services/decrypt_worker.py`；解密在**子进程隔离**中跑（`app/services/decrypt_isolation.py:43` 默认超时 15s），崩溃/超时安全回收——**保持该隔离，不要去掉**。
- **媒体下载**：`scripts/download_wecom_media_once.py` 当前 service 传 `--since-hours 72 --newest-first --limit 20`（`deploy/systemd/wecom-archive-media-download.service:11`）；核心在 `app/services/media_worker.py`（`:103`/`:114` 每候选 `commit`；`:142`/`:166`/`:184` 逐条下载+超时）。
- **缩略图回填**：`scripts/backfill_thumbnails_once.py` 当前 service `--limit 50`（`.service` 注释），脚本默认 `--limit 100`/`-batch-size 25`（`:107-108`）；已严格顺序、每 row commit、`.part` 临时文件——**资源控制已较稳**，主要调 `--limit` 节奏。

### 安全机制（务必保留）
- 三类任务均有**非阻塞 fcntl 文件锁**：同脚本重叠运行安全 no-op（`run_archive_worker_once.py:54-81`、`download_wecom_media_once.py:145-160`、`backfill_thumbnails_once.py:119-137`）。
- 媒体下载写 `.part` 临时文件、失败即删（`download_wecom_media_once.py` 模块 docstring + `WECOM_MEDIA_TIMEOUT` 默认 30s）。
- 幂等/可重试：`run_archive_worker_once.py:14` 标明按 `(tenant_id, msgid)` 幂等；媒体 `get_or_reset_media_file` 不跨消息复用行。

---

## 三、阶段一：复现 + 测量（RED，必须可追溯到 RND-190）

1. 在 2c2g 生产（或等价压测机）按基线场景跑：
   - **单独**跑一次 worker（`systemctl start wecom-archive-worker`）与一次媒体下载（`systemctl start wecom-archive-media-download`），用 `top`/`ps_mem`/`journalctl` 记录 CPU 峰值、RSS 峰值、运行时长、处理条数。
   - **叠加**跑（同时触发两个 timer 或手动并发）记录叠加峰值——这是 RND-190 最可能的瓶颈项。
2. 抓 `WECOM_CHAT_LIMIT`、媒体 `--limit`/`--since-hours` 当前值，记录单次任务吞吐（条/分钟）与失败重试次数。
3. 把 RED 数字（叠加峰值、单跑峰值、吞吐）写进实现说明，标注对应 RND-190 哪条瓶颈。

---

## 四、阶段二：实现（GREEN，按杠杆排序，最小变更）

### 1. 错峰调度（最高杠杆，改 systemd，非代码）
- 让 worker 与媒体下载**不再同时触发**：例如媒体下载改为 `OnCalendar=*:2/5`（每 5 分钟、偏移 2 分），或降低媒体频率（如 `OnUnitActiveSec=10min`）。
- 任何 `.timer` 改动**先 `cp` 原文件到 `shared/rollback/` 备份**，改后 `systemctl daemon-reload` + 手动触发验证，回滚 = 还原 + reload。
- 不得删除 `AccuracySec`/`Persistent` 等既有安全属性（媒体 timer 的 `AccuracySec=30s` 有助于抖动错峰，可保留或调大）。

### 2. 批量/限流旋钮（改 service ExecStart 参数或 env）
- **Sync** 峰值过高 → 在不丢数据前提下调小 `WECOM_CHAT_LIMIT`（如 200–300），或缩短 worker timer 间隔让单次更轻（权衡：更频繁但每次更短）。
- **媒体下载** 峰值过高 → 调小 `--limit`（如 10–15）或缩短 `--since-hours`（如 48）；保持 `--newest-first` 优先近期。
- **缩略图回填** 手动任务：保持 `--limit 50` 小批；文档注明「CPU 高时再调小」。

### 3. 重试 / 退避 / 残留（代码，仅当需要）
- 媒体失败重试：确认 `download_media_candidates`（`app/services/media_worker.py`）已有失败态与重试入口（`--retry`）；若观察到失败行堆积，评估**指数退避**或**限流重试**，但不得跳过失败记录、不得降低数据完整性。
- 临时文件/失败残留：确认 `.part` 失败即删；检查媒体目录 / `shared/run/` 锁文件无长期残留（RND-193 会统一治理，但此处顺手确认）。

### 4. 内存/网络读取方式
- 确认媒体下载是**流式写盘**（已 `.part` 后原子发布），不在内存攒全文件；若发现大文件整读进内存，改为流式。此项若已满足，标注「已合规」，不改。

约束：
- ❌ 不引入重型队列系统（issue 非目标，除非有测量依据且单独批准）。
- ❌ 不改同步/解密/媒体内容语义；不以跳过失败记录或减少数据完整性换资源。
- ✅ 保留文件锁、子进程隔离（decrypt）、`.part` 安全发布、幂等/可重试/可观测。

---

## 五、阶段三：验证（GREEN + 回归 + make verify）

1. **功能验证**：回阶段一场景——
   - 单跑 worker / 媒体下载的 CPU/RSS 峰值、**叠加峰值**应显著低于 RED。
   - 前台 API（员工/会话/时间线/搜索）在后台任务运行期间仍可用（手动或脚本探活 `/health/ready` + 抽样列表接口）。
   - 任务吞吐（条/分钟）无明显退化。
2. **回归**（必须全绿）：
   - `make verify`
   - 重点：`test_revoke_concurrency.py`（worker 锁/幂等/并发）、媒体下载相关测试、`test_rnd207_thumbnail_backfill.py`（回填幂等/可重试）。
   - 确认 worker 锁、幂等、失败状态、重试行为回归通过。
3. 若测试失败，**先修实现**，不擅自改测试（除非断言旧行为且获用户确认）。

---

## 六、硬约束（违反即判失败）

- ❌ 不引入重型队列（无测量依据+批准不动）。
- ❌ 不改同步/解密/媒体语义；不跳过失败记录 / 降数据完整性。
- ❌ 破坏文件锁 / 子进程隔离 / `.part` 安全发布 / 幂等 / 可重试 / 可观测。
- ❌ 不执行 git commit / push。
- ✅ 任何 systemd 单元/定时器改动有「保存前副本 + 回滚命令 + 验证命令」三件套。

---

## 七、收尾（交付物）

向用户交付：
1. RED 基线数字（单跑峰值、叠加峰值、吞吐）+ 对应 RND-190 瓶颈条目。
2. GREEN 数字（同上）+ 前后对比。
3. 改动清单（预期：`deploy/systemd/wecom-archive-worker.timer`、`deploy/systemd/wecom-archive-media-download.timer`、可能 `wecom-archive-media-download.service`(ExecStart 参数) / `wecom-archive-worker.service`(env)、`backend/scripts/*.py` 与 `app/services/*_worker.py` 仅当调重试/流式时）。
4. systemd 改动的三件套回滚记录（原文件备份路径 + 回滚命令）。
5. `make verify` 通过日志。
6. 注明：未提交，待用户 Review 后自行 commit。
