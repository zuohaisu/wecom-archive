# RND-229 — 搜索分页测试全套偶发失败（结果重叠）执行提示词

> 状态：**已应用（2026-07-24，由 Developer 直接修复）**。`tests/test_search_api.py` 新增 `autouse` fixture 在每个测试前后 `app.dependency_overrides.clear()`；`test_search_messages_pagination` 改用唯一 `uuid` 关键词 + 游标全页遍历 + 精确总数（==5）与无重叠断言。`make verify` 全绿，全套连跑 3 次稳定通过。保留作根因记录，勿重复应用。

## 背景（Context）
RND-229 第 3 次验收中 `make verify` 仍 **FAIL**：除 RND-206 的 `focusPending is not defined`（见另一份提示词 `rnd-229-focuspending-test-fix-execution-prompt.md`）外，全套 pytest 运行中 `test_search_messages_pagination` **偶发**失败（报错为 `assert first_ids.isdisjoint(second_ids)` 结果重叠），但单独连续重跑 10 次均通过。RND-229 未修改 `search.py`，本报告暂**不能归因于本任务**，但当前全套不稳定，需先定位并加固，才能让 `make verify` 全绿。

## PM 侧已完成的可只读排查结论
- **`search.py` 游标分页实现正确**：`search_messages`（`backend/app/routers/search.py:266`）使用 keyset 分页：
  - `before` 游标解析为 `(before_msgtime, before_id)`（`:302–309`）；
  - 过滤条件 `msgtime < before_msgtime OR (msgtime == before_msgtime AND id < before_id)`（`:311–319`）；
  - `order_by(ArchiveMessage.msgtime.desc(), ArchiveMessage.id.desc())` + `.limit(limit + 1)` 探测 `has_older`（`:321–330`）。
  - 对**固定结果集**，该分页必然产生互不重叠的页面，不可能出现 `isdisjoint` 失败。
- **非跨测试 DB 污染**：`tests/test_reachability_audit.py` 的 `db` fixture 为 **function-scoped**（`:185–189`），每次测试 `_make_session()`（`:165–182`）新建独立 `sqlite:///:memory:`（StaticPool + `check_same_thread=False`）。每个测试的数据彼此隔离。
- **`search.py` 无 `lru_cache`/模块级缓存**；`_build_staff_ids`（`:117`）为纯函数，不影响分页结果。
- `test_search_messages_pagination`（`tests/test_search_api.py:318`）通过 `_authed()` 显式覆盖 `get_db`/`get_current_user`（`:45–55`），自身插入 5 条 `msgtime` 为 996–1000、内容含 `keyword` 的消息；独立运行时结果集固定为这 5 条，分页必然不重叠 → 必定通过。
- **结论**：孤立运行永远通过、全套偶发失败，说明根因在 **`app` 单例的跨测试状态泄漏**（`app` 为模块级单例；其它测试设置 `app.dependency_overrides`/`app.state` 等可变状态，若未清理，在全套排序下可能影响后续测试），而非 `search.py` 逻辑或本测试数据。

## 复现（Repro）
不要只跑单文件（单文件必过）。在 `backend/` 下多次跑全套以捕获 flake：
```bash
cd backend
for i in 1 2 3 4 5; do
  echo "=== run $i ==="
  .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -4
done
```
重点复现「共享 `app` 单例 + 设置 override 的模块」相邻排序：
```bash
.venv/bin/python -m pytest tests/test_search_api.py tests/test_conversation_display_names.py tests/test_tenant_isolation.py tests/test_auth.py tests/test_tenant_media_access.py -q
```
关注是否出现 `assert first_ids.isdisjoint(second_ids)` 失败，以及失败前的 `app.dependency_overrides` 残留。

## 修复方向（Fix，仅改 `backend/tests/*`，不碰 `search.py` / `main.py`）
目标：让该测试对跨测试状态完全免疫，并把「偶发重叠」转成「确定性总量校验」。

1. **测试开始即清理 `app` 单例状态**：在 `test_search_messages_pagination` 函数开头加 `app.dependency_overrides.clear()`（不只 `finally`），避免继承前序测试残留的 override。
2. **唯一化关键词**：用 `import uuid; kw = f"kw_{uuid.uuid4().hex}"`，插入消息时内容用该 `kw`，查询 `q=kw`。彻底排除任何其它代码路径或残留数据命中 `keyword`。
3. **显式总量断言**：翻页前确认命中总数恰为 5（例如循环把 `has_older` 链拉完计数，或断言两次请求返回的去重总数 == 5），再校验页面 `isdisjoint`。一旦有污染/泄漏，立即以清晰断言暴露，而非偶发重叠。
4. **收口所有测试（推荐）**：在 `tests/test_search_api.py` 或共享 conftest 增加 `autouse` fixture，在每个测试开始时 `app.dependency_overrides.clear()`，从根上消除跨测试 override 泄漏。
5. **优先在泄漏源头修复**：若复现定位到某个具体测试（如某测试设置 override 后未清理、或改了 `app.state`/模块级可变状态），优先清理该测试，而非只加固本测试。

## 验收标准（Acceptance Criteria）
- [ ] 全套 pytest 连续运行 ≥ 3 次全绿（或至少该用例在整套排序下稳定通过）。
- [ ] 单独重跑 `test_search_messages_pagination` 仍通过。
- [ ] 不修改 `backend/app/main.py`、`backend/app/routers/search.py` 业务逻辑，仅加固测试与（必要时）清理泄漏源。
- [ ] `make verify` 整体通过（须同时包含 focusPending 修复）。
- [ ] 不引入新的 lint / 类型错误。

## 约束（重要）
- 本任务只动 `backend/tests/*` 测试文件；**不要碰 `backend/app/main.py`、`backend/app/routers/search.py` 等源码**。
- **不要 commit**：用户会自行核验并通过 GitHub PR 合并。完成后把结果告知用户 / PM 即可。
- 若对 `search.py` 真实游标实现不确定，先读 `backend/app/routers/search.py:266–400` 确认，再写测试——保持测试断言与源码语义一致。

## 参考
- 分支：`feature/rnd-229`
- 关联提示词：`rnd-229-focuspending-test-fix-execution-prompt.md`（同目录）
- 相关记忆：`2026-07-23.md`、`2026-07-24.md`、MEMORY.md（"Findings → prompt only, never self-fix" 规则）
