# RND-211 QA / 验收 agent 提示词
> 面向独立测试 / QA agent。只读言、不改实现、不 commit / push。
> 验收对象：开发 agent 按 `rnd-211-execution-prompt.md` 产出的改动。

## 一、验收目标
确认"立即同步"按钮可触发真实 Worker、同步状态 API 返回正确状态、前端在同步完成后版本感知地自动刷新（滚动/媒体播放不丢失）、防并发锁生效、零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端 — 数据库 & API
- [ ] B1 `SyncState` 表新增 `status`、`started_at`、`error_message`、`seq_version` 列 —— 证据：`\d sync_states` 或 Alembic migration 源码中列存在
- [ ] B2 `GET /api/admin/sync-status` 返回 `{status, lastSeq, startTime, errorMessage, seqVersion}` —— 证据：`curl -s` 返回 JSON 含以上字段
- [ ] B3 创建新 tenant 后无同步记录时，`GET /api/admin/sync-status` 返回 `status: "idle"`、`lastSeq: 0`、`seqVersion: 0` —— 证据：curl 输出
- [ ] B4 `POST /api/admin/sync-now` 返回 HTTP 202 并触发归档 Worker —— 证据：curl 返回 202；稍后 `sync-status` 先变为 syncing 再变回 idle；数据库中 `lastSeq` 推进
- [ ] B5 同步完成后（含零消息）`seq_version` 递增 1 —— 证据：同步前后 seqVersion 差值
- [ ] B6 重复调用 `POST /api/admin/sync-now`（30s 内第二次）返回 429 或 409 —— 证据：curl 返回频率限制状态码
- [ ] B7 未认证请求被拒绝 —— 证据：无 Cookie 的 curl 返回 401/403
- [ ] B8 多租户隔离：Tenant A 调用不影响 Tenant B 的 sync-status —— 证据：分别用两个 tenant 会话调用

### 后端 — Worker 集成
- [ ] B9 `run_sync_once()` 在同步开始前将 status 设为 syncing、完成（含零消息）设为 idle、异常设 error —— 证据：加临时日志或跨 API 轮询验证状态变化序列
- [ ] B10 `fcntl.flock` 防并发生效：连续两次 `POST /api/admin/sync-now` 不会启动并行 Worker —— 证据：第二次调用返回排队状态，进程列表中只有一个 archive-worker

### 前端 — "立即同步"按钮
- [ ] F1 `review_console.html` 中出现"立即同步"按钮（或等效的 UI 元素） —— 证据：查看页面源码
- [ ] F2 点击"立即同步"后按钮变为禁用态 + 显示"同步中..." —— 证据：浏览器截图观察
- [ ] F3 `syncStatus` 遵循状态机变化：idle → syncing → idle/error —— 证据：浏览器控制台观察 `window.syncStatus`
- [ ] F4 同步完成后显示"上次同步：{时间}" —— 证据：页面可见
- [ ] F5 同步失败时显示友好错误提示 —— 证据：模拟 Worker 异常后验证

### 前端 — 版本感知刷新
- [ ] F6 同步完成且 `seq_version` 变化后，列表/时间线在 ≤10s 内自动刷新 —— 证据：��比同步前后 DOM 数据变化
- [ ] F7 `seq_version` 不变时（如同步无新消息期间，另一个人手动触发同步且成功但无新消息），控制台不额外刷新 —— 证据：观察 30s 周期内无额外接口调用
- [ ] F8 自动刷新后当前聊天的滚动位置不变 —— 证据：滚动到某条消息，同步后 scrollTop 不变
- [ ] F9 自动刷新后媒体播放不中断（如播放中的语音/视频） —— 证据：播放中触发同步，播放不中断

### 前端 — 导航集成
- [ ] F10 "同步与任务"（`nav.syncTasks`）导航项已启用（非 disabled）—— 证据：页面可见并可点击
- [ ] F11 点击"同步与任务"可跳转到/__待定__（如直接定位到同步状态区域或暂无独立页） —— 证据：实际跳转行为

### i18n
- [ ] I1 zh-CN、zh-TW、en 三种语言下"立即同步"等对应 key 均正确显示 —— 证据：切换 locale 后页面文案变更

### 全局契约
- [ ] C1 不改既有 API 的 URL/status/body/OpenAPI —— 证据：`git diff` 不含既有 route 的改动
- [ ] C2 不改 `deploy/systemd/wecom-archive-worker.timer` —— 证据：该文件未被改动
- [ ] C3 不改 i18n 已有 key 名称 —— 证据：`git diff` 无既有 key 名修改
- [ ] C4 不改 `get_current_user` 认证逻辑与租户隔离 —— 证据：源码检查
- [ ] C5 未执行 commit/push —— 证据：`git status` 显示待提交

## 三、回归套件（必须全绿）
- `make verify` 全部通过
- 既有 `test_sync_worker.py` 或相关 sync 测试零回归
- 定时同步（systemd timer）行为不变 —— 证据：timer 单元文件未被改动
- 现有前端刷新按钮行为不改（`refreshNow('manual')` 仍工作）—— 证据：手动点击刷新按钮仍正常

## 四、智能路由判定（每轮必给）
- RND-211 涉及前后端联调，Bug 优先反馈开发 agent 修复，附错误失败测试+期望；不自行改实现
- 测试代码 Bug（如断言逻辑写错）→ 可自行修正测试（须标注）
- 全部通过 → 报告 SUCCESS，附各检查点 PASS 数 / FAIL 数

最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-211 验收结论：PASS / FAIL
后端 B1-B10: [PASS/FAIL counts]
前端 F1-F11: [PASS/FAIL counts]
i18n I1: [PASS/FAIL]
全局 C1-C5: [PASS/FAIL]
回归 make verify: [绿/红]
遗留：___
```
