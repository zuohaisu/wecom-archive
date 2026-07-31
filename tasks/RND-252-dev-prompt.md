[Goal check] This work advances 开发（Development） by 交付配置中心的连通性自检端点（七牛/企微/域名），复用既有 QiniuStorageProvider 与 get_wecom_token，帮用户在保存配置前发现凭据问题。

# RND-252 开发提示词（Developer Prompt）— 配置中心 T7

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-252 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 开工前必须先核实的前置条件
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.routers.settings import settings_router; print('OK')"
```
T4/T5 未就绪 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 任务身份
- 工单：RND-252「配置中心 T7：连通性自检（七牛/企微/域名）」｜父 Epic RND-244
- 优先级：Low（P1）｜风险等级：**R1**｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实，两个现成的复用点）

**企微连通性**：`backend/app/auth.py:364` 的 `get_wecom_token(corp_id, oauth_secret, cache_key=None)`——<issue>RND-312</issue>（B2-2 租户级连通性自检）已经用同样的方式复用过这个函数，**本票照抄同样的复用模式**（传独立的 `cache_key`，理由同 RND-312：不传会复用生产解密路径共享的缓存，见该票的经验教训——`cache_key=f"config-self-check:wecom"` 即可，本票是全局配置不是按租户，key 不需要带 tenant_id）。

**七牛连通性**：`backend/app/qiniu_storage.py` 的 `QiniuStorageProvider`——构造函数已做基本参数校验（缺字段直接抛 `QiniuConfigurationError`），内部持有 `self._bucket_manager`（七牛 SDK 的 `qiniu.BucketManager` 实例）。**用这个已实例化的 bucket manager 做一次轻量只读调用**（如 `list()` 小 limit 或 `stat()` 任意 key）验证凭据有效——**不要自己写调七牛 REST API 的 HTTP 请求**，SDK 已经封装好了签名逻辑。

**域名格式校验**：纯格式检查（`https://` 前缀 + 合法域名格式），用 `urllib.parse.urlparse` 校验 scheme，**不需要真实网络请求**。

**❗ 本项目高频踩坑：**
- **`cache_key` 必须显式传**（同 RND-312 教训）：省略会读到生产解密路径共享的 token 缓存，自检可能不发真实请求就"成功"。
- 架构边界：不得 import `app.routers.*`/`app.main`。

## 目标（Goal）
交付一个自检端点：分别验证七牛、企微、域名格式三项配置的可用性，返回逐项结果，不泄露密钥。

## 范围边界

**In scope：**
1. `backend/app/config/validation.py`（**新建**）：
   - `def check_qiniu(access_key, secret_key, bucket, domain, region=None) -> tuple[bool, Optional[str]]`：构造 `QiniuStorageProvider` + 轻量只读调用，成功 `(True, None)`，失败 `(False, "<脱敏后的简要原因>")`。
   - `def check_wecom(corp_id, oauth_secret) -> tuple[bool, Optional[str]]`：调用 `get_wecom_token(corp_id, oauth_secret, cache_key="config-self-check:wecom")`。
   - `def check_domain_format(domain: str) -> tuple[bool, Optional[str]]`：纯格式校验，无网络请求。
2. `backend/app/routers/settings.py`（**追加**）：`POST /settings/test-connection`，请求体 `{"target": "qiniu"|"wecom"|"domain"}`（**不接受直接传密钥明文走这个端点**——从 config resolver 读当前已保存的值来测，避免在这个端点上重新造一套"临时凭据测试"的输入面；若用户想测试尚未保存的新值，前端应先 `PUT /settings` 保存，再调用本端点——这是本票刻意的简化范围）。
3. 测试：`backend/tests/test_rnd252_connectivity_check.py`（**mock** `get_wecom_token`/`QiniuStorageProvider`，不发真实请求）。

**Out of scope（显式非目标）：**
- 不支持测试"尚未保存的临时凭据"（见上）。
- 不改 `get_wecom_token`/`QiniuStorageProvider` 本身。
- 不做定时自动自检。

**本工单拥有的文件（只许写这些）：**
- `backend/app/config/validation.py`（新）
- `backend/app/routers/settings.py` —— **仅追加** `POST /settings/test-connection`
- `backend/tests/test_rnd252_connectivity_check.py`（新）

**只读、绝不可写：** `app/auth.py`（只调用 `get_wecom_token`）、`app/qiniu_storage.py`（只调用 `QiniuStorageProvider`）、`app/config/resolver.py`（只调用）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 三项自检均可用**：`target=qiniu`/`wecom`/`domain` 各自返回 `{"ok": bool, "reason": Optional[str]}`。
- **AC-2 复用而非重写**：`grep -n "get_wecom_token\|QiniuStorageProvider" backend/app/config/validation.py` 均有命中；无自造 HTTP 调用七牛/企微 API 的代码。
- **AC-3 `cache_key` 显式传递**：`get_wecom_token` 调用显式传 `cache_key`，不省略。
- **AC-4 零泄露**：`reason` 字段脱敏，不含明文密钥或未处理的底层异常信息。
- **AC-5 从已保存配置读取，不接受临时凭据**：请求体只含 `target`，不含任何密钥字段；validation 函数的入参来自 `resolve()` 读取当前已保存值。
- **AC-6 测试无真实网络请求**：`get_wecom_token`/`QiniuStorageProvider` 在测试中均被 mock。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.config.resolver import resolve; from app.routers.settings import settings_router; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd252_connectivity_check.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "get_wecom_token\|QiniuStorageProvider" backend/app/config/validation.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T4（<issue>RND-248</issue>）+ T5（<issue>RND-249</issue>）**必须先落地**。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：忘记传 `cache_key`，复用生产缓存导致自检失真（同 RND-312 的真实教训）——由 AC-3 防守。
- 回滚：纯新增/追加，`git checkout -- <files>` 即可。

## 人工点位
- **Trigger**：Haisu 置 In Progress（建议 T4/T5 落地后派发）。
- **Gate**：测试绿即可（R1，只读自检）。
- **Escalation**：T4/T5 未就绪 → `BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引
1. 先跑前置核实命令。
2. 读 `app/auth.py:364`、`app/qiniu_storage.py`（`QiniuStorageProvider` 构造与 `_bucket_manager`）、`app/config/resolver.py`。
3. 写 `validation.py` 三个 check 函数。
4. 追加端点。
5. 写测试（**mock 两个外部依赖**）。
6. 跑验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push；不碰生产数据密钥；测试必须 mock 外部调用；复用优先；证据优先。
