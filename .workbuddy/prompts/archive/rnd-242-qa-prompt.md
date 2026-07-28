# RND-242 QA / 验收 agent 提示词 —— 产品更名验收

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-242-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认品牌更名为「康冠时代」企业微信会话存档 / Crowntime WeCom Archive 已在 A 层（展示/品牌）完整落地，**B 层（生产基础设施路径）零改动**，官网撤地址电话留邮箱，免责声明就位，零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### A 层落地
- [ ] A1 `README.md` L1 标题 = `# Crowntime WeCom Archive`，含中文名「康冠时代」企业微信会话存档 —— 证据：读文件头
- [ ] A2 README 含免责声明（第三方独立项目 / 非腾讯官方 / 商标说明）—— 证据：grep "非腾讯官方\|腾讯公司商标"
- [ ] A3 `git grep "365 WeCom Archive"` = 0 命中
- [ ] A4 `git grep -n "wecom-archive-365" -- README.md` = 0 命中（L28 cd 命令、L186 目录树已换 `crowntime-wecom-archive`）
- [ ] A5 官网 `static_site/company_homepage/index.html`：无「公司地址」「联系电话」区块；`hs@crowntime.cn` 邮箱保留；公司名 + ICP 备案号保留 —— 证据：grep
- [ ] A6 docs 中产品名指称已统一为新名（路径类引用除外）—— 证据：`git grep "365企微\|365 WeCom" -- docs/` = 0

### B 层守恒（最关键 — 改了会打断生产部署）
- [ ] B1 `git diff --name-only` 不含：`deploy/`、`scripts/deploy_server.sh`、`.github/`、`.env.example`、`backend/scripts/`
- [ ] B2 `/srv/apps/wecom-archive-365` 全仓出现次数 == 开发 agent 报告的 RED 基线 N（守恒）
- [ ] B3 systemd 单元名（wecom-archive-worker 等）未改 —— 证据：`git diff -- deploy/` 为空
- [ ] B4 `crowntime.cn` 域名字符串未动（RND-233 范围外溢检查）—— 证据：`git diff | grep crowntime.cn` 仅出现于官网撤字段的上下文或为空

### 全局契约
- [ ] C1 `git diff` 无任何 `.py` 业务逻辑改动（backend/app 零 diff）
- [ ] C2 `make verify` 全绿
- [ ] C3 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）

## 三、回归套件

`make verify`（全量）。改动理论上仅文档/静态文件；若任何测试失败，先判断是否测试断言了旧名/旧标题。

## 四、智能路由判定（每轮必给）

- 源码/文档改动有错（漏改、误改 B 层）→ 反馈开发 agent 修复，附具体文件:行号 + 期望；不自行改实现。
- 测试断言旧产品名 → 可自行修正测试并标注（属「测试迁就旧路径」例外）。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-242 验收结论：PASS / FAIL
A 层：A1–A6 各项 PASS/FAIL + 证据
B 层守恒：/srv 路径计数 RED=__ GREEN=__（须相等）；diff 文件清单
回归：make verify __（绿/红）
契约：backend/app 零 diff __；未 commit __
遗留：__
```
