# RND-192 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「Codex 验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收对象：开发 agent 按 `rnd-192-execution-prompt.md` 产出的改动。

---

## 一、验收目标

确认 RND-192「Worker 和媒体下载资源优化」达成：后台任务在 2c2g 峰值可控、错峰无叠加尖峰、前台查阅不受影响，**零回归**（同步/解密/媒体语义、锁/幂等/重试/可观测不变），且每项优化可追溯到 RND-190 基线。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 调度错峰（最高杠杆）
- [ ] **S1** worker 与媒体下载 timer 不再同时触发（如媒体改为错峰 `OnCalendar` 或更长 `OnUnitActiveSec`）。
  - 证据：读 `deploy/systemd/wecom-archive-worker.timer` + `wecom-archive-media-download.timer`；`systemctl show` 确认下次触发时刻错开。
- [ ] **S2** **叠加峰值**显著低于 RED（手动并发触发两任务，记录 `top`/RSS 峰值对比）。
  - 证据：前后数字；附命令与采样。
- [ ] **S3** timer 改动有「保存前副本 + 回滚命令 + 验证命令」三件套；回滚可还原且 daemon-reload 后行为正确。

### 批量 / 限流
- [ ] **B1** Sync 单跑 CPU/RSS 峰值低于 RED；`WECOM_CHAT_LIMIT` 调整有据（非盲目）。
- [ ] **B2** 媒体下载单跑峰值低于 RED；`--limit`/`--since-hours` 调整有据；仍覆盖近期 72h 优先。
- [ ] **B3** 缩略图回填保持小批（`--limit 50`/`-batch-size 25`）顺序逐 row commit，资源可控。

### 前台可用性
- [ ] **F1** 后台任务运行期间，前台 API（员工/会话/时间线/搜索）仍可用；`/health/ready`（127.0.0.1:8035）始终通过。
  - 证据：后台跑批中抽样列表/时间线接口 + 探活。

### 吞吐与数据完整性
- [ ] **T1** 任务吞吐（条/分钟）无显著退化（对比 RED）。
- [ ] **T2** 失败重试次数无异常上升；无「跳过失败记录 / 降低数据完整性」行为。

### 安全机制保留
- [ ] **X1** 文件锁保留：同脚本重叠运行安全 no-op（`run_archive_worker_once.py`/`download_wecom_media_once.py`/`backfill_thumbnails_once.py` 锁逻辑未删）。
- [ ] **X2** 解密子进程隔离保留（`app/services/decrypt_isolation.py` 15s 超时机制未去）。
- [ ] **X3** 媒体 `.part` 临时文件 + 失败即删 + 原子发布保留；无大文件整读进内存（若已流式则标注合规）。
- [ ] **X4** 幂等/可重试/可观测保留（同一 `(tenant_id, msgid)` 不重复；媒体 `get_or_reset_media_file` 不跨消息复用行）。

### 全局
- [ ] **M0** `make verify` **全绿**。
- [ ] **N0** 未引入重型队列系统（除非有测量依据且单独批准——本单默认不引入）。

---

## 三、回归套件（必须全绿）

```
make verify
```
重点确认（任一失败即 FAIL，附失败栈）：
- `test_revoke_concurrency.py`（worker 锁/幂等/并发）
- `test_generic_media_serving.py` / 媒体下载相关测试
- `test_rnd207_thumbnail_backfill.py`（回填幂等/可重试/可中断）
- 任何涉及 sync/decrypt 的集成测试

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码有 Bug** → 反馈给开发 agent 修复，附具体错误 + 失败测试名 + 期望行为。**不自行改实现**。
- **测试代码有 Bug** → 仅当断言旧行为且获用户确认时可自行修正（须报告标注）。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 数字对比 + 每项对应 RND-190 瓶颈条目。

最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮回归验证；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-192 验收结论：PASS / FAIL
RED 基线：worker 峰值 CPU __% / RSS __MB；媒体峰值 __%；叠加峰值 __%
GREEN：    worker 峰值 CPU __% / RSS __MB；媒体峰值 __%；叠加峰值 __%
调度：timer 错峰 ___（原 ___）；WECOM_CHAT_LIMIT ___；媒体 --limit ___ / --since-hours ___
前台：后台跑批中 /health/ready + 列表/时间线可用
数据完整性：失败重试无异常；幂等/锁/隔离/.part 保留
回归：make verify ___（绿/红，附失败项）
遗留：___（若有）
```
