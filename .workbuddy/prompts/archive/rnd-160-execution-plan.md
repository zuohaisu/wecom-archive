# RND-160 执行计划（服务器资源优化 2核2GB — 父任务编排）

> 顶层计划，串联 4 个子任务。本文件**不直接改代码/生产配置**——RND-160 是父任务（统筹），所有实现在子任务 190–193 中完成。
> 配套文档：`rnd-160-execution-prompt.md`（程序执行 brief）、`rnd-160-qa-prompt.md`（父任务验收）。
> 各子任务的详细开发/验收提示词（`rnd-19x-execution-prompt.md` / `rnd-19x-qa-prompt.md`）按本计划 §4 的执行顺序逐一生成。

## 0. 任务与来源
- Linear：**RND-160「服务器资源优化（2核2GB）」**（父任务 / P3 / In Progress），assignee Haisu。
- 目标：在不优先升级服务器的前提下，建立资源基线、定位瓶颈、分阶段优化 API/SQL、Worker/媒体下载、日志/磁盘/备份增长。
- 执行原则（来自 issue）：先测量再优化；每个子任务独立验收；父任务不直接做大范围代码/生产配置修改；所有优化保留回滚方案，且验证不影响 归档 / 解密 / 媒体下载 / 后台查阅 主流程。
- 父任务验收标准：四个子任务均完成或有明确延期结论；形成优化前后资源对比；2核2GB 运行建议/阈值/运维手册已更新。

## 1. 决策记录（规划时确认）
- **RND-160 是编排/父任务，本身不落地代码或生产配置。** 一切实现在 190/191/192/193。因此本计划 + 执行 brief 只定义「方法、顺序、测量框架、回滚、运维手册交付」，不重复子任务的实现细节。
- **测量先行。** 任何优化项必须可追溯到 RND-190 的基线数字（前后对比）。无基线证据不优化。
- **回滚强制。** 代码改动走既有 RND-227 自动回滚（`scripts/deploy_server.sh`，健康门 `127.0.0.1:8035/health/ready`，回滚到 `last_known_good_sha`）；systemd 单元 / postgres 配置 / logrotate / journald 等生产配置改动，必须保存改动前副本并写明回滚步骤。
- **每子任务独立 QA。** 开发 agent 实现后先交独立 QA agent 验收；父任务验收 = 汇总四子任务结论 + 前后对比 + 运维手册。
- **Agent 绝不 commit/push**（项目硬规则），提交由 Haisu 操作。

## 2. 子任务现状盘点（来自 Linear，2026-07-26/27）
| 子任务 | 标题 | 状态 | 依赖 | 范畴要点 |
|---|---|---|---|---|
| RND-190 | 服务器资源基线与瓶颈报告 | **Done** | —（父） | CPU/内存/swap/磁盘/inode/网络 + FastAPI/Uvicorn/PostgreSQL/Nginx/systemd 占用 + 关键 API 测量 + 慢查询/行数/响应体 + 归档/解密/媒体峰值 + 日志/DB/媒体增速；输出瓶颈 Top5 + 前后优化排序 |
| RND-191 | API / SQL 性能优化 | In Progress | 190 | 员工/联系人/会话列表、消息时间线、搜索查询；索引；消除 N+1/无效 join/重复聚合/过大结果集；分页/轻字段/渐进加载；响应体控制；性能回归测试 |
| RND-192 | Worker 和媒体下载资源优化 | In Progress | 190 | 归档同步/解密/图片下载/Qiniu 迁移的批量/并发/调度/重试；避免高成本任务重叠；临时文件/内存缓冲/网络读取；幂等/可重试/可观测 |
| RND-193 | 日志、磁盘与备份增长治理 | In Progress | 190 | 日志/journal/PG/媒体/备份占用与增速；logrotate/保留；临时文件/失败残留清理；备份范围/频率/保留/恢复演练；磁盘/inode 阈值与告警 |

## 3. 依赖图
```
RND-190 (基线, Done)
   ├──► RND-191 (API/SQL)       [In Progress]
   ├──► RND-192 (Worker/媒体)   [In Progress]
   └──► RND-193 (日志/磁盘/备份) [In Progress]
```
- 三者均 blocked-by 190；190 已完成 → 三者均已解锁。
- 三者之间无 Linear 硬依赖，但存在**文件面重叠**（见 §4），故建议顺序执行而非并行。

## 4. 建议执行顺序（含并行安全性分析）
**RND-190（Done）→ RND-191 → RND-192 → RND-193**

理由：
1. **RND-191 先**：用户感知最强的首屏延迟（员工/会话列表等待）在此；且 DB 查询减负是 2GB 内存的最大单一杠杆（降 DB 内存 + 降 CPU）。
2. **RND-192 次**：后台任务成本。Worker/媒体脚本（`scripts/run_archive_worker_once.py`、`scripts/download_wecom_media_once.py`）与 191 共享后端 ORM/连接模式，等 191 的 DB 调优结论更稳；同时 192 改 `deploy/systemd/*` 的 ExecStart 参数与 timer 频率，会与 193 的生产配置面相邻，先做完 192 再让 193 收口配置，避免同一单元文件并发改动冲突。
3. **RND-193 后**：最偏运维配置（logrotate / journald / postgres conf / 备份脚本），与代码改动面最小；放在最后能覆盖前序代码/调度变更带来的新磁盘增长行为，且不与 191/192 抢工作树。

并行安全性：
- **191 vs 192**：文件重叠低（191=`backend/app/routers/*`、`conversation_membership.py` 等；192=`scripts/*_once.py` + `deploy/systemd/*.{service,timer}`）。可并行但**不推荐**——同一 `main` 分支 + 项目「agent 不 commit、每任务独立 QA」规则下，顺序合并更稳。
- **192 vs 193**：均可能改 `deploy/systemd/*`（192 改 ExecStart 参数/timer；193 改 journald/logrotate/系统配置），**有重叠风险**，必须顺序（192 先于 193）或同一引擎同一分支完成。
- **结论**：RND-160 内部按 191→192→193 顺序、各子任务独立 dev+QA 提示词、合入 `main` 后再开下一个。RND-193 若想提前拿「快速胜利」（纯运维配置、风险低），可在 191 之后插入，但仍须先合 191 再动 193，避免与 192 的 systemd 改动冲突。

## 5. 各阶段范畴 / 关键落点 / 关键指标（给子任务提示词作输入）
- **RND-191（API/SQL）**
  - 关键文件：`backend/app/routers/conversations.py`、`backend/app/conversation_membership.py`、`backend/app/routers/` 下员工/联系人/搜索路由；`alembic/` 索引迁移；`backend/tests/` 性能/回归。
  - 关键指标：员工列表、会话列表、消息时间线、搜索 的 P50/P95 响应时长、响应体大小、DB 查询行数/慢查询、内存/CPU 峰值（对比 RND-190 基线）。
  - 约束：不改同步/解密/媒体语义；不靠放宽租户过滤换性能；无基线证据不引入缓存。
- **RND-192（Worker/媒体）**
  - 关键文件：`scripts/run_archive_worker_once.py`、`scripts/download_wecom_media_once.py`、`scripts/backfill_thumbnails_once.py`（手动）、`deploy/systemd/wecom-archive-worker.{service,timer}`、`deploy/systemd/wecom-archive-media-download.{service,timer}`。
  - 关键指标：单次任务 CPU/内存峰值、运行时长、吞吐（条/分钟）；前台 API 在后台任务期间可用性。
  - 约束：不引入重型队列；不改同步/解密/媒体语义；不以跳过失败/降数据完整性换资源；锁/幂等/重试回归通过。
- **RND-193（日志/磁盘/备份）**
  - 关键文件（多为**服务器侧、不在仓库**，需定位/补建）：应用日志（uvicorn/FastAPI，见 `backend/app/main.py` 日志配置）、Nginx 日志、`systemd journald` 配置、PostgreSQL 数据目录、媒体目录、备份目录；备份/恢复脚本（仓库内暂无，需 RND-193 补建或落 `scripts/`）。
  - 关键指标：各目录占用与日增速；logrotate/保留策略；备份 RPO/RTO 与至少一次受控恢复演练；磁盘/inode 阈值与告警路径。
  - 约束：不定义客户数据删除/合规留存政策；不删生产归档消息/媒体（除非明确批准+可验证备份）；不把单机本地备份当完整灾备。

## 6. 与现有部署/交付的衔接
- 代码部署：`scripts/deploy_server.sh`（RND-227）已具备「迁移校验 + 健康门 + 自动回滚到 last_known_good_sha」；任何后端代码优化经此安全上线。
- Web 应用单元：`wecom-archive-365.service`（uvicorn `app.main:app`）由 systemd 托管，**该 unit 不在仓库内**（`deploy_server.sh` 注释 line 47 明确「not included in this repo — out of scope」），生产配置改动在服务器侧完成并单独回滚。
- 健康门：`127.0.0.1:8035/health/ready`（DB + schema 版本），优化后须保持该门通过。

## 7. 通用硬性约束（违反即判失败）
- ❌ Agent 不 commit / push（Haisu 操作）。
- ❌ RND-160 父任务不直接做大范围代码/生产配置修改（实现在子任务）。
- ❌ 无 RND-190 基线证据不优化、不引入缓存/复杂基础设施。
- ❌ 改变 归档 / 解密 / 媒体下载 / 后台查阅 任一主流程语义。
- ❌ 诊断/备份泄露密钥、聊天内容或完整签名 URL（RND-190/RND-193 口径）。
- ✅ 每项优化有前后对比数字；每项生产配置改动有保存前副本 + 回滚步骤；每子任务 `make verify` 绿。
