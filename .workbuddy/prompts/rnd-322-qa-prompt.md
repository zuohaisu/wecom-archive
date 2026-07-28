# RND-322 QA / 验收 agent 提示词 —— 语言下拉可见性修复验收

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-322-execution-prompt.md` 产出的工作树改动。
> 关联 Linear issue：**RND-322**。

## 一、验收目标

确认登录后左下角「语言」按钮下拉在**任意视口高度下都完整可见、可点击、即时切换全站文案并持久化**，且零回归、零越界改动（未触碰后端/i18n 语义）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 下拉可见性（核心）
- [ ] V1 登录进入审阅控制台，点击 `#btn-lang-toggle`，`#lang-menu` 出现且**完整可见**（不被裁剪）。
  - 证据：Playwright `is_visible()`=true；`boundingBox()` 底部 ≤ 视口高度（`page.viewport_size`）；DevTools 确认未落在 `.side-nav` 裁剪区外。
- [ ] V2 在**矮视口**（如 768px 高）下重复 V1，仍完整可见（防止「高屏没事矮屏裁剪」复发）。
  - 证据：设 viewport height=700~800 重跑。

### 切换与持久化行为
- [ ] B1 下拉列出 zh-CN / zh-TW / en 三选项，均可点击。
- [ ] B2 选择非当前语言后：`<html lang>` 改变；已知 `data-i18n` 文案（如侧栏标题、按钮文案）即时切换。
- [ ] B3 选择后**动态内容**也切换（搜索框 placeholder、scope 标签、详情面板等，对照 `console-entry.js:31-90` `applyLocale` 覆盖项）。
- [ ] B4 刷新页面后 locale 从 `localStorage`（key `wecom_admin_locale`）恢复，界面仍为所选语言。
- [ ] B5 点击下拉外部区域，`#lang-menu` 收起（外部点击关闭逻辑未被破坏）。

### 契约 / 范围
- [ ] C1 `git diff --name-only` 不含业务改动：无 `backend/app/*.py` 逻辑变更（i18n_assets.py 等不应变）。
- [ ] C2 `backend/app/assets/i18n.js` 的 `LocaleRegistry` / `STORAGE_KEY` / `setLocale` 语义**未改**（仅前端可见性修复）。
- [ ] C3 改动主要限于 `review_console.html`（CSS + 必要的 data-testid）+ 新增/修改测试文件；无新依赖、无布局大重构。
- [ ] C4 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）。

### 回归
- [ ] R1 新增/加固的 Playwright E2E 测试存在且通过（覆盖 V1/V2/B1–B5 路径）。
- [ ] R2 `make verify` 全绿（含既有 console 测试与新测试）。

## 三、回归套件

- `make verify`（全量，含新 E2E）。
- 如本地无法跑 Playwright（缺浏览器/秘钥），至少：手测 V1/B1–B5 + 静态核查 C1–C4 + 说明 E2E 未能本机执行的原因。

## 四、智能路由判定（每轮必给）

- 前端可见性/行为不符（V1/V2/B1–B5 任一 FAIL）→ 反馈开发 agent 修复，附具体文件:行号 + 期望 + 实测值；不自行改实现。
- 测试本身脆弱/误断言 → 可自行修正测试并标注。
- 越界改动（C1–C3 任一 FAIL）→ 明确判 FAIL，要求开发 agent 收敛范围。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-322 验收结论：PASS / FAIL
可见性：V1 __ V2(矮屏) __
行为：B1 __ B2 __ B3 __ B4 __ B5 __
契约：C1 __ C2 __ C3 __ C4 __
回归：R1(新E2E) __ R2(make verify) __
遗留：__
```
