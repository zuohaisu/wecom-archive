# RND-239 执行提示词（营销官网 + 静态 Demo）

> 当前批准版本。以 Linear RND-239 最新评论和 Haisu 2026-08-04 的批准为准；不采用早期“品牌身份 B / 删除 `company_homepage` / 隐藏公司主体”的过期说明。

## 一句话任务

最小补齐现有 `static_site/company_homepage/` 官网的按空间计费、FAQ、SEO/GEO、真实 CTA 和品牌字段，同时新增 `/demo/` 下的一套只读、无后端、全合成数据的产品静态 Demo。

## 不可变决策

- 产品名：康冠时代企业微信会话存档 / Crowntime WeCom Archive。
- 官网入口和部署源目录：`static_site/company_homepage/`，必须继续保留并被部署脚本复制。
- 套餐：99 元/年含 5GB 存储空间；不限座席；超出 5GB 后每增加 1GB 加 1 元/GB/月。
- 企业微信会话存档接口开通与相关官方费用另计。
- 保留公司主体、ICP、公安网安备和公开联系邮箱；删除详细地址和电话。
- 保留“第三方独立产品，非腾讯官方”的清晰声明。
- 对外不得声称为七牛官方分销、合作或授权服务。
- “预约演示”一律改为“查看 Demo”，链接到 `demo/`；咨询必须使用真实 `mailto`，不能是无行为按钮。

## 实现范围

### 官网

修改 `static_site/company_homepage/index.html`、`style.css`、`site.webmanifest`：

1. Hero 和导航突出按空间计费，主 CTA 进入 `demo/`。
2. 增加定价区：基础方案、超额空间规则、官方接口费用说明、按座席/按空间的中性对比。
3. 增加可展开 FAQ，含座席、官方费用、空间占用、超额、多租户、访问与审计、自部署问题。
4. 更新 title、description、H1/H2 与 `SoftwareApplication` / `FAQPage` 结构化数据；不得夸大合规或法律结论。
5. 页脚移除详细地址/电话，保留公司、备案、邮箱和免责声明。

### Demo

新增 `static_site/company_homepage/demo/`：

```text
demo/
├── index.html
├── conversations.html
├── search.html
├── contacts.html
├── media.html
├── analytics.html
├── audit-log.html
└── assets/
    ├── demo.css
    ├── demo-data.js
    └── demo-shell.js
```

- 所有页面共用导航、主题切换和本地交互。
- `conversations.html` 可切换虚构会话；`search.html` 只在浏览器内筛选虚构消息；其他动作只展示“静态演示，不执行操作”提示。
- 每页必须有“静态演示、无生产连接、数据全虚构”的明确标识。
- 不使用后端模板、认证、API、生产图片、真实聊天导出、真实客户信息、追踪脚本或新依赖。
- 设计参考可来自 `design/ui-v1/` 的视觉 tokens，但不得将其尚未实现的页面直接作为产品功能宣称。

### 文档

- 更新 `static_site/company_homepage/README.md`，说明 `/demo/`、部署复制行为与内容/数据 guardrails。
- 同步本计划和 `tasks/RND-239-qa-prompt.md`，避免旧 QA 按已作废的品牌 B 规则验收。

## 明确排除

- 后端、FastAPI 路由、认证、OAuth、数据库、Worker、systemd、CI/CD、DNS/CDN、生产部署。
- 支付、账单、自动开通、表单收集、CMS、真实试用账号。
- RND-170、RND-321、RND-341、RND-343 的工作树文件。
- git commit、push、PR、生产操作。

## 开发后验证

1. `python3 -m http.server` 打开 `/` 和 `/demo/`；检查所有相对链接、JS、CSS、品牌资产。
2. 检查各 Demo 页面在桌面和窄视口可访问；主题切换、会话切换、搜索筛选和静态操作提示正常。
3. grep：无“预约演示”、无“99 元/月”、无详细地址/电话；价格和官方接口费用说明存在。
4. grep：Demo 不含网络请求、生产数据或凭证；页面明确是静态演示。
5. `git diff --check`、HTML/JS 语法检查、相关静态链接检查；运行 `make verify`（环境可用时）。
6. 确认仅预期的 `static_site/company_homepage/**` 与 `tasks/RND-239-*.md` 改动，无 commit/push。
