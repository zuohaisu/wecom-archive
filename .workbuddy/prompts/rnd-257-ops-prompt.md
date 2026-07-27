# RND-257 — 运维执行提示词（部署后一次性回填批处理）

> 适用对象：运维 / SRE agent。
> 目标：RND-257 合并并部署到生产后，在产线主机上**手动**跑一次性回填（历史 chatrecord/mixed 结构化内容重解析 + 内嵌媒体下载补全）。
> 关联：父 RND-243（解析层修复，已部署）、RND-200（嵌套媒体下载管线，复用）、RND-231（隔离解密，复用）、RND-208（SIGSEGV 根因）。
> 开发提示词：`.workbuddy/prompts/rnd-257-execution-prompt.md`；验收提示词：`.workbuddy/prompts/rnd-257-qa-prompt.md`。

---

## 0. 一句话结论

**部署 ≠ 回填。** CI/CD（`.github/workflows/deploy.yml` + `scripts/deploy_server.sh`）只把代码与迁移上到主机，**不会**自动跑回填脚本。部署完成后，必须由运维在产线主机手动执行一次性批处理：先重解析、再触发全量历史的媒体下载。规模很小（约 36 行，详见下），单机一次性跑即可。

---

## 1. 前置条件（开始操作前必查）

- [ ] RND-257 已 merge 到 `main`，且 `deploy.yml` 的部署任务已跑完（代码 + 迁移在主机生效）。
- [ ] 回填脚本已随部署落到主机：
  ```bash
  ls -l /srv/apps/wecom-archive-365/current/backend/scripts/reparse_structured_content_once.py
  ```
  （部署是 `git checkout` 到 `/srv/apps/wecom-archive-365/current`，`backend/scripts/` 一并同步，故该脚本应存在。）
- [ ] `.env` 含必需环境变量（隔离解密与 DB 连接都从这里读）：
  ```bash
  grep -E "WECOM_SDK_LIB_PATH|DATABASE_URL" /srv/apps/wecom-archive-365/current/backend/.env
  ```
  `WECOM_SDK_LIB_PATH` 是隔离解密子进程加载 `libWeWorkFinanceSdk_C.so` 的绝对路径，缺失会直接报错。

---

## 2. 影响操作方式的关键事实（务必先读）

- **产线路径约定**（来自 `deploy/systemd/wecom-archive-worker.service` 等）：
  - 应用目录：`/srv/apps/wecom-archive-365/current/backend`
  - Python：`/srv/apps/wecom-archive-365/current/backend/.venv/bin/python`
  - 运行用户：`wecomarchive`
  - 环境文件：`/srv/apps/wecom-archive-365/current/backend/.env`（systemd `EnvironmentFile=` 读取）
- **隔离解密读 `WECOM_SDK_LIB_PATH`**（来自 `.env`）：重解析会重跑隔离解密子进程，必须保证该变量已 `source`。
- **⚠️ 最大陷阱 — 下载默认时间窗**：
  - `wecom-archive-media-download.service` 实际命令是 `download_wecom_media_once.py --since-hours 72 --newest-first --limit 20`（见 `deploy/systemd/wecom-archive-media-download.service:11`）。
  - `build_nested_media_candidate_query`（`backend/app/media_download.py:363`）按 `ArchiveMessage.msgtime >= since_ms` 过滤；`--since-hours` 缺省为 `None` 时 `since_ms=None` → **全量历史**，但 systemd timer 硬编码了 `72`，所以**定时任务只扫最近 72 小时**。
  - 我们要回填的是**数月前的历史聊天记录**，其重解析后的 `media_refs` **不会被默认 timer 覆盖**。因此回填的下载步骤必须**显式不带 `--since-hours`（全量历史）**地跑，不能只靠 `systemctl start wecom-archive-media-download`。

---

## 3. 操作步骤

> 所有命令在产线主机执行。建议用 `wecomarchive` 用户运行（与 systemd 单元一致），不要用 root 直接写库。

### Step 0（推荐但可选）— 先备份 DB
仅约 36 行改动、风险低，但稳妥起见可先备份：
```bash
cd /srv/apps/wecom-archive-365/current/backend
sudo -u wecomarchive bash scripts/backup_once.sh
```

### Step 1 — 重解析（先 dry-run，再真实跑）
```bash
cd /srv/apps/wecom-archive-365/current/backend
set -a; source .env; set +a        # 载入 DATABASE_URL / WECOM_SDK_LIB_PATH 等

# 1a. 预览（不写库）：应报告约 36 个候选行、无异常报错
sudo -u wecomarchive .venv/bin/python scripts/reparse_structured_content_once.py --dry-run

# 1b. 真实重解析（按 id 升序、幂等、可重跑）
sudo -u wecomarchive .venv/bin/python scripts/reparse_structured_content_once.py --limit 1000
#   多租户时按租户分批：追加 --tenant <TENANT_ID>
```
- 观察日志：`reparsed` 计数应≈36；`SIGSEGV_SENTINEL` 计数（RND-231 隔离折叠单条 SIGSEGV 并跳过该行、继续整批，预期=0 或极小，不致命）。
- 该步骤**只写 `structured_content` 单列**，不碰 `decrypted_payload`/`decrypt_status`/`media_files`/收件人/加密信封。

### Step 2 — 媒体下载补全（⚠️ 全量历史，勿带 --since-hours）
```bash
cd /srv/apps/wecom-archive-365/current/backend
set -a; source .env; set +a

# 2a. 先只看候选数（确认历史嵌套 media_refs 被纳入）
sudo -u wecomarchive .venv/bin/python scripts/download_wecom_media_once.py --count-only

# 2b. 真实下载（不带 --since-hours → since_ms=None → 全量历史扫描）
sudo -u wecomarchive .venv/bin/python scripts/download_wecom_media_once.py --limit 1000
```
- **不要**加 `--since-hours`（否则只扫最近 72h，漏掉历史行）。
- 观察日志：`sdk_error` 计数（旧媒体令牌过期 → 字节不可恢复，但 structured_content 已正确，前端显示规整节点 + 占位符 + 原始 md5sum/filesize 元信息）；`download_status=failed` 行；成功落 `MediaFile` 计数。
- 若 RND-257 开发脚本的 `--download` 开关已自动串接此步，可省略手动 2a/2b；但手动显式跑更可控（可先 `--count-only` 预估）。

### Step 3 — 保留 media-download timer
不要停 `wecom-archive-media-download.timer`（每 5 分钟），它继续正常处理**新**消息的媒体，与本次回填互不干扰、无害。

---

## 4. 监控 / 日志关键字

| 阶段 | 关注关键字 | 预期 / 处理 |
|------|-----------|------------|
| 重解析 | `reparsed` 计数 | ≈36 |
| 重解析 | `SIGSEGV_SENTINEL` | 单条崩溃被隔离跳过，不中断整批；计数应≈0 或极小 |
| 下载 | `sdk_error` | 旧令牌过期导致；媒体字节不可恢复（WeCom 限制），非缺陷 |
| 下载 | `download_status=failed` | 与 `sdk_error` 同源；记录比例供汇报 |
| 下载 | 成功落 `MediaFile` 计数 | 未过期 sdkfileid 的媒体字节已补全 |

---

## 5. 验收 / 验证（运维侧自检）

- [ ] 重解析候选数 ≈36（与开发 agent 生产只读核实一致：chatrecord 34 success 中 36 行需修、mixed 3 行全 NULL）。
- [ ] 抽样某历史 `chatrecord`：`structured_content` 含 `media_refs[{type: image/voice/video/file/emotion, sdkfileid: ...}]`，**无** `text=原始JSON` 坏形态。
- [ ] **`decrypted_payload` 仍为 NULL**（SF-1 未被破坏——回填严禁填充它）。
- [ ] `decrypt_status` 仍为 `success`；`media_files`、收件人、加密信封列均未改动。
- [ ] 未过期 `sdkfileid` 的 `MediaFile` 行已创建（占位符 → 可看媒体）。
- [ ] 前端抽检：打开受影响会话，聚合消息显示**媒体节点**（非 raw-JSON 文本），时间戳**非 1970**。
- [ ] 全库其它 `msgtype` 行为零回归。

---

## 6. 安全边界（严禁）

- 不得填充 `decrypted_payload`（SF-1 数据最小化，刻意不持久化全量解密包）。
- 不得改动 `decrypt_status` / `media_files` / 收件人 / 加密信封（`raw_encrypted_payload`/`encrypt_random_key`/`encrypt_chat_msg`）。
- 只在 `chatrecord` / `mixed` 上操作，其它 `msgtype` 不在范围。
- 脚本幂等：可安全重跑（重跑不会重复破坏，因源是加密信封、每次重解密重建）。

---

## 7. 已知限制（如实汇报，非缺陷）

- **WeCom 媒体下载令牌会过期**：极旧历史消息的 `sdkfileid` 可能已失效，下载返回 `sdk_error`、字节无法恢复（WeCom API 限制）。验收应确认：即便如此，前端也从「raw-JSON 垃圾」升级为「规整媒体节点 + 占位符 + 原始 md5sum/filesize 元信息」。
- **SIGSEGV / 隔离（RND-208 / RND-231）**：重解析重跑隔离解密子进程；单条 SIGSEGV 折叠为 `SIGSEGV_SENTINEL` 并跳过该行、继续整批。该崩溃与 msgtype/输入内容无关（RND-208 §5），重解密这几十行不比重解密其它历史行更危险。

---

## 8. 若开发交付了 systemd unit（备选执行方式）

若 RND-257 开发同时交付了 `deploy/systemd/wecom-archive-reparse-chatrecord.service`（oneshot），重解析可改为：
```bash
sudo systemctl start wecom-archive-reparse-chatrecord
journalctl -u wecom-archive-reparse-chatrecord -f
```
**但下载步骤仍须按 §3 Step 2 手动显式跑（全量历史，不带 `--since-hours`）**——默认 timer 的 72h 窗口不会覆盖历史行。

---

## 9. 排错速查

- `WECOM_SDK_LIB_PATH ... not found` / `_require_env` 报错 → `.env` 未 `source`，先 `set -a; source .env; set +a`。
- 候选数 =0 → 可能已全 reparse 过（幂等），或 `--tenant` 写错；
- `Permission denied` → 用 `sudo -u wecomarchive` 跑，勿用 root 直写库。
- 下载候选远少于预期 → 确认**没带** `--since-hours`（带了就只扫 72h，漏历史）。
