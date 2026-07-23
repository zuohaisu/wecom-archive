# RND-229 — focusPending / focusMsgId 测试修复 执行提示词

> PM 交付物（execution prompt）。PM 不修改任何源码，仅提供此提示词给开发 agent / 用户执行。
> 状态：**已应用（2026-07-24，由 Developer 直接修复）**。修复点不在 `_DOM_PREAMBLE`，而在 `_state_vars_block()`（该函数输出位于 `_bundle()` 的顶层作用域，早于测试的 async IIFE）；在其中补 `var focusMsgId = null; var focusPending = false;`。`make verify` 已全绿（2069 passed / 3 skipped）。本提示词保留作根因记录，勿重复应用。

## 背景（Context）
RND-229（前端：独立搜索结果页 + 跳转消息高亮/滚动）在 `backend/app/main.py` 中新增了两处顶层全局变量声明，供 `fetchTimelinePage` 等新逻辑引用：

- `var focusMsgId = null;` （约 `main.py:2509`）
- `var focusPending = false;` （约 `main.py:2515`）

同时 `fetchTimelinePage`（约 `main.py:894`）及搜索跳转相关代码会读取这两个变量。

## 问题现象（Root Cause）
`backend/tests/test_rnd_206_qa_fixes.py` 通过 `_DOM_PREAMBLE`（约 L190）构造一个 JS 运行环境，再用 `_bundle()`（约 L61）从 `main.py` 抽取函数定义灌进去跑测试。

关键点：`_bundle()` 只抽取「函数定义」，**不会把 `main.py` 顶层 `var focusMsgId` / `var focusPending` 这两个全局变量带进测试环境**。而 RND-229 的 `fetchTimelinePage` 现在依赖这两个变量 → 测试运行时报 `focusPending is not defined`（或 `focusMsgId is not defined`），导致该测试失败，进而 `make verify` 不过。

## 修复方向（Fix）
在测试侧补齐这两个全局声明，使测试 JS 环境里存在这两个变量（取值与 `main.py` 保持一致）。

在 `backend/tests/test_rnd_206_qa_fixes.py` 的 `_DOM_PREAMBLE` 字符串中，加入：

```js
var focusMsgId = null;
var focusPending = false;
```

位置建议：放在 `_DOM_PREAMBLE` 顶部、其它全局/初始声明附近即可（当前 `_DOM_PREAMBLE` 开头已有 `var fetchCalls = [];` 等全局声明，`_bundle()` 只抽函数，不会重复声明这两个变量，加在 preamble 里最安全）。

## 可选：复用已 draft 的修复（谨慎）
2026-07-23 这版修复曾由 agent 写好并随工作区一起暂存在 `stash@{0}`（一次 `git reset` 前）。如需参考：

```
git stash show -p stash@{0} -- backend/tests/test_rnd_206_qa_fixes.py
```

⚠️ **注意**：`stash@{0}` 同时含一份 `2026-07-23.md` 的增量，与当前工作区可能冲突——**只取 test 文件的 diff，不要整体 `git stash pop`**，避免把 memory 文件也带进来。最干净的做法是直接按上面的「修复方向」手动改。

## 验收标准（Acceptance Criteria）
- [ ] `backend/tests/test_rnd_206_qa_fixes.py` 相关用例通过（不再报 `focusPending` / `focusMsgId` is not defined）
- [ ] 运行 `make verify` 整体通过（或至少该测试所在的 pytest 通过）
- [ ] 不改动 RND-229 业务逻辑，仅补齐测试环境的全局声明
- [ ] 不引入新的 lint / 类型错误

## 约束（重要）
- 本任务**只动 `backend/tests/test_rnd_206_qa_fixes.py` 一个文件**，不要碰 `backend/app/main.py` 或其它源码。
- **不要 commit**：用户会自行核验并通过 GitHub PR 合并。完成后把结果告知用户 / PM 即可。
- 若对 `main.py` 中变量真实声明位置/取值不确定，先读文件确认，再写测试——保持测试里的声明与源码完全一致。

## 参考
- 分支：`feature/rnd-229`
- 相关记忆：`2026-07-23.md`、`2026-07-24.md`、MEMORY.md（"Findings → prompt only, never self-fix" 规则）
