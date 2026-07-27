# RND-193 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「Codex 验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收对象：开发 agent 按 `rnd-193-execution-prompt.md` 产出的交付（含服务器侧配置与仓库脚本/文档）。
> 注意：部分证据在**生产服务器**，需用户在服务器上执行验证命令后回填结果；你据此判定 PASS/FAIL。

---

## 一、验收目标

确认 RND-193「日志、磁盘与备份增长治理」达成：容量变化可观察/可预警/可清理/可恢复，阈值与告警清晰，**不泄露敏感信息**，且每项优化可追溯到 RND-190 基线。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 增长基线
- [ ] **G1** 有磁盘/日志增长基线：各目录（应用日志、journal、PostgreSQL、媒体、备份、nginx）占用与日增速，含 inode 使用率（RED 数字齐全）。
  - 证据：交付说明中的测量命令与输出。

### 日志轮转 / 保留
- [ ] **L1** journald 已设上限（`SystemMaxUse`/`SystemKeepFree`/`MaxRetentionSec`），且有**原 conf 备份 + 回滚命令**。
  - 证据：生产机 `grep -E "SystemMaxUse|MaxRetentionSec" /etc/systemd/journald.conf` + `systemctl show systemd-journald -p ...` 确认生效。
- [ ] **L2** Nginx / PostgreSQL / 文件日志有 logrotate 覆盖（按大小+保留周期+压缩），且有**原配置备份 + 回滚命令**。
  - 证据：`/etc/logrotate.d/wecom-archive` 存在且 `logrotate -d` 演练通过。
- [ ] **L3** 采用的 journald/logrotate 片段已收入 `docs/operations/2c2g-runbook.md`（版本化、可复现）。

### 磁盘 / inode 阈值与告警
- [ ] **D1** `scripts/disk_usage_check.*` 检查磁盘 %、inode %、内存 %、swap、PG 连接数；超阈值告警、正常静默。
  - 证据：读源码；手动以「注入高占用」或单测验证阈值分支。
- [ ] **D2** 该检查已注册为 timer/cron（每 15–30 分钟），且有验证记录。
  - 证据：`systemctl list-timers` 或 crontab 截图。
- [ ] **D3** 有「安全清理命令」与「不可清理数据边界」清单（归档消息/媒体/私钥/.env 绝不清理），写入文档或脚本注释。

### 备份 / 恢复
- [ ] **B1** `scripts/backup_once.*` 覆盖 DB（pg_dump）+ 媒体；范围/频率/保留明确且写入文档。
  - 证据：读脚本 + 文档；确认保留周期数值。
- [ ] **B2** 备份加密、日志脱敏，**无硬编码密钥/webhook/签名 URL**（全部读环境变量）。
  - 证据：`grep -nE "https?://|webhook|sk-|secret=" scripts/backup_once.*` 应无命中（或仅变量引用）。
- [ ] **B3** **至少一次受控恢复演练**（测试库 restore + 抽样校验），步骤写入 `docs/operations/2c2g-runbook.md`。
  - 证据：演练日志/截图；或文档中**明确写出无法演练的缺口**（不得假装完成）。
- [ ] **B4** 文档明示「单机本地备份 ≠ 完整灾备」（不误导）。

### 安全
- [ ] **X1** 诊断/备份过程未泄露密钥、聊天内容或完整签名 URL（RND-190/RND-193 口径）。
- [ ] **X2** 服务器侧配置改动均有「保存前副本 + 回滚命令 + 验证命令」三件套。

### 回归 / 全局
- [ ] **M0** 仓库新增脚本（若有单测/.bats）通过；`make verify` 不因本单变红。
- [ ] **R0** 未删生产归档消息/媒体（除非明确批准 + 可验证备份，且文档记录）。

---

## 三、回归 / 全局检查

- 若新增 `scripts/disk_usage_check.*` / `scripts/backup_once.*`：补单测或 `.bats`（参照 `scripts/tests/deploy_server.bats` 风格）并运行。
- `make verify` 绿（新增脚本不破坏既有后端测试）。
- 抽查 `docs/operations/2c2g-runbook.md` 与实际生产配置一致（L3/D3/B3 落地）。

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码/脚本有 Bug** → 反馈给开发 agent 修复，附具体错误。**不自行改实现**。
- **服务器侧验证缺证据** → 要求用户在生产机执行对应命令并回填输出；不得凭空判 PASS。
- **恢复演练无法完成** → 可接受「明确缺口」结论，但文档必须写明，不得伪装成功。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 数字对比 + 每项对应 RND-190 瓶颈条目。

最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮复核；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-193 验收结论：PASS / FAIL
RED 基线：journal ___MB/日增；媒体 ___GB/日增；inode ___%；备份 RPO/RTO ___
GREEN：    journal 已限容 ___；logrotate 覆盖 ___；disk_usage_check 已注册；备份 ___ + 演练 ___
配置回滚：journald/logrotate 均含三件套（备份+回滚+验证）
安全：无硬编码密钥/签名URL；不泄露聊天内容
约束：未删生产归档/媒体；文档明示本地备份≠灾备
遗留：___（含恢复演练缺口，若有）
```
