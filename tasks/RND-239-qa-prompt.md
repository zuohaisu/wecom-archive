# RND-239 QA 提示词（营销官网 + 静态 Demo）

> 独立验收时以 Linear RND-239 最新评论和本文件为准。早期“隐藏法律实体、删除 `company_homepage`”规则已作废，不能据此判错。

## 验收清单

### 官网与套餐

- [ ] `static_site/company_homepage/` 仍是官网源目录；未删除、忽略或迁移，部署脚本语义未改。
- [ ] 所有“预约演示”均改为“查看 Demo”，链接到可访问的 `demo/`。
- [ ] 套餐文案准确：**99 元/年含 5GB、不限座席、超出 5GB 每增加 1GB 加 1 元/GB/月**。
- [ ] 页面清楚说明：企业微信会话存档接口开通及相关官方费用另计。
- [ ] 不声明免费套餐、支付、自动开通、七牛官方分销/合作或未经证实的法律/合规结果。
- [ ] 定价区、FAQ、计费对比、SEO title/description/H1/H2 与基础结构化数据存在。
- [ ] CTA 有可用落点：Demo 为静态 URL，咨询为 `mailto`；没有伪表单或无行为预约按钮。

### 品牌、法律与公开字段

- [ ] 产品名为“康冠时代企业微信会话存档”；“365”不作为产品名出现。
- [ ] 公司主体、ICP、公安网安备和公开邮箱保留。
- [ ] 详细地址、电话号码不在官网可见内容或 metadata 中出现。
- [ ] “第三方独立产品 / 非腾讯官方”的清晰声明存在。
- [ ] 不添加密钥、生产数据、隐私数据或真实客户数据。

### Demo

- [ ] `demo/index.html`、`conversations.html`、`search.html`、`contacts.html`、`media.html`、`analytics.html`、`audit-log.html` 全部存在并互相可导航。
- [ ] Demo 每页明确标识静态演示环境、无生产连接、使用虚构数据。
- [ ] Demo 不需要登录，不请求 API，不执行下载/导出/写入；静态操作仅提示说明。
- [ ] 所有展示的企业、人员、消息、附件、ID 和统计数字为合成示例；无生产聊天导出或真实媒体。
- [ ] Demo 仅展示已存在或票面确认能力；不把尚未落地的昵称追踪、平台超管、支付或自动开通包装成已可用功能。
- [ ] 本地交互可用：主题切换、会话切换、搜索本地筛选、页面导航；键盘焦点可见。
- [ ] 主题仅在当前页面 DOM 生命周期内切换；刷新恢复亮色默认值，Demo Shell 不读取或写入 `localStorage`、`sessionStorage`、Cookie、IndexedDB 或其他持久化浏览器状态。

### 技术、回归与范围

- [ ] 纯 HTML/CSS/原生 JS；无新框架、CDN、构建依赖、后端路由或部署配置改动。
- [ ] `python3 -m http.server` 下官网和各 Demo 页面、CSS、JS、图片、品牌资产均正常加载。
- [ ] `node --check` 通过所有 Demo JS；`node tasks/RND-239-theme-persistence-smoke.mjs` 通过；HTML 可解析；内部相对链接均指向存在文件。
- [ ] `git diff --check` 通过；`make verify` 在环境可用时通过。
- [ ] 改动仅限 `static_site/company_homepage/**` 和 `tasks/RND-239-*.md`；不含 auth、外部联系人后端、Worker、systemd、CI/CD、生产配置文件。
- [ ] 未 commit、push、创建 PR 或部署生产。

## 报告格式

```text
RND-239 QA：PASS / FAIL
官网与套餐：...
品牌与法律字段：...
Demo：...
本地验证命令：...
改动文件：...
范围与安全：...
遗留风险：...
```
