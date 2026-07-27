# RND-241 执行提示词（将 .workbuddy 排除出开源仓库并归档已完成提示词）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。父任务 RND-237，隶属 epic RND-232（开源准备）。
> 不自行 commit / push；最终 commit / push 由 Haisu 操作。

## 0. 任务与来源
- RND-241「将 .workbuddy 排除出开源仓库并归档已完成提示词」，父任务 RND-237，epic RND-232。
- 目标：`.workbuddy/` 整体不进开源仓库（类比 `.claude/`、`.cursor/`）。原因：目录内含内部项目记忆 `memory/`（内部决策 + `crowntime` 引用）与 `prompts/` 中含真实域名的文件（如 RND-233 域名映射、RND-239 官网迁移计划）。
- 已完成（Done）issue 的提示词已归档至 `.workbuddy/prompts/archive/`（约 24 个，本地保留不删）。

## 1. 前置只读盘点（执行前先确认）
- 当前 `.workbuddy/` 被 git 追踪的文件（已确认）：
  - `memory/2026-07-21.md` ~ `memory/2026-07-26.md`、`memory/MEMORY.md`（共 7 个）
  - `prompts/archive/` 下约 24 个已归档提示词
  - `prompts/` 下未归档若干（含 `rnd-233-execution-prompt.md`、`rnd-237-execution-prompt.md`、`rnd-239-*.md` 等含真实域名 / 内部标识的文件）
- 当前 `.gitignore` **不含** `.workbuddy/` 条目（末尾为 `.DS_Store`）。
- 校验命令：`git ls-files .workbuddy/`（执行前应返回约 30+ 行追踪路径）。

## 2. 执行步骤（严格按顺序）

### 步骤 A：取消 git 追踪整个 .workbuddy/（仅 --cached，不删本地文件）
```bash
# 关键点：此步必须在「步骤 B 追加 .gitignore 条目」之前执行。
# 否则路径已被 ignore，git rm --cached 会被拒绝（需 --force，应避免）。
git rm --cached -r .workbuddy/
```
- 预期结果：`.workbuddy/` 下所有文件从 index 移除，工作区文件**原样保留**。
- 不删本地校验：`ls -la .workbuddy/` 应仍列出 `memory/`、`prompts/`（含 `archive/`）等子目录。

### 步骤 B：在 .gitignore 追加忽略规则
```bash
# 在 .gitignore 末尾追加（.DS_Store 之后）。
# 若 RND-237 已先改过 .gitignore（含 .qoder/ 等），只追加 .workbuddy/，勿删、勿重复。
grep -q '^\.workbuddy/$' .gitignore || printf '\n# WorkBuddy agent workspace — internal only, not for open source\n.workbuddy/\n' >> .gitignore
```
- 不要用 `>` 覆盖文件；用 `>>` 或 Edit 工具在末尾追加。
- 若因误序（已先加 ignore）导致步骤 A 被拒，改用 `git rm --cached -r --force .workbuddy/`，但优先保持「先 rm 后 ignore」顺序。

### 步骤 C：验证（详见 QA 提示词 rnd-241-qa-prompt.md）
```bash
git ls-files .workbuddy/                    # 期望：空输出（0 行）
git check-ignore -v .workbuddy/memory/MEMORY.md   # 期望：命中 .gitignore 的 .workbuddy/ 规则
ls -la .workbuddy/                         # 期望：目录完好，archive/ 与 memory/ 仍在
git status --short | grep -i workbuddy     # 期望：仅 staged 删除（待 Haisu commit），无 untracked 重新出现
```

## 3. 与 RND-237 的协调
- RND-241 与 RND-237 的 LICENSE / README / .gitignore 复核**一并执行**。
- `.gitignore` 的 `.qoder/`、`\.env` 等条目由 RND-237 负责；RND-241 **仅追加** `.workbuddy/`，互不覆盖。
- 若两任务由不同 agent 顺序执行，后者应先确认当前 `.gitignore` 状态再追加，避免重复或冲突。

## 4. 硬性约束
- **不 commit / push**：`git rm --cached` 仅为提交做准备；最终 commit / push 由 Haisu 操作。
- **绝删本地文件**：只使用 `--cached`，禁止 `git rm`（无 --cached）、`git clean`、`rm -rf .workbuddy`。
- `prompts/archive/` 下约 24 个已归档提示词**本地保留不删**。
- 不影响 RND-233 / 234 / 236 / 237 其他去标识化工作（这些改动业务代码 / 配置，与 .workbuddy 排除正交）。
- 不改动业务代码、不引入依赖、不动 RND-212 重构 epic。

## 5. 收尾动作
- Linear 评论：说明已将 `.workbuddy/` 移出 git 追踪（index 移除 + .gitignore 忽略），本地目录完好，约 N 个文件取消 tracked。保留 commit 由 Haisu 操作。
- 不自行创建 commit。
