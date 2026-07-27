# RND-241 QA 验收提示词（将 .workbuddy 排除出开源仓库）

> 用途：粘贴给 QA agent（或 Haisu 自行验收）。对应开发提示词 rnd-241-execution-prompt.md。
> QA 不修改实现；只读验证 + 智能路由判定。

## 0. 验收范围
- 验证 RND-241「将 `.workbuddy/` 排除出开源仓库」的执行结果。
- 不重做实现；仅验证开发 agent 的输出是否符合验收标准。

## 1. 验收标准与验证命令

| # | 验收标准 | 验证命令 | 期望结果 | 阶段 |
|---|---|---|---|---|
| 1 | `.gitignore` 含 `.workbuddy/` | `grep -n '^\.workbuddy/$' .gitignore` | 命中 1 行 | 开发阶段（agent） |
| 2 | `.workbuddy/` 不再被 git 追踪 | `git ls-files .workbuddy/` | 空输出（0 行） | 开发阶段（agent） |
| 3 | 忽略规则生效 | `git check-ignore -v .workbuddy/memory/MEMORY.md` | 命中 `.workbuddy/` 规则 | 开发阶段（agent） |
| 4 | 本地 `.workbuddy/` 目录完好 | `ls -la .workbuddy/ && ls .workbuddy/prompts/archive/ \| wc -l` | 目录存在，`archive/` 仍有约 24 个文件 | 开发阶段（agent） |
| 5 | `memory/` 与含真实域名的 prompt 均不再 tracked | `git ls-files .workbuddy/memory/ .workbuddy/prompts/rnd-233-execution-prompt.md .workbuddy/prompts/rnd-239-*.md` | 空输出 | 开发阶段（agent） |
| 6 | 提交后 `git status` 干净（.workbuddy 不出现） | 由 Haisu commit 后 `git status` | 无 `.workbuddy/` 条目 | **用户 commit 后** |

## 2. 关键澄清（避免误判）
- **阶段 6 是 Haisu commit / push 之后的验收**，不在 agent 阶段内。开发阶段（agent）只需满足标准 1–5。
- 开发阶段 `git status --short | grep -i workbuddy` **会**显示 staged 删除（`.workbuddy/...` deleted）——这是 `.gitignore` 已忽略、尚未 commit 的预期状态，**不代表失败**。判定失败的真正信号是：`.workbuddy/` 以 **untracked** 形式重新出现（`?? .workbuddy/...`），或 `git ls-files` 仍返回路径。
- 不要因为「git status 还显示 workbuddy 删除」而判 FAIL。

## 3. 智能路由判定
- 全部标准 1–5 通过 → **NoOne**（验收通过；提醒 Haisu 执行最终 commit / push 后标准 6 自验）。
- 标准 1 失败（.gitignore 缺条目）→ 反馈**开发 agent** 补 `.workbuddy/` 一行（参考执行提示词步骤 B）。
- 标准 2 / 3 / 5 失败（仍有 tracked 文件）→ 反馈**开发 agent** 重跑 `git rm --cached -r .workbuddy/`（注意必须在 .gitignore 条目**之前**执行，或加 `--force`，参考执行提示词步骤 A 顺序）。
- 标准 4 失败（本地目录 / 文件丢失）→ **立即上报 Haisu**，存在本地文件不可恢复风险，严禁任何进一步删除动作。
- 无测试代码问题（纯 git 操作，不适用测试代码自修分支）。

## 4. 安全红线
- 若发现本地 `.workbuddy/` 被实际删除（非仅 index 移除），**立即停止**并上报，不得执行任何 `rm` / `clean`。
- 安全收益确认：排除后 `.workbuddy/memory/` 不再进仓库，正是预期的安全脱敏收益。

## 5. QA 输出模板
```
## RND-241 QA Summary
Files changed: .gitignore (追加 .workbuddy/)、git index (移除 .workbuddy/ 追踪)
Acceptance criteria:
- [x] .gitignore 含 .workbuddy/: pass
- [x] git ls-files .workbuddy/ 返回 0: pass
- [x] 忽略规则生效: pass
- [x] 本地 .workbuddy/ 完好(archive/ 保留): pass
- [x] memory/ 与含域名 prompt 不再 tracked: pass
- [ ] commit 后 git status 干净: pending (待 Haisu commit)
Commands run: <列出>
Manual verification: <列出>
Risks or gaps: 无 / 见上
No secrets introduced: confirmed
Only intentional files changed: confirmed
```
