# RND-242 开发 agent 执行提示词 —— 产品更名「康冠时代」企业微信会话存档（品牌公开化）

> 面向开发 agent（单人端到端实现 RND-242）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

把产品品牌从「365 WeCom Archive / 365企微会话存档」更名为 **「康冠时代」企业微信会话存档**（英文 **Crowntime WeCom Archive**，仓库 slug **`crowntime-wecom-archive`**），只动 A 层（展示/品牌），**严禁触碰 B 层（生产基础设施路径）**，官网撤地址电话、留邮箱，并加"非腾讯官方"免责声明。

## 二、决策背景（已全部拍板，不要再问）

- 「康冠时代」= 用户 100% 持股公司的公司名 + 已注册商标，品牌**主动公开**（不是泄露）。
- 「企业微信」是腾讯商标，采用「自有品牌 + 描述性用语」的指示性合理使用 → 必须配免责声明。
- 「365」从品牌中移除（未来以自部署白标能力承接，RND-259，与本票无关）。
- 官网：保留公司名 / ICP 备案号 / 邮箱 `hs@crowntime.cn`；**撤掉公司地址和联系电话**。
- `crowntime.cn` 域名引用**不属于本票**（RND-233 处理），看到别动。

## 三、精确落点（已扫描，2026-07-27 基线）

### A 层 —— 本票要改的

1. **`README.md`**
   - L1：`# 365 WeCom Archive` → `# Crowntime WeCom Archive`
   - L1 下方紧跟中文产品名一行：`「康冠时代」企业微信会话存档`
   - L3 简介段：保持技术描述，酌情把产品指称换为新名。
   - L28：`cd wecom-archive-365` → `cd crowntime-wecom-archive`
   - L186：目录树根 `wecom-archive-365/` → `crowntime-wecom-archive/`
   - **新增免责声明**（放简介后、目录前）：
     > 本项目为第三方独立开源项目，与腾讯公司无关联，非腾讯官方产品。"企业微信/WeCom"为腾讯公司商标，本项目名称仅用于描述产品用途。
2. **`static_site/company_homepage/index.html`**
   - ~L245 区块「公司地址」：整个 contact 条目删除（含值）。
   - ~L254 区块「联系电话」：整个 contact 条目删除（含值）。
   - L265 邮箱 `hs@crowntime.cn`：**保留**。
   - 公司名 / ICP / 公安备案号：**保留原样**。
   - 若页面提及产品，统一为「康冠时代」企业微信会话存档。
3. **`docs/*.md` 中的产品名称指称**（非路径！）
   - 先 `git grep -n "365 WeCom Archive\|365企微\|365 会话存档" -- docs/` 确认；产品名指称 → 新名。
   - 中文旧名「365企微会话存档」在 tracked 文件中基线为 0 命中，如 grep 到新增出现再处理。
4. **`pyproject.toml`**：若含 `name`/描述引用旧名 → 同步为 `crowntime-wecom-archive` / 新描述（不加 author email）。

### B 层 —— 明确排除，改了即判失败

以下文件/字符串**一个字符都不许动**（它们是生产基础设施引用，改动会破坏私仓 CI/CD 到 ECS 的部署；由 RND-237 发布导出脚本或后续 ops 迁移另行处理）：

- `/srv/apps/wecom-archive-365` 的所有出现（`scripts/deploy_server.sh` ×12、`docs/DEPLOYMENT.md`、各 runbook、`.env.example`、`backend/scripts/*` ×5、`deploy/systemd/*`）
- systemd 单元名（`wecom-archive-worker.service`、`wecom-archive-media-download.service`、`wecom-backup.service` 等）及其文件内引用
- `.github/workflows/deploy.yml`（×2 处路径）
- `docs/` 中作为**部署路径/命令示例**出现的 `wecom-archive-365`（区别于产品名指称——判断标准：出现在路径、shell 命令、unit 名里的都是 B 层）

### C 层 —— 不在本票

- `ppt-365-intro/`（jsx/PPTX 品牌更新延后至 RND-239 营销站）
- `crowntime.cn` 域名 env 化（RND-233）

## 四、阶段一：基线（RED）

```bash
git grep -c "365 WeCom Archive" | wc -l        # 预期 ≥1（README）
git grep -n "wecom-archive-365" -- README.md    # L28、L186
grep -n "公司地址\|联系电话" static_site/company_homepage/index.html
git grep -c "/srv/apps/wecom-archive-365" | awk -F: '{s+=$2} END {print s}'  # 记录 B 层基线数 N
```

记录各计数，作为 RED 基线。

## 五、阶段二：实现（GREEN，最小变更）

按第三节逐项修改。禁止全局无差别 `sed`——B 层字符串与 A 层同名，必须逐文件、逐处确认后修改。

## 六、阶段三：验证

1. `git grep "365 WeCom Archive"` → 0 命中。
2. `git grep -n "wecom-archive-365" -- README.md` → 0 命中。
3. B 层守恒：`/srv/apps/wecom-archive-365` 总出现次数 == RED 基线 N（一个都没少）。
4. `git diff --stat` 确认改动仅限：README.md、static_site/company_homepage/index.html、docs 内产品名文件、（可选 pyproject.toml）。
5. 官网：无地址/电话，邮箱在，ICP 在。
6. `make verify` 全绿（改动均为文档/静态文件，理论零影响；若测试断言 README 标题等需标注）。

## 七、硬约束（违反即判失败）

- 不改任何 URL / API / 业务行为 / 租户隔离 / i18n。
- B 层文件列表零改动（`git diff` 不得出现 deploy/、scripts/deploy_server.sh、.github/、.env.example、backend/scripts/）。
- 不动 `crowntime.cn` 域名字符串（RND-233 范围）。
- 不执行 git commit / push。

## 八、收尾（交付物）

向用户交付：RED/GREEN grep 计数对比、改动文件清单（`git diff --stat`）、B 层守恒证明、`make verify` 日志、未提交声明。
