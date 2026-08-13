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
- PR CI 的 PG job 设（`.github/workflows/test.yml`），offline job 故意不设。
- 各开发机 `.env` 里的 `DATABASE_URL` 通常指向**本机开发库**（有数据），
  **不是**测试库。

## 2. 三条硬性否决 —— 命中任意一条就不许用那个库

在对任何库执行 `alembic upgrade/downgrade` 或写入测试数据之前，逐条检查：

| 否决条件 | 怎么查 |
|---|---|
| ❌ 库名不含 `test` 标记 | 解析连接串（环境变量或 `.env`）末段库名，必须匹配 `*_test` 或 `test_*` |
| ❌ 库里已有业务数据 | `select count(*) from information_schema.tables where table_schema='public'` > 0 且非你本次建的 |
| ❌ 主机不是本机/明确的一次性实例 | host 必须是 `localhost` / `127.0.0.1` / 你自己起的容器 |

**尤其注意**：`.env` 里的 `DATABASE_URL` 常常指向开发库（例如 `wecom_archive`）。
它可能 `alembic_version` 远落后于当前 head——对它跑 `upgrade head` 会一次性套用
几十个 migration，**属于破坏性操作，绝对禁止**。

## 3. 自建一次性测试库（允许，且是首选做法）

### 3.1 凭据从 `.env` **文件**读，不是从环境变量读

⚠️ 这一步最容易搞错：走到这里时 `DATABASE_URL` **环境变量必然是空的**
（那正是你需要建库的原因）。凭据在**仓库根的 `.env` 文件**里，必须读文件。
借它的 user / password / host / port，**只把库名换掉**。

### 3.2 不需要 `psql` / `createdb`

Postgres.app、Docker、远程实例等情形下，客户端二进制常常**不在 `PATH` 上**，
但 `.venv` 里有 `psycopg2`。**没有 `psql` 不是 BLOCK 的理由。**
（若确实想用命令行：Postgres.app 的二进制在
`/Applications/Postgres.app/Contents/Versions/latest/bin/`，把它加进 `PATH` 即可。）

下面这段**已实测可用**，直接照抄：

```bash
.venv/bin/python - <<'PY'
import pathlib, urllib.parse as u, psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

TEST_DB = "wecom_archive_test"          # 必须带 test 标记

# 1) 从 .env 文件读凭据（环境变量此时为空）
url = None
for line in pathlib.Path('.env').read_text().splitlines():
    line = line.strip()
    if line.startswith('DATABASE_URL=') and not line.startswith('#'):
        url = line.split('=', 1)[1].strip().strip('"').strip("'")
        break
assert url, ".env 里没有 DATABASE_URL"
p = u.urlparse(url)
conn_kw = dict(user=p.username, password=p.password,
               host=p.hostname or 'localhost', port=p.port or 5432)
print(f"user={p.username} host={p.hostname} port={p.port or 5432}")   # 不要打印密码

# 2) 连 postgres 维护库，建一个全新空库
c = psycopg2.connect(dbname='postgres', **conn_kw)
c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = c.cursor()
cur.execute("select 1 from pg_database where datname=%s", (TEST_DB,))
if cur.fetchone():
    print(f"{TEST_DB} 已存在，复用")
else:
    cur.execute(f'CREATE DATABASE "{TEST_DB}"')
    print(f"created {TEST_DB}")

# 3) 确认它是空的（应输出 0）
c2 = psycopg2.connect(dbname=TEST_DB, **conn_kw)
k = c2.cursor()
k.execute("select count(*) from information_schema.tables where table_schema='public'")
print("public tables =", k.fetchone()[0])
c2.close(); c.close()
PY
```

然后**只在本次会话内**导出（不写进 `.env`、不写进任何被追踪的文件）：

```bash
export DATABASE_URL="postgresql://<user>:<password>@<host>:<port>/wecom_archive_test"
```

### 3.3 什么时候才算真的没辙

依次排除后仍不行，才输出 `BLOCKED_NEEDS_HUMAN`，并写明**卡在哪一步**：

| 症状 | 先做这个 | 仍不行 → 上报内容 |
|---|---|---|
| `command -v psql` 为空 | 用 §3.2 的 psycopg2 方案，或把 Postgres.app 的 bin 加进 `PATH` | —（这不构成 BLOCK） |
| 环境变量 `DATABASE_URL` 为空 | 读 `.env` 文件（§3.1） | `.env` 里也没有 DATABASE_URL |
| 用 `.env` 凭据连 `postgres` 库失败 | 确认 host/port/user/password 是否照抄自 `.env`（含密码） | 「`.env` 凭据无法连接 PG，需要可用凭据」 |
| 连上了但 `CREATE DATABASE` 被拒 | — | 「该角色无 CREATEDB 权限，需要建库权限或一个预建的 `*_test` 库」 |
| 端口无人监听 / 无 PG 实例 | — | 「本机没有可用的 PostgreSQL 实例」 |

**不要**把「没有 `psql`」或「环境变量为空」当成 BLOCK 理由——这两个都有解法。

## 4. 用完清理

`dropdb` 不在 `PATH` 时同样用 psycopg2（把 §3.2 的 `CREATE` 换成 `DROP`）：

```bash
dropdb -h <host> -p <port> -U <user> wecom_archive_test   # 有 dropdb 时
```

**只能 drop 你自己在 §3 建的那个库。** 不得 drop 任何既有库。
不清理也可以（下次直接复用，§3.2 的脚本对已存在的库是幂等的），
但必须在 QA Summary 里写明它还在。

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
- **2026-08-02（第 2 轮）**：dev agent 第二次 BLOCK，理由是「没有 `psql`/`createdb`，
  默认认证连不上」。根因是本文件 §3 原来写的是「从**现有** `DATABASE_URL` 借参数」
  ——而走到 §3 时该环境变量必然为空，属循环依赖；凭据其实在 `.env` **文件**里。
  故改写 §3：明确从 `.env` 文件读凭据（§3.1）、给出不依赖 `psql` 的 psycopg2
  建库脚本（§3.2，已实测）、并补 §3.3 的排除表，把「没有 psql」「环境变量为空」
  明确排除出 BLOCK 理由。
