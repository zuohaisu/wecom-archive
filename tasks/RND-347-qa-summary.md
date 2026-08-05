# RND-347 QA Summary

- 未知 CorpID 只回到既有 `/admin/login?error=organization_not_found`，没有新建“组织不存在”落地页。
- 只有持有有效、未过期、服务端可解析 claim Cookie 的浏览器看到“创建组织”动作。
- 确认页只展示企业微信官方企业名称，使用只读 `output`；不输出 CorpID、主体或凭证。
- claim 公共引用为不可猜随机值，服务端只保存摘要；确认、取消和重放均有状态约束。
- 同 CorpID 已存在时安全冲突，不创建 claim；仍不创建 Tenant、用户、身份或 Session。
- 确认页覆盖中文、英文、日文并带语义化标题、label、状态与原生表单操作。
