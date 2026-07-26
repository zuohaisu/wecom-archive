# RND-236 执行提示词（清理 `.qoder/repowiki` 自动生成 wiki）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。关联 epic：RND-232。
> 不自行 commit / push。

## 0. 任务与来源
- RND-236「清理 `.qoder/repowiki` 自动生成 wiki」，父 epic RND-232。
- 现状：`.qoder/repowiki/` 共 **193 个已跟踪文件**，未被 `.gitignore`，属 IDE（Qoder）自动生成、冗余、且含项目细节，发布前应移除。

## 1. 执行步骤

### 阶段一：确认范围（只读）
- `git ls-files .qoder | grep -v '^.qoder/repowiki'` 应为空（确认 `.qoder` 下只有 repowiki，无项目规则等需保留内容）。若非空，先列出差异再决定。
- 确认 `.qoder` 不被运行时依赖（仅是 IDE wiki）。

### 阶段二：移除并忽略
- `git rm -r --cached .qoder/repowiki`（或整个 `.qoder` 若阶段一确认安全）。
- 在 `.gitignore` 追加 `.qoder/`（RND-237 会复核 `.gitignore`，可在此一并加，避免重复）。
- 本地物理目录可保留或 `rm -rf .qoder`（若阶段一确认安全）；推荐保留本地、仅从跟踪移除，避免 IDE 重新生成时噪音。

### 阶段三：验证
- `git status` 显示约 193 个文件 deleted（暂存）。
- `git ls-files .qoder | wc -l` → 0。
- `grep -rn "repowiki" . --exclude-dir=.git` 应无跟踪引用（若有 CI/脚本引用需同步改，否则跳过）。

## 2. 硬性约束
- 仅删自动生成 wiki；不删项目源码 / 配置。
- 不提交 / 推送。
- 若阶段一发现 `.qoder` 下有非 repowiki 的重要文件（如团队规则），**停下告知 Haisu**，不要误删。

## 3. 收尾动作
- Linear 评论：删除文件数 + 已加 `.gitignore`。保留 commit。
