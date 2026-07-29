# RND-239 验收提示词（前端营销网站 · 纯静态 · 品牌化）

> 独立验收用，读言不改实现。关联：RND-239 / RND-234（收口）。

## 验收清单
- [ ] **纯静态**：站点为 HTML/CSS/JS，本地 `python -m http.server` 可完整预览；无后端 / 构建依赖（CDN 轻量库已确认）。
- [ ] **页面齐全**：Hero / 能力与场景 / 架构 / 开源与部署 / 联系品牌 五区块均在。
- [ ] **品牌正确（RND-234）**：全站使用产品 / 品牌身份「365 企微会话存档」；**未出现** ICP 号、公安网安备编号、真实地址 / 电话 / 邮箱、「深圳康冠时代科技有限公司」全称 + 敏感标识。
- [ ] **去标识（RND-233 协同）**：页面未硬编码 `crowntime.cn`；如涉部署域名用 `example.com` / 环境变量说明。
- [ ] **主题切换**：light / dark / system 三态可用、平滑、跟随系统；无闪烁。
- [ ] **设计质感**：玻璃拟态、磁吸按钮、滚动入场动画存在且 60fps；尊重 `prefers-reduced-motion`。
- [ ] **响应式**：移动 / 平板 / 桌面断点正常。
- [ ] **可访问性**：语义化标签、对比度达标、键盘可达。
- [ ] **旧站不进库（RND-234 收口）**：`static_site/company_homepage/` 已被 `.gitignore` 忽略（`static_site/company_homepage/`）+ 本轮 `git rm --cached`，不进开源库；`git check-ignore static_site/company_homepage/README.md` 应命中。新站文件 `static_site/*` 正常 tracked。`grep` 敏感字段验证应 `--exclude-dir=company_homepage`（本地旧站含身份是预期的）。
- [ ] **不破坏部署**：现有部署流水线仍服务 `static_site`；替换后站点可部署。
- [ ] **无新依赖 / 不提交**：未引入未确认依赖；未 commit / push（Haisu 操作）。
- [ ] **性能**：首屏 < 1.5s（本地静态）。

## 验收方式
- 本地起静态服务器手测上述项；逐项打勾。
- 不通过项列明文件 / 现象，退回开发 agent 修复；不自行改实现、不提交。
