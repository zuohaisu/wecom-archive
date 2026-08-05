# RND-346 QA Summary

- 第三方企业授权与现有自建应用登录路由、状态和数据表隔离。
- state 服务端存储、短时且一次性；证明仅服务端存储，浏览器 Cookie 为不可猜随机绑定。
- 仅接受管理员授权且授权主体拥有应用管理权限；成员授权失败关闭。
- 管理员校验严格使用第三方应用契约：`POST /service/get_admin_list`、查询参数 `suite_access_token`、JSON 包体 `auth_corpid`/整型 `agentid`；录制式单测断言 method/path/query/body，并禁止回退到企业 `/agent/get_admin_list`。
- 失败不创建 Tenant、TenantWecomConfig、AdminUser、AdminLoginIdentity 或 AdminSession。
- 官方接口封装可通过 FastAPI dependency 替换为 fake，测试不访问企业微信。
- 运维真实企业安装验证按用户授权后置，见 `docs/operations/wecom-third-party-authorization.md`。
