# RND-236 QA 验收提示词（清理 `.qoder/repowiki` 自动生成 wiki）

> 用途：粘贴给 QA agent（或 Haisu 自行验收）。对应执行提示词 rnd-236-execution-prompt.md。
> QA 不修改实现；只读验证 + 智能路由判定。

## 0. 验收范围
- 验证 RND-236「清理 `.qoder/repowiki` 自动生成 wiki」结果。
- 注：RND-241 批（2026-07-26）已把 `.qoder/` 整体 `git rm --cached -r .qoder/` + 加 `.gitignore`，git 层面动作已完成。本 QA 聚焦**确认**该结果 + 可选本地清理核查。

## 1. 验收标准与验证命令

| # | 验收标准 | 验证命令 | 期望结果 |
|---|---|---|---|
| 1 | `.qoder/` 不再被 git 追踪 | `git ls-files .qoder/ \| wc -l` | 0 |
| 2 | `.gitignore` 含 `.qoder/` | `grep -n '^\.qoder/$' .gitignore` | 命中 1 行 |
| 3 | 忽略规则生效 | `git check-ignore -v .qoder/repowiki/x` | 命中 `.qoder/` 规则 |
| 4 | 无业务 / 配置误伤 | `git status --short \| grep -iE 'backend/|Makefile|scripts/|\.env' \| grep -v qoder` | 空（除预期 .qoder 删除外无其它） |
| 5 | 本地 IDE wiki 可选清理 | `ls -d .qoder 2>/dev/null && echo 仍在` | 本地仍在（推荐保留，IDE 可重生成）；或已 `rm -rf`（用户选择） |

## 2. 关键澄清
- RND-236 的“清理”本质是**移出 git 追踪**（IDE 自动生成 wiki，冗余）。本地 `.qoder/` 是否物理删除是**用户可选**动作，不影响开源安全（已 ignore）。
- 开发阶段 `git status` 会显示 `.qoder/...` 的 staged 删除——预期，非 FAIL。
- 若 `git ls-files .qoder/` 仍非 0 → 反馈开发 agent 重跑 `git rm --cached -r .qoder/`。
- 若 `git check-ignore .qoder/repowiki/x` 不命中 → 反馈开发 agent 在 `.gitignore` 补 `.qoder/` 一行。

## 3. 智能路由
- 标准 1–3 通过 + 标准 4 无业务误伤 → **NoOne**（验收通过；提醒 Haisu commit / push 后彻底干净）。
- 标准 1 / 2 / 3 失败 → 反馈**开发 agent** 补 `.gitignore` + `git rm --cached -r .qoder/`。
- 标准 4 失败（误伤业务文件）→ **立即上报 Haisu**，停止任何进一步操作。

## 4. QA 输出模板
```
## RND-236 QA Summary
Files changed: .gitignore (含 .qoder/)、git index (.qoder/ 移除追踪)
Acceptance:
- [x] .qoder/ 不再 tracked: pass
- [x] .gitignore 含 .qoder/: pass
- [x] 忽略生效: pass
- [x] 无业务误伤: pass
- [ ] 本地 .qoder 物理清理: 用户可选（保留 / 删除）
Commands run: <列出>
Manual verification: <列出>
Risks or gaps: 无 / 见上
No secrets introduced: confirmed
Only intentional files changed: confirmed
```
