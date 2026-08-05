# RND-348 QA Summary

- 单事务创建 `provisioning` Tenant、独立第三方授权绑定、唯一 Owner/身份和最小会话；不创建 `TenantWecomConfig`。
- 企业官方名称直接使用，内部 slug 由 CorpID 的单向摘要生成；租户永不自动激活。
- claim 消费结果可幂等读取，数据库唯一约束兜住同 CorpID 并发；已有组织安全冲突。
- provisioning session 在通用认证依赖处被拒绝，只允许等待页、配置准备页和安全状态接口。
- provisioning session ID 使用 36 字符 UUID，匹配生产 `admin_sessions.id VARCHAR(36)`；新增真实 PostgreSQL 插入回归测试（无 `DATABASE_URL` 时按仓库约定跳过）。
- 平台租户列表使用外连接，尚未创建 `TenantWecomConfig` 的 provisioning 租户也可被发现；缺失配置以 `corp_id=null`、`agent_id=null`、`config_is_active=false` 安全表达。
- 平台激活 provisioning 租户时，同一事务将其未撤销的受限会话升级为 admin 会话；登录页按会话 scope 分流，激活前返回等待页、激活后进入 dashboard，不形成登录跳转环。
- 注入事务失败后 Tenant、binding、Owner、Identity、Session、Audit 全部为零，claim 仍可重试。
- 既有租户按 `is_active` 回填 `active`/`suspended`；平台创建和启停路径同步 lifecycle。
- 归档 worker 保持单一 `WECOM_CORP_ID` 模式，没有读取第三方绑定或多租户调度。
