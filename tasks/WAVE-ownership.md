# 波次文件所有权表 — RND-335 ~ RND-339

本项目的并行方案是「直接在 `main` 上、靠文件所有权错峰」
（见 `docs/ticket-autopilot-workflow.md` §2、§3.4）。该方案成立的**唯一前提**是：
**任意两张可能同时 In Progress 的工单，拥有文件集合必须不相交。**

单张 dev prompt 只能看见自己的清单，看不见别人的——所以跨票冲突必须在这里
统一分配。**本文件是所有权的权威来源；dev prompt 的清单与本文件冲突时，以本文件为准。**

## 1. 波次与阻塞关系

```
波次 A（安全活动）      RND-335 ──blocks──> RND-336
波次 B（消息可见性）    RND-337 ──blocks──> RND-338
                              └─blocks──> RND-339      (338 与 339 可并行)
```

RND-335 与 RND-337 均无前置，**两个波次同时开工**。因此下面的并发矩阵按
「实际可能同时 In Progress」计算，而不是按波次内顺序。

## 2. 并发矩阵

| | 335 | 336 | 337 | 338 | 339 |
|---|---|---|---|---|---|
| **335** | — | 不并发（335 blocks 336） | 可并发 ✅ | 可并发 ✅ | 可并发 ✅ |
| **336** | | — | 可并发 ✅ | ⚠️ **冲突** | 可并发 ✅ |
| **337** | | | — | 不并发（337 blocks 338） | 不并发（337 blocks 339） |
| **338** | | | | — | 可并发 ✅ |
| **339** | | | | | — |

✅ = 拥有文件集合已核对不相交。

## 3. ⚠️ RND-336 ∥ RND-338 冲突与裁决

两票的原始清单同时拥有：

| 文件 | RND-336 用途 | RND-338 用途 |
|---|---|---|
| `backend/app/assets/i18n.js` | audit → security & activity 文案，新增 settings card keys | `nav.diagnostics` → archive health 文案 |
| `backend/tests/test_sidenav.py` | 断言 audit-log 项已从一级导航移除 | 断言 diagnostics 项文案/key 变化 |

且 RND-336 会**结构性修改** `backend/app/web/sidenav.py`（从 `NAV` 删除
`audit-log` 项），而 RND-338 把同一文件列为只读并注明「结构不变」。

**裁决（2026-08-02）：串行化，RND-336 在前。**

- `i18n.js`、`test_sidenav.py`、`sidenav.py` 三个文件在本波次中**归 RND-336**。
- **RND-338 不得在 RND-336 的共享文件落定之前开工。** RND-338 dev agent 的 Preflight
  必须检查 `tasks/archive/RND-336-qa-verdict.json`。这道闸的目的是**文件所有权交接**，不是
  功能依赖，所以判据是「代码是否已落定」而非 verdict 字面值：
  - `PASS` → 通过。
  - `BLOCKED` 但全部 AC 为 `PASS`、无 scope/security finding、阻塞原因仅
    `HUMAN_VISUAL_REVIEW_PENDING` → **通过**（视觉 gate 是 Haisu 的人工动作，
    RND-336 不会再回头改共享文件）。
  - 文件缺失，或存在未过的 AC / finding → `BLOCKED_NEEDS_HUMAN`，零产品代码改动。
- 理由：RND-336 改的是导航**结构**（少一个 item），会使 RND-338 关于 NAV 的假设
  失效；RND-338 只改**文案**，可以无痛适配一个少了一项的 NAV。反向顺序则需要
  RND-336 重做 RND-338 已经写好的 nav 断言。
- RND-338 开工时 `NAV` 中**不应该**再有 `audit-log` 项——这是 RND-336 的交付物，
  **不是** RND-338 的归因 diff，QA 不得据此判 `SCOPE_VIOLATION`。

若运维现实要求反向顺序（例如 RND-335 长期卡住而 RND-337 已 PASS），
**由 Haisu 在本文件改判并同步更新两份 dev prompt 与两份 qa prompt**，
不得由 agent 自行调换。

## 4. 波次内顺序共享（非冲突）

下列文件被同波次的前后两票共同拥有。因为它们之间有硬 blocker，**不可能同时
In Progress**，所以不构成冲突，但后一票必须在**前一票已落地的版本**上继续：

| 文件 | 先 | 后 | 后者允许的改动 |
|---|---|---|---|
| `backend/app/db/models.py` | RND-337 | RND-339 | 仅新增 finding 模型/约束，复用 337 的 run 模型 |
| `backend/app/main.py` | RND-337 | RND-339 | 仅 import + include findings router |
| `backend/tests/test_http_contract.py` | RND-337（+2） | RND-339（+1） | 读**当前真实** route_count 再 +1，禁止写死数字 |
| `backend/app/services/reachability_check_service.py` | RND-337（新建） | RND-339 | 仅 trigger source / watermark / reconcile 的向后兼容扩展 |
| `backend/alembic/versions/` | RND-337（0032） | RND-339（0033） | 从**实际唯一 head** 线性延伸，文件名以实际落地为准 |

## 5. 跨票只读交叉引用（补全 dev prompt 缺失的 owner 标注）

`tasks/_templates/dev-prompt-template.md` 要求只读条目标注 `— 所有者 RND-<M>`。
下列条目在原 prompt 中缺少 owner，按本表补全：

| 文件 | 所有者 | 对谁只读 |
|---|---|---|
| `backend/app/assets/i18n.js` | RND-336 | RND-338 |
| `backend/tests/test_sidenav.py` | RND-336 | RND-338 |
| `backend/app/web/sidenav.py` | RND-336 | RND-338、RND-339 |
| `backend/app/main.py` | RND-337 → RND-339 | RND-335、RND-336、RND-338 |
| `backend/tests/test_http_contract.py` | RND-337 → RND-339 | RND-335、RND-336、RND-338 |
| `backend/app/db/models.py` | RND-337 → RND-339 | RND-335、RND-336、RND-338 |
| `backend/app/routers/web.py` | RND-338 | RND-339 |

**归因提醒（§3.4）**：RND-335 的 QA 会检查「`main.py` 无业务改动」，但 RND-337
合法地在改 `main.py`。QA agent 必须先按本表分离归因，把 RND-337 的在途改动写进
`notes`，**不得**记在 RND-335 账上。同理适用于 `models.py` 与 `test_http_contract.py`。

## 6. 全项目不变量（每张新增路由的票都适用）

见 `docs/ticket-autopilot-workflow.md` §3.3。本波次中：

- RND-337（+2 route）、RND-339（+1 route）必须拥有 `test_http_contract.py` ✅ 已在清单内
- 两票的新 router 均使用 `get_current_user`（非 `require_role`），
  **不触发** `test_rnd280_rbac_scaffold.py` 的闭世界白名单 → 保持只读 ✅
- RND-335 修改 `AuditLogOut` 增加 `category` 字段：`_snapshot_response_model()`
  只记录 response model 的**类名**（`test_http_contract.py:126-136`），
  新增字段不会触发快照失败 → `test_http_contract.py` 对 RND-335 保持只读 ✅

## 沿革

- **2026-08-02**：建立。起因是 RND-336 与 RND-338 的拥有清单在 `i18n.js` 与
  `test_sidenav.py` 上重叠，而两份 prompt 互相完全没有提及对方——RND-338/339
  只在**波次内**核对了 disjoint，没有做**跨波次**核对。
