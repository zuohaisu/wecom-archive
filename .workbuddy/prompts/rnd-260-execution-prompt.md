# RND-260 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-260）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户 Haisu 本人操作）。只改工作树（含 `.gitignore`），交用户 Review。

## 一、任务（一句话）
在**私有仓**（双仓库发布模式）撤销 RND-241 / remove-process-docs 对内部目录的 untrack：修改 `.gitignore` 解除忽略规则并用 `git add` 恢复跟踪，使多台开发机可通过 git 同步这些内部文档。**绝不**执行 `git commit`/`push`，也**不做** `git rm --cached`。

## 二、精确落点 / 根因（已定位，附 文件:行号）

### 根因 A — .gitignore 新增忽略规则挡住了内部目录
- `.gitignore` **L249–268**（含注释块）新增了以下忽略规则：
  - L250 `.workbuddy/`
  - L253 `.qoder/`、L254 `.trae/`
  - L257 `ppt-365-intro/` → 需改为仅忽略 `ppt-365-intro/.slidep/` 与 `ppt-365-intro/.cache/`
  - L261 `static_site/company_homepage/`
  - L264 `TASK_TRACKING.md`、L265 `PROJECT_DOCUMENTATION_REPORT.md`
  - （L267–268 `docs/ip-protection-plan-365huifang.md` **保留忽略，本任务不改动**）
- 这些规则是在以下提交之后加入的：
  - `8a1a7d9`（RND-241）：untrack `.workbuddy/`，约 50 文件
  - `3fee9bd`（remove process docs）：untrack `.qoder/`（约 193）、`ppt-365-intro/`（约 27）、`.trae/`（约 1）、`static_site/company_homepage/`（约 3）、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md`，共约 228 文件变更

### 根因 B — 文件仍在磁盘，仅被忽略
- `git ls-files | grep -c '^\.workbuddy/'` 当前为 `0`（应 > 0）
- 所有约 278 个文件仍在工作树（属 `--cached` 式移除，无数据丢失），只需解除忽略 + `git add` 即可恢复跟踪

## 三、阶段一：复现 + 测量（RED，量化基线）
1. 确认当前被忽略状态（记录基线数字）：
   - `git ls-files | grep -c '^\.workbuddy/'` → 期望基线 `0`
   - 同理对 `.qoder/`（约 193）、`.trae/`（约 1）、`ppt-365-intro/`（约 27，注意不含 `.slidep/`/`.cache/` 子项）、`static_site/company_homepage/`（约 3）、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md` 计数，基线均为 `0`
2. 确认磁盘文件仍在：
   - `ls .workbuddy .qoder .trae ppt-365-intro static_site/company_homepage TASK_TRACKING.md PROJECT_DOCUMENTATION_REPORT.md` 均存在
3. 记录 RED 基线数字，供 GREEN 对比

## 四、阶段二：实现（GREEN，最小变更）

### 路 A — 修改 .gitignore（L249–268 区域）
- 删除以下忽略行（连同其注释块）：`.workbuddy/`、`.qoder/`、`.trae/`、`static_site/company_homepage/`、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md`
- 将 `ppt-365-intro/`（L257）替换为两行（IDE 垃圾仍不跟踪，演示源文件恢复跟踪）：
  ```
  ppt-365-intro/.slidep/
  ppt-365-intro/.cache/
  ```
- **保留** L267–268 `docs/ip-protection-plan-365huifang.md` 的忽略不变
- 不要动 L1–L248 的既有 Python/SSL/IDE 规则
- **禁止** `git rm --cached`（双仓库模式下私仓历史无所谓，但本任务只解除忽略，不删历史）

### 路 B — 重新加回跟踪
- `git add .workbuddy .qoder .trae ppt-365-intro static_site/company_homepage TASK_TRACKING.md PROJECT_DOCUMENTATION_REPORT.md`
- 验证 `ppt-365-intro/.slidep/` 与 `ppt-365-intro/.cache/` 仍被忽略：
  - `git check-ignore ppt-365-intro/.slidep/foo` 应命中（返回路径）
  - `git check-ignore ppt-365-intro/.cache/foo` 应命中
  - `git check-ignore ppt-365-intro/some-deck.pptx` 不应命中

## 五、阶段三：验证（GREEN + 回归）
1. 功能验证：重跑阶段一计数
   - `git ls-files | grep -c '^\.workbuddy/'` → GREEN 应 `> 0`（约 50）
   - `.qoder/`（约 193）、`.trae/`（约 1）、`ppt-365-intro/`（约 27，不含缓存）、`static_site/company_homepage/`（约 3）、`TASK_TRACKING.md`、`PROJECT_DOCUMENTATION_REPORT.md` 均 `> 0`
2. 不触发无关改动：`git status` 应只显示这些恢复跟踪的文件，无其他意外
3. **不执行 commit / push**（硬约束）

## 六、硬约束（违反即判失败）
- **不执行 `git commit` / `git push`**（用户本人提交）
- **不做 `git rm --cached`**（双仓库模式，私仓无需清洗历史）
- 不改 L1–L248 的既有忽略规则；不改 `docs/ip-protection-plan-365huifang.md` 的忽略
- 公开仓的排除职责由 RND-237 发布导出脚本承接（本任务不动公开仓）
- 只解除忽略 + `git add`，不改任何源代码 / 文档正文
- 与相关工作树改动无冲突前提下最小化改动；冲突则停下报告

## 七、收尾（交付物）
向用户交付：
- RED 基线计数（各目录 `git ls-files | grep -c` 均为 0）
- GREEN 计数（各目录均 > 0）
- `git status` 摘要
- `.gitignore` 改动 diff
- 未提交声明（请 Haisu 本人 `git commit`）
