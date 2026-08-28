# 企业微信第三方企业授权运维说明

本流程与现有自建应用登录完全隔离。只有以下配置均已就绪，才可访问
`/api/auth/wecom/third-party/install` 开始安装：

- `WECOM_THIRD_PARTY_SUITE_ID`
- `WECOM_THIRD_PARTY_SUITE_SECRET`
- `WECOM_THIRD_PARTY_CALLBACK_URL`
- `WECOM_THIRD_PARTY_INSTRUCTION_TOKEN`
- `WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY`
- `WECOM_THIRD_PARTY_CORP_ID`
- `FIELD_ENCRYPTION_KEY`

`WECOM_THIRD_PARTY_CALLBACK_URL` 必须精确指向
`/api/auth/wecom/third-party/callback`；它是**用户授权码回调**，反向代理必须对该路径的
query string 做脱敏。服务只接受管理员授权模式，并再次确认安装主体在应用管理员列表中
拥有管理权限；成员授权会失败关闭，不能成为首位 Owner。

## suite_ticket 生命周期

企业微信会每 10 分钟向服务商应用的**指令回调 URL**推送新的 `suite_ticket`；每个 ticket
实际有效期为 30 分钟，且应始终使用最近收到的值。该 URL 与上述用户授权码回调 URL
是两个不同的地址，绝不能将 `/api/auth/wecom/third-party/callback` 配置为指令回调。
官方说明：[推送 suite_ticket](https://developer.work.weixin.qq.com/document/path/90628)。

RND-350 提供独立 HTTPS 指令回调：

`/api/wecom/third-party/instructions`

该路径同时支持企业微信的 GET URL 验证和 POST 加密指令。它使用独立的
`WECOM_THIRD_PARTY_INSTRUCTION_TOKEN` 与
`WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY`，不得复用 OAuth 回调或
`/api/wecom/archive/events` 的 Token/AESKey。反向代理访问日志必须对完整 query string
脱敏。

**信封 receiver 与明文 SuiteId 的校验边界**：GET URL 验证和 POST 指令
使用不同的已验证 receiver 语义；两者不可混用：

- **GET URL 验证**：AES 信封 receiver id ⟷ `WECOM_THIRD_PARTY_CORP_ID`（服务商
  CorpID，ww 开头 18 位，见企业微信服务商后台“服务商信息”）。不匹配拒绝
  403 `Corp ID mismatch`。
- **POST `suite_ticket` 指令**：AES 信封 receiver id **和**明文 `<SuiteId>` 都必须
  ⟷ `WECOM_THIRD_PARTY_SUITE_ID`。任一不匹配拒绝 403 `Suite ID mismatch`。
- `WECOM_THIRD_PARTY_CORP_ID`、SuiteID、Token 和 EncodingAESKey 都是回调配置的
  必需部分；缺失或无效时 GET/POST 均 fail-closed 返回 503 `configuration_error`，
  不做解密或落库。

服务只接受配置 `suite_id` 的 `suite_ticket` 指令，校验签名、请求时间、相应 HTTP
方法的 receiver 语义、明文 `SuiteId`（POST）和事件时间。ticket 按 `suite_id` 在
`wecom_suite_ticket_states` 中以 Fernet 密文原子保存；相同或更旧事件不会覆盖较新的
权威记录。`WECOM_THIRD_PARTY_SUITE_TICKET` 已废止，禁止把 ticket 放入环境、日志、
GitHub Issue、聊天或截图。

授权服务只读取数据库中的最新 ticket：

- 小于 20 分钟：`fresh`；
- 20 分钟起：`warning`，运维应告警；
- 30 分钟起：`expired`，新的 install/OAuth 流程失败关闭；
- `suite_access_token` 按 `suite_id` 单飞刷新并最多缓存 2 小时，提前 5 分钟刷新；刷新失败
  只能临时使用尚未过期的缓存，绝不使用已过期 token。

平台管理员可只读检查
`GET /api/platform/wecom/third-party/suite-ticket-status`。响应仅包含是否接收、
`fresh|warning|expired|missing|unconfigured`、最后接收时间、年龄和告警布尔值，并带
`Cache-Control: no-store`；不包含 suite id、ticket、token、secret、hash 或前后缀。

### 部署与回滚

1. 发布前按 `docs/DEPLOYMENT.md` 运行当前迁移/修订验证流程：`alembic upgrade head`、
   `scripts/verify_alembic_head.py`，以及适用的 CI schema-drift gate。不要把历史 revision
   号当作当前 head。
2. 保持第三方自助入口关闭，先配置非生产独立指令回调；确认 GET 验证通过并收到至少一次
   `fresh` ticket 后，再执行 GitHub #78 / #79 的非生产授权验收。
3. 回滚应用前先关闭 install 入口。不要把自动 `alembic downgrade` 当作应用回滚的一部分；
   任何 destructive schema action 都需要独立、经批准的运行手册。恢复后必须等待企业微信
   推送新 ticket，不能从日志或环境恢复旧值。
4. 任一异常只记录固定结果类型与时间。禁止记录请求体、解密 XML、ticket、suite token、
   suite secret、CorpID、UserID 或完整回调 URL。

## FIELD_ENCRYPTION_KEY 配置前只读核查

`FIELD_ENCRYPTION_KEY` 不是可随意替换的开关。若已有使用它加密的数据，设置新的未知 key
会使相应值无法解密；`SETTINGS_ENCRYPTION_KEY` 是另一套配置中心密钥，不在本核查范围内。
在生成或写入该 key 前，运维只能运行不返回任何凭证内容的计数查询：

```sql
SELECT 'tenant_wecom_configs' AS store, count(*) AS encrypted_rows
  FROM tenant_wecom_configs
 WHERE app_secret LIKE 'gAAAAA%'
    OR COALESCE(private_key_encrypted, '') LIKE 'gAAAAA%'
UNION ALL
SELECT 'wecom_authorization_proofs', count(*)
  FROM wecom_authorization_proofs
 WHERE permanent_code_encrypted LIKE 'gAAAAA%'
UNION ALL
SELECT 'wecom_organization_claims', count(*)
  FROM wecom_organization_claims
 WHERE permanent_code_encrypted LIKE 'gAAAAA%'
UNION ALL
SELECT 'third_party_organization_bindings', count(*)
  FROM third_party_organization_bindings
 WHERE permanent_code_encrypted LIKE 'gAAAAA%';
```

若 `KEY_PROVIDER=kms_envelope`，还须只读统计 `key_versions.private_key_path LIKE 'gAAAAA%'`。
任一计数非零时，停止配置并从受控密钥系统确认现有 key；禁止通过查询、日志或导出取得明文。
所有计数均为零后，才可为新功能生成并安全注入新的 Fernet key。

## 非生产端到端验收

仅在 ticket 已在受控非生产环境中更新、且不使用康冠生产 CorpID 时执行：

1. 请求 install URL，确认得到跳转企业微信的 302，而非 500；配置故障应跳转
   `login?error=config_error`。
2. 用测试企业的应用管理管理员完成授权，确认授权码回调只产生短时 proof/claim，不暴露
   CorpID、用户标识或授权凭据。
3. 确认官方企业名称并创建组织，确认浏览器只进入 `/admin/provisioning`。
4. 在激活前验证 dashboard、归档、同步、导出和邀请均失败关闭，只允许等待页、配置准备页和
   状态接口；平台激活后才允许进入正常控制台。
5. 全程检查应用和反向代理日志，确认不包含 code、state、ticket、token、permanent code、
   CorpID 或 UserID。

当前开发者登记应用实测前的上线门禁：使用非生产测试企业完成一次安装、确认授权模式为
管理员授权、确认两个回调的地址与权限，再允许开启入口。现有康冠生产企业无需作为
“第二家企业”重复创建；如仅验证同一 CorpID，系统应走安全冲突分支而不是新建租户。

排障时只能记录粗粒度错误类型。禁止记录授权 code、state、suite ticket、suite token、
permanent code、CorpID、UserID 或完整回调 URL。

配置不完整或字段加密密钥缺失/无效时，install 与授权码 callback 均安全跳转至
`/admin/login?error=config_error`，且不创建或消费授权状态、proof、组织或用户数据。

自助创建完成后租户保持 `provisioning` 且 `is_active=false`。其会话只能访问
`/admin/provisioning`、`/admin/provisioning/settings` 与 `/api/provisioning/status`；归档、
同步、导出、邀请和普通后台均失败关闭。运维完成会话存档凭证、回调与连通性验证后，
再由平台启用租户；激活后的 archive dispatch 使用该租户的显式 tenant-scoped
worker path。现有单 `WECOM_CORP_ID` worker 属于 transitional runtime，且不由本
runbook 改动。
