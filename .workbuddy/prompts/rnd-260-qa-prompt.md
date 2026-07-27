# RND-260 QA / 验收 agent 提示词
> 面向独立测试 / QA agent。只读言、不改实现、不 commit / push。
> 验收对象：开发 agent 按 `rnd-260-execution-prompt.md` 产出的改动（仅 `.gitignore` + `git add`）。

## 一、验收目标
确认内部目录已恢复 git 跟踪、`.gitignore` 正确解除忽略（且 `ppt-365-intro` 的 `.slidep/`/`.cache/` 仍忽略）、未误删历史、未 commit。

## 二、逐条验收清单（PASS/FAIL，附证据）

### .gitignore 修改（根因 A）
- [ ] A1 `.gitignore` 不再包含 `.workbuddy/`、`.qoder/`、`.trae/`、`static_site/company_homepage/`、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md` 的忽略行 —— 证据：grep / Read `.gitignore`
- [ ] A2 `ppt-365-intro/` 已改为 `ppt-365-intro/.slidep/` 与 `ppt-365-intro/.cache/` 两行 —— 证据：Read `.gitignore`
- [ ] A3 `docs/ip-protection-plan-365huifang.md` 仍被忽略（L267–268 未动） —— 证据：`git check-ignore docs/ip-protection-plan-365huifang.md` 命中
- [ ] A4 L1–L248 既有规则未被改动 —— 证据：`git diff .gitignore` 仅含 L249+ 区域

### 跟踪恢复（根因 B）
- [ ] B1 `git ls-files | grep -c '^\.workbuddy/'` > 0（约 50） —— 证据：命令输出
- [ ] B2 `.qoder/`（约 193）、`.trae/`（约 1）恢复跟踪 —— 证据：计数 > 0
- [ ] B3 `ppt-365-intro/` 非缓存文件恢复跟踪（约 27，不含 `.slidep/`/`.cache/`） —— 证据：计数 + `git check-ignore` 反证
- [ ] B4 `static_site/company_homepage/`（约 3）、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md` 恢复跟踪 —— 证据：计数 > 0
- [ ] B5 `ppt-365-intro/.slidep/` 与 `ppt-365-intro/.cache/` 仍被忽略 —— 证据：`git check-ignore` 命中

### 全局契约
- [ ] C1 未执行 commit / push —— 证据：`git status` 显示待提交、无远程推送；或询问用户确认
- [ ] C2 未做 `git rm --cached` —— 证据：磁盘文件完整、git 历史无 `rm --cached`；`git ls-files` 计数与开发agent 申报一致
- [ ] C3 磁盘文件无丢失（约 278 文件仍在） —— 证据：`ls` 抽查关键目录

## 三、回归套件（必须全绿）
本任务无代码改动，无需 `make verify`；但需确认：
- `git status` 仅含恢复跟踪的内部文件，无业务源码 / 文档意外改动
- 既有 `.gitignore` Python/SSL 规则（L1–L248）功能不变（用 `git check-ignore` 抽样验证 `.env`、`.venv`、`*.key` 仍被忽略）

## 四、智能路由判定（每轮必给）
- `.gitignore` 改错 / 漏改 → 反馈开发 agent 修正，附错误 + 失败点 + 期望；不自行改实现
- 仅验证命令（如 `git check-ignore` 参数）写错 → 可自行修正验证命令（须标注）
- 全部通过 → 报告 SUCCESS，附 RED→GREEN 计数对比

最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-260 验收结论：PASS / FAIL
RED 基线：.workbuddy/ 0, .qoder/ 0, .trae/ 0, ppt-365-intro/ 0, static_site/company_homepage/ 0, TASK_TRACKING.md 0, PROJECT_DOCUMENTATION_REPORT.md 0
GREEN：.workbuddy/ >0, .qoder/ >0, ...
契约：未 commit / 未 rm --cached / 磁盘完整 / L1–L248 不变
遗留：___
```
