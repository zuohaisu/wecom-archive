[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-333 的 6 条 AC（重点验证缺 key 时 fail-closed 而非静默存明文）并产出带证据的 PASS/FAIL 判定。

# RND-333 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-333「D2-1 字段级加密原语 + app_secret 加密落盘」｜风险等级 **R2（触及凭据处理）**
- **本票 blocks RND-311 与 RND-332。误判 PASS 的代价：两张下游票会建在一个可能"假加密"的地基上，且这类缺陷不报错、不可见。从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令、在本地跑测试。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 原语可用且对称
- 证据：测试断言 `decrypt_value(encrypt_value(x)) == x` 且密文 `!= x`。
- 判定：两条断言都有 = PASS。

### AC-2 — 缺 key fail-closed（**最高风险项，重点查**）
- 证据：**必须存在**「`FIELD_ENCRYPTION_KEY` 未设置 → `encrypt_value` 抛异常」的测试用例（如 `monkeypatch.delenv` 后断言 `pytest.raises`）。
- **代码审阅（关键）**：`grep -nE 'or plain|return plain|except.*return|getenv\(.*,\s*["\x27]' backend/app/crypto.py` —— 确认**没有**任何「取不到 key 就返回明文 / 用默认 key / 吞异常」的分支。
- 背景：静默降级存明文是本票最严重的失败模式——系统看起来在加密，实际没有，且不报错，可能几个月无人发现，而这存的是客户的企微永久凭据。
- 判定：有抛错测试 **且** 代码无降级分支 = PASS。**发现任何静默降级（含用硬编码默认 key）→ FAIL（`SECURITY_VIOLATION`, severity: blocker）。**

### AC-3 — 新写入加密
- 证据：测试构造 `TenantWecomConfig`、经写入访问器落库后，**直查 DB** 断言存储值 `!= 入参明文`；且 `decrypted_app_secret == 入参明文`。
- 判定：加密落盘 + 可还原，两向都有断言 = PASS。仅测了 round-trip 而没直查 DB 值 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）——那证明不了它真的以密文落盘。

### AC-4 — 再加密脚本幂等且可 dry-run
- 证据：`--dry-run` 报告行数且不写库；对已加密行重复运行为 no-op（靠 `is_encrypted`）。
- 判定：两项均有用例 = PASS。

### AC-5 — 零泄露（**重点查**）
- 证据：
  - 密钥不在代码/测试固定值中：`grep -rnE '[A-Za-z0-9_\-]{43}=' backend/app/crypto.py backend/tests/test_field_encryption.py` 不应命中写死的 Fernet key（测试应动态 `Fernet.generate_key()`）。
  - `.env.example` 里 `FIELD_ENCRYPTION_KEY` **只有占位符 + 生成说明，不是真实 key**。
  - **异常消息不回显明文**：审阅 `crypto.py` 与访问器的 `raise` 语句，确认没有把 `plain` / `app_secret` 值拼进 error string。
  - 无 `print`/`log` 输出明文 secret。
- 判定：全部干净 = PASS。任一泄露 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-6 — 架构边界与回归
- 证据：
  - `"app.crypto"` 已加入 `backend/tests/test_architecture_boundary.py:70` 的 `_FLAT_SERVICE_MODULES`，且该测试通过。
  - `make verify` exit 0。
  - **归档同步 / 解密链路零改动**：`git diff --stat -- backend/app/services/decrypt_worker.py backend/scripts/decrypt_wecom_messages_once.py backend/app/decrypt_isolation.py` **必须无输出**。
- 判定：全部满足 = PASS。解密链路有任何改动 → FAIL（`SCOPE_VIOLATION`, blocker）——本票明确只管 `app_secret` 一个字段。

## 本项目专属检查（必查）
1. **未新增依赖**：`git diff -- backend/requirements.txt` **必须无输出**（`cryptography==49.0.0` 本来就在第 1 行）。新增依赖 → FAIL（`SCOPE_VIOLATION`）。
2. **未越界做 RND-332**：diff 中不得出现 `KeyProvider` / `local_file` / `kms_envelope` 类抽象、`KeyVersion` 约束修改、解密审计钩子。出现 → FAIL（`SCOPE_VIOLATION`）——那是 RND-332，抢做会导致两处实现打架。
3. **未越界做 RND-311**：不得出现 `POST /api/platform/tenants` / `routers/platform.py`。
4. **列名未变**：`app_secret` 列名保持不变（`grep -n "app_secret" backend/app/db/models.py`），且**无新迁移**（本票不改列定义）。出现新迁移文件 → 需在 QA Summary 中有充分理由，否则 FAIL。
5. **未碰 R1 页面票文件**：`git diff --stat -- backend/app/web/ backend/app/assets/i18n.js backend/app/routers/admin_*_page.py` 必须无输出。
6. **文件所有权**：`git status --porcelain` 改动应限于：`app/crypto.py`、`models.py`、`reencrypt_app_secrets_once.py`、`test_architecture_boundary.py`（仅一行）、`test_field_encryption.py`、`.env.example`。清单外文件 → FAIL（`SCOPE_VIOLATION`）。
   - **例外说明**：若 QA Summary 提到 `bootstrap_default_tenant.py` 仍以明文写 `app_secret`——**这是本票已知的、被要求上报而非修复的缺口**（该文件不在拥有清单内）。仅上报不算失败；但若开发 agent **真的改了**该文件 → FAIL（`SCOPE_VIOLATION`）。

## 附加检查（Security）
- 无真实企微 secret / 真实凭据进入测试固定值。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL（`SECURITY_VIOLATION`）。
- 未改 CI/CD、部署配置。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_field_encryption.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/services/decrypt_worker.py backend/scripts/decrypt_wecom_messages_once.py backend/app/decrypt_isolation.py   # 必须无输出
git diff --stat -- backend/requirements.txt        # 必须无输出
grep -nE 'or plain|return plain|except.*return' backend/app/crypto.py   # AC-2 降级分支排查
grep -n "FIELD_ENCRYPTION_KEY" .env.example        # 只应是占位符
git status --porcelain
git log origin/main..HEAD                          # 必须无输出
```

## 产出
写入 `tasks/RND-333-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：`bootstrap_default_tenant.py` 明文写入缺口是否被如实上报（上报=符合预期，修改=越权）。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-2 若无「缺 key 时抛错」的测试用例 → 直接 FAIL**，不接受「代码看起来会抛」。这是本票唯一防止"假加密"的防线。
- **AC-3 若只有 round-trip 测试而无直查 DB 断言 → 直接 FAIL**——round-trip 通过不代表落盘的是密文。
