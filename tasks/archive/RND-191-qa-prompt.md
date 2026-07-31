# RND-191 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「Codex 验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收对象：开发 agent 按 `rnd-191-execution-prompt.md` 产出的改动。

---

## 一、验收目标

确认 RND-191「API / SQL 性能优化」达成：关键列表/时间线/搜索更快、DB 资源峰值下降，**零回归**（对外契约、排序、分页、租户隔离、认证、主流程语义不变），且每项优化可追溯到 RND-190 基线。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 时间线与详情（A / B）
- [ ] **A1** `get_conversation_messages`（`timeline_service.py:582`）不再在 Python 里全量加载整段会话再切片；游标分页已下推 SQL（LIMIT + `msgtime,id < cursor`），仅取本页列。
  - 证据：读源码确认 `_fetch_conversation_messages` 及其调用链带 LIMIT/游标；`:633-640` 的 `sorted`+切片改为消费已分页结果。
- [ ] **A2** 大会话（>1000 条）翻页 P95 显著低于 RED（目标：同等场景下从秒级降到百毫秒级）。
  - 证据：同阶段一场景计时；附前后数字。
- [ ] **B1** `get_conversation_detail` 已用聚合 SQL（`COUNT`/`SUM(decrypt_status)`/`DISTINCT sender`）或确认 RND-240 已合入且本单未重复实现。返回 `participants`/`decrypted_percent` 字段逐字段等价。
  - 证据：读源码；同一会话新旧路径 JSON 一致（旧路径可临时用 git stash 对照）。

### 列表 N+1 与冗余（C / D / E）
- [ ] **C1** `list_monitored_accounts`（`listing_service.py:561-589`）的 per-seat 循环已合并为分组 SQL；seat 数多时无 ≈7×S 查询尖峰。
  - 证据：`EXPLAIN` / SQL 日志确认单条分组查询；压测 seat 多时查询数不再线性增长。
- [ ] **D1** `list_contacts`/`list_monitored_accounts`/`search_messages` 不再各跑冗余 tenant-wide `distinct()`；请求内 seat 集合只计算一次。
- [ ] **E1** 显示名加载改用 `_load_display_names_for_ids`（按参与者 id 作用域），不再无差别加载全 Contact 表。

### 搜索与索引（F / G）
- [ ] **F1** `search_messages`（`search.py:325`）关键字搜索命中索引（`pg_trgm` GIN 或 `to_tsvector`+`websearch_to_tsquery`），不再全租户 `ILIKE` 顺序扫描。`Contact`/`AdminUser` 名搜索同理。
  - 证据：`EXPLAIN (ANALYZE, BUFFERS)` 显示 Index Scan；搜索 P95 低于 RED。
- [ ] **G1** 新增复合索引（`(tenant_id, msgtime, id)` 等）经 `EXPLAIN` 验证被采用；迁移可回滚（`alembic downgrade` 或等价）。
  - 证据：迁移文件存在；测试库 `EXPLAIN` 附件。

### 响应体与连接池（I / H）
- [ ] **I1** 列表端点投影摘要列、限制/分页，响应体不含整 `content_text`；`list_contacts` 已分页。
- [ ] **H1** 连接池（`db/session.py:17`）若调整，须在 N+1 全部修复且压测证明排队后才改；`pool_size+max_overflow ≤ 5` 且有依据。

### 全局契约
- [ ] **C0** 路由数不变（`test_http_contract` 断言）。
- [ ] **T0** 租户隔离未被破坏（`test_search_messages_tenant_isolation` 等）。
- [ ] **M0** `make verify` **全绿**。
- [ ] **X0** 无基线证据未引入缓存/队列/新依赖；无未经 `EXPLAIN` 验证的索引。

---

## 三、回归套件（必须全绿）

```
make verify
```
重点确认（任一失败即 FAIL，附失败栈）：
- `test_search_api.py` / `test_search_messages_tenant_isolation.py`
- `test_http_contract.py`（路由数不变）
- `test_rnd_228_search_scalability.py`
- `test_rnd240_*`（若复用其聚合路径）
- `test_staff_seats.py`（列表/监控账号语义）

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码有 Bug** → 反馈给开发 agent 修复，附具体错误 + 失败测试名 + 期望行为。**不自行改实现**。
- **测试代码有 Bug** → 仅当测试断言了旧的慢路径时可自行修正（须报告标注并说明依据）。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 数字对比 + 每项对应 RND-190 瓶颈条目。

最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮回归验证；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-191 验收结论：PASS / FAIL
RED 基线：列表 P95 ___ms；时间线翻页 ___ms；搜索 ___ms；响应体 ___KB
GREEN：    列表 P95 ___ms；时间线翻页 ___ms；搜索 ___ms；响应体 ___KB
索引/迁移：___（可回滚 / EXPLAIN 验证）
契约：URL/status/body/OpenAPI/租户/i18n —— 不变
回归：make verify ___（绿/红，附失败项）
追溯：每项优化对应 RND-190 瓶颈 #___
遗留：___（若有）
```
