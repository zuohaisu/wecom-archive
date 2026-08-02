# Agent 一次性测试数据库（单一来源）

多张工单的 AC 需要**真实 PostgreSQL**：本仓 `AuditLog.detail` 等列是 `JSONB`
（SQLite 无此类型），且 SQLite 默认不强制外键，`alembic` 也需要真库才能跑往返。
本文件是这类需求的唯一操作说明；提示词一律引用本文件，不复制。

## 1. 为什么不能靠现成的 `DATABASE_URL`

本仓生产形状测试统一用：

```python
_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())

@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
```

（见 `backend/tests/test_rnd293_audit_log.py:31,58`，全仓 32 个测试文件同此模式。）

- `make test` **不设** `DATABASE_URL` → 这些用例自动 skip。
- CI 的 PG job 设（`.github/workflows/deploy.yml:134`），offline job 故意不设（`:121-126`）。
- 各开发机 `.env` 里的 `DATABASE_URL` 通常指向**本机开发库**（有数据），
  **不是**测试库。

## 2. 三条硬性否决 —— 命中任意一条就不许用那个库

在对任何库执行 `alembic upgrade/downgrade` 或写入测试数据之前，逐条检查：

| 否决条件 | 怎么查 |
|---|---|
| ❌ 库名不含 `test` 标记 | 解析 `DATABASE_URL` 末段库名，必须匹配 `*_test` 或 `test_*` |
| ❌ 库里已有业务数据 | `select count(*) from information_schema.tables where table_schema='public'` > 0 且非你本次建的 |
| ❌ 主机不是本机/明确的一次性实例 | host 必须是 `localhost` / `127.0.0.1` / 你自己起的容器 |

**尤其注意**：`.env` 里的 `DATABASE_URL` 常常指向开发库（例如 `wecom_archive`）。
它可能 `alembic_version` 远落后于当前 head——对它跑 `upgrade head` 会一次性套用
几十个 migration，**属于破坏性操作，绝对禁止**。

## 3. 自建一次性测试库（允许，且是首选做法）

从**现有** `DATABASE_URL` 里借 user/host/port，**只把库名换掉**：

```bash
# 1) 取连接参数（不要打印密码）
python3 - <<'PY'
import os, urllib.parse as u
p = u.urlparse(os.environ.get("DATABASE_URL", ""))
print(f"user={p.username} host={p.hostname} port={p.port or 5432} db={(p.path or '/').lstrip('/')}")
PY

# 2) 建一个全新的空库（库名必须带 test 标记）
createdb -h <host> -p <port> -U <user> wecom_archive_test

# 3) 只在本次会话内导出，不写进 .env、不写进任何被追踪的文件
export DATABASE_URL="postgresql://<user>@<host>:<port>/wecom_archive_test"

# 4) 确认它是空的（应输出 0）
psql "$DATABASE_URL" -tAc "select count(*) from information_schema.tables where table_schema='public';"
```

若本机根本没有 PostgreSQL 实例可用（无 `createdb`、无监听端口、无容器运行时），
才输出 `BLOCKED_NEEDS_HUMAN`，说明缺的是「一个可用的 PG 实例」。

## 4. 用完清理

```bash
dropdb -h <host> -p <port> -U <user> wecom_archive_test
```

**只能 drop 你自己在第 3 步建的那个库。** 不得 drop 任何既有库。
不清理也可以（下次复用），但必须在 QA Summary 里写明它还在。

## 5. 绝对禁止

- 对开发库 / 共享库 / 生产库执行 `alembic upgrade`、`downgrade` 或任何写入。
- 为了让测试不 skip 而修改 `conftest`、测试基础设施、`Makefile` 或 CI 配置。
- 用 SQLite 冒充生产形状（`JSONB` 不存在，外键默认不强制）。
- 把测试库连接串写进 `.env`、提交进仓库、或打印其密码。
- drop / truncate 任何不是你本次创建的对象。

## 6. 记录要求

QA Summary 与 verdict evidence 中必须写明：

- 实际使用的库名与 host（**不写密码**）
- 该库是你新建的，还是环境已提供的
- 是否已清理
- 若走了 `BLOCKED_NEEDS_HUMAN`，写明缺的是实例还是权限

## 沿革

- **2026-08-02**：建立。起因是 RND-337 的 dev agent 因 `DATABASE_URL` 为空而整票
  BLOCK——当时的提示词写的是「你无法自备 PG，直接上报」，把本可自行解决的环境
  准备变成了人工闸。同时发现某开发机 `.env` 的 `DATABASE_URL` 指向有数据、
  且 `alembic_version` 落后 26 个版本的开发库，故补齐第 2 节的硬性否决。
