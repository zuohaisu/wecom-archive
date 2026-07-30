# 密钥托管与租户隔离

WeCom 的每个企业都拥有独立的 RSA 私钥和从 `publickey_ver=1` 开始的版本序列。系统以 `(tenant_id, publickey_ver)` 唯一约束选择密钥，因此一个租户的版本号不会与另一个租户冲突，也不会跨租户取钥。

## 云托管

云托管使用 `KEY_PROVIDER=kms_envelope`。私钥 PEM 在写入 `key_versions` 前由现有的字段加密服务加密；数据库中保存的是 Fernet 信封，不是 PEM。读取时只在工作进程内存中解封并构造 RSA 对象，流程不会把明文写入磁盘、日志或异常消息。字段加密根密钥由部署环境的受控密钥管理机制提供，数据库读取权限本身不足以还原私钥。

只有运行解密任务、且已解析到该租户和密钥版本的服务身份能够请求这份密钥。每次解密批次都会通过 append-only `audit_logs` 记录 `tenant_id`、`publickey_ver` 和操作类型；审计记录不含 PEM、解密内容或会话密钥。

## 自托管

自托管默认 `KEY_PROVIDER=local_file`，保留现有 `WECOM_PRIVATE_KEY_PATH` 行为。客户自行保管 PEM 和主机访问控制；为逐版本管理时，`key_versions.private_key_path` 保存本机 PEM 路径。迁移期间尚未登记 KeyVersion 的单租户安装会安全回退到该环境变量，不改变既有解密路径。

无论托管方式如何，密钥版本必须属于目标租户且处于 active 状态。密钥获取失败只返回通用错误，避免凭据出现在操作日志或终端输出中。
