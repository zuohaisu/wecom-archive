# RND-233 执行提示词（域名去标识化 · 读环境变量策略）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。策略已定：**部署脚本从环境变量读域名；`.env.example` 用占位符；文档/测试用 `example.com`**。
> 不自行 commit / push。关联 epic：RND-232。

## 0. 任务与来源
- Linear：RND-233「去标识化真实域名 crowntime.cn（代码/测试/文档/脚本）」，父 epic RND-232。
- 策略（Haisu 拍板）：**改为读环境变量**。不要全文硬编码替换成 `example.com`（会让线上部署失效）。
- 注（来自全仓库泄露审计）：仓库含至少 3 个不同主机名 / 标识：`media.crowntime.cn`、`qwhhcd.crowntime.cn`、`admin@crowntime.cn`（邮箱）。脱敏映射建议：`media.crowntime.cn → media.example.com`、`qwhhcd.crowntime.cn → archive.example.com`、`admin@crowntime.cn → admin@example.com`。部署脚本侧按各真实主机名改为可读环境变量（如 `MEDIA_DOMAIN` / `ARCHIVE_DOMAIN` / `ADMIN_EMAIL`），`.env.example` 用对应 `example.com` 占位符；文档 / 测试侧直接用 `example.com` 等价物。

## 1. 受影响文件清单（共 30；执行前先 `grep -rln crowntime . --exclude-dir=.git --exclude-dir=.qoder` 复核）

### A 组 · 部署关键（真实线上域名，必须改为读环境变量）
- `scripts/deploy_server.sh`
- `Makefile`
- `ssl-renew/notify.sh`
- `ssl-renew/lib/common.sh`
- `ssl-renew/examples/domain.env.example`
- `ssl-renew/README.md`
- `ssl-renew/tests/08_idempotency_and_multi_domain.bats`
- `ssl-renew/tests/07_notify_webhook.bats`
- `ssl-renew/tests/11_qiniu_helper_argv_safety.bats`
- `ssl-renew/tests/05_qiniu_bind_and_verify.bats`
- `ssl-renew/tests/01_config_and_validation.bats`
- `ssl-renew/tests/04_qiniu_upload.bats`
- `ssl-renew/tests/03_staging.bats`
- `ssl-renew/tests/02_dry_run.bats`
- `ssl-renew/tests/test_qiniu_helper.py`

### B 组 · 文档（非部署，替换成 `example.com` 或 `${ARCHIVE_DOMAIN}` 说明）
- `docs/rnd-207-migration-runbook.md`
- `docs/API.md`
- `docs/ops/media_storage_ops.md`
- `docs/ARCHITECTURE.md`
- `docs/ssl-renewal/DEPLOYMENT_GUIDE.md`
- `docs/DEPLOYMENT.md`
- `docs/ai/current-status.md`
- `docs/ssl-renewal/DISASTER_RECOVERY.md`
- `docs/ssl-renewal/TROUBLESHOOTING.md`
- `docs/ssl-renewal/ARCHITECTURE.md`

### C 组 · 后端代码/测试（先读确认用途，再改）
- `backend/app/schemas/media.py`（确认是硬编码 base URL 还是示例：运行时域名 → 改读 settings；docstring/示例 → 替换 `example.com`）
- `backend/tests/test_media_access_descriptor.py`
- `backend/tests/test_qiniu_storage.py`

### D 组 · 官网（由 RND-239 迁移处理，本任务不动）
- `static_site/company_homepage/README.md`
- `static_site/company_homepage/index.html`

## 2. 执行步骤

### 阶段一：盘点与确认（只读）
- 跑 `grep -rln crowntime . --exclude-dir=.git --exclude-dir=.qoder` 得到完整清单，与 §1 比对。
- 重点读 A 组每个文件，确认域名出现位置与现有配置加载机制（shell 脚本如何取配置？是否已有 `source .env`？backend settings 是否已有对应字段？）。记录每处改造方式。

### 阶段二：脱敏改造（最小改动，保持运行行为）
- 选定统一环境变量名 `ARCHIVE_DOMAIN`（如项目已有同类命名可沿用，需保持一致）。
- **A 组（部署脚本）**：把字面量 `*.crowntime.cn` 按主机名改为对应环境变量读取（如 `MEDIA_DOMAIN` / `ARCHIVE_DOMAIN`）；确保脚本在 `.env` 提供真实值时行为完全不变。`.env.example` / `ssl-renew/examples/domain.env.example` 中各值写为占位符（`media.example.com` / `archive.example.com`）并加注释。
  - shell 侧：在脚本开头 `source` 既有配置，或 `export ARCHIVE_DOMAIN="${ARCHIVE_DOMAIN:-archive.example.com}"` 等；原有 `domain.env` 机制保持不变。
  - 测试 `.bats` / `.py`：域名从环境变量读取，默认 `example.com` 等价物；涉及真实联调的用例标注「需设置对应 `*_DOMAIN` 运行」。
- **B 组（文档）**：`crowntime.cn` → `example.com`；若文档在讲部署配置，改为 `${ARCHIVE_DOMAIN}` 并说明从 `.env` 读取。
- **C 组（后端）**：读 `backend/app/schemas/media.py` 确认用途；若是配置项，移到 pydantic settings（`Settings.archive_domain`，默认 `example.com`）；测试文件同理改用 `example.com` 或从 settings 读取。
- **D 组不动**（RND-239 处理）。

### 阶段三：验证
- `grep -rn "crowntime" . --exclude-dir=.git --exclude-dir=.qoder` 应**无结果**（历史残留属 RND-238）。
- 部署脚本烟雾测试：临时 `export ARCHIVE_DOMAIN=crowntime.cn` 后 `bash -n scripts/deploy_server.sh` 语法检查 + 跑 dry-run（若支持），确认不报错。
- ssl-renew 测试：`cd ssl-renew && <既有测试命令>`（bats / `test_qiniu_helper.py`）以 `ARCHIVE_DOMAIN=example.com` 跑，确认不因字面量消失而失败。
- backend：`cd backend && make verify`（lint-diff + typecheck + 全量 pytest）全绿；重点 `tests/test_media_access_descriptor.py`、`tests/test_qiniu_storage.py`。

## 3. 硬性约束
- 不改变线上运行行为：`.env` 配真实域名时结果与改前一致。
- 不引入新依赖；不重构无关代码；不动 RND-212。
- 不提交 / 推送（Haisu 操作）。
- 禁止把真实域名写回任何 committed 文件（`.env.example` 只用占位符）。

## 4. 收尾动作
- Linear 评论：改造方式（按组）+ `grep` 清零证据 + 测试结论。
- 保留给用户 commit。
