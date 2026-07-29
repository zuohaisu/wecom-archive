# RND-239 执行提示词（前端营销网站 · 纯静态 · 品牌化）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。关联 epic：RND-232；RND-234 随本任务收口。
> 策略已定：**纯静态 HTML/CSS/JS**、**产品/品牌身份为主**、**新站取代旧 company_homepage**、**先出本提示词不写代码**。
> 不自行 commit / push。

## 0. 任务与来源
- Linear：RND-239「构建前端营销网站（介绍系统 + 含官网，保留项目内）」，父 epic RND-232。
- 目标：一个**高级感、轻量、纯静态**的营销站，介绍「365 企微会话存档」，并作为官网。取代 `static_site/company_homepage`。

## 1. 技术决策（已锁定）
- **纯静态**：HTML + CSS + 原生 JS；复用并扩展现有 `static_site/company_homepage/style.css` 的设计语言（深浅主题、玻璃拟态、磁吸）。不引入后端 / 框架 / 构建链（除非仅 CDN 轻量库，且需用户确认）。
- **目录**：新站落地 `static_site/`（取代 company_homepage）。建议结构：`static_site/index.html` + `static_site/assets/{style.css, app.js, hero.(svg|canvas)}` + 可选 `static_site/pages/*.html`（或单页锚点）。实现完成后删除 `static_site/company_homepage/`。
- **部署**：延续现有部署流水线对 `static_site` 的服务；确保替换后站点仍可被部署（不改部署脚本语义，仅换内容）。

## 2. 页面结构（建议单页 + 锚点导航）
1. **Hero**：产品名「365 企微会话存档」+ 一句话价值主张 + 主 CTA（GitHub / 体验入口）+ 可选轻量 Three.js / Canvas 背景（粒子或网格；性能优先，尊重 `prefers-reduced-motion`）。
2. **能力与场景**：卡片网格（会话存档 / 全文检索 / 媒体归档 / 合规审计 / 多租户 / 撤回对账…），hover 玻璃拟态 + 磁吸。
3. **系统架构**：SVG 架构图（前端 + 后端 API + 存储 + 企微回调），简洁可读。
4. **开源与部署**：GitHub 链接、部署说明（指向 `.env.example` 与 `ARCHIVE_DOMAIN` 环境变量策略，呼应 RND-233）、License 段落（呼应 RND-237）。
5. **联系 / 品牌**：产品联系入口（邮箱 / Issues）。**严禁**展示法律实体敏感字段：ICP 号、公安网安备编号、真实地址 / 电话 / 邮箱；公司名用品牌名而非「深圳康冠时代科技有限公司」全称 + 敏感标识。

## 3. 设计语言（高级感，Senior Developer 要求）
- **主题**：light / dark / system 三态切换，平滑且即时（无闪烁）；跟随系统偏好。
- **质感**：玻璃拟态卡片（backdrop-filter blur + 低透白边）、精致排版层级、慷慨留白。
- **交互**：磁吸按钮（cursor 吸引）、滚动入场动画（IntersectionObserver，60fps）、hover 微交互。
- **性能**：首屏 < 1.5s；动画 60fps；资源按需懒加载；CDN 库最小化。
- **响应式**：移动优先；断点适配。
- **可访问性**：WCAG 2.1 AA；语义化标签、对比度达标、`prefers-reduced-motion` 降级。
- **品牌**：统一使用产品 / 品牌视觉（「365 企微会话存档」），无真实法律实体敏感信息。

## 4. 与 RND-234 的关系（收口）
- 新站按「产品 / 品牌身份」落地即满足 RND-234：不出现法律实体敏感字段。
- 删除旧 `static_site/company_homepage/` 后，公开仓库不再含原真实身份页面 → RND-234 标记完成。

## 5. 执行步骤（最小改动、保持可部署）
### 阶段一：骨架与内容（静态）
- 在 `static_site/` 建新站文件（index.html + assets）；首屏 + 各锚点区块。
- 复用现有 `style.css` 设计 token，新增营销站专属样式（玻璃拟态、动画）于 `assets/style.css`（或扩展原文件）。
- 写原生 `app.js`：主题切换、磁吸、IntersectionObserver 入场、移动端菜单。

### 阶段二：品牌化与去标识
- 全站使用产品名；检查并**不**写入 ICP 号 / 公安网安备 / 真实地址电话邮箱（呼应 RND-234）。
- 与 RND-233 协调：若页面引用部署域名，用 `example.com` / 环境变量说明，不写 `crowntime.cn`。

### 阶段三：旧站收口 + 验证
- 确认 `static_site/company_homepage/` 已被 `.gitignore` 忽略（`static_site/company_homepage/` 一行）+ 本轮（RND-241 批）已 `git rm --cached`，故**不进开源库**（RND-234 收口）；本地目录可保留（参考）或 `rm -rf` 删除（用户选择）。新站文件落在 `static_site/`（不被忽略）即正常 tracked。
- 本地 `python -m http.server` 预览；手测主题切换 / 响应式 / 动画 / 可访问性。
- `grep -rn "crowntime\|康冠\|ICP备\|公安网安备" static_site/ --exclude-dir=company_homepage` 对**新站部分**应无结果（company_homepage/ 本地含身份是预期的，已被 `.gitignore` 忽略、不进开源库）。

## 6. 硬性约束
- 纯静态；不引后端 / 新依赖（CDN 轻量库需用户确认）。
- 不改动后端业务代码 / 部署脚本语义；仅替换 `static_site` 内容。
- 不提交 / 推送（Haisu 操作）。
- 不破坏现有部署（static_site 仍被服务）。

## 7. 收尾动作
- Linear 评论：页面清单 + 品牌化处理 + 旧站已删 + 预览方式。RND-239 / RND-234 标记完成（待用户 commit）。
