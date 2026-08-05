# 企业微信第三方企业授权运维说明

本流程与现有自建应用登录完全隔离。配置以下环境变量后，访问
`/api/auth/wecom/third-party/install` 开始安装：

- `WECOM_THIRD_PARTY_SUITE_ID`
- `WECOM_THIRD_PARTY_SUITE_SECRET`
- `WECOM_THIRD_PARTY_SUITE_TICKET`
- `WECOM_THIRD_PARTY_CALLBACK_URL`
- `FIELD_ENCRYPTION_KEY`

回调地址必须精确指向 `/api/auth/wecom/third-party/callback`，反向代理也必须对该路径的
query string 做脱敏。服务只接受管理员授权模式，并再次确认安装主体在应用管理员列表中
拥有管理权限；成员授权会失败关闭，不能成为首位 Owner。

当前开发者登记应用实测前的上线门禁：使用非生产测试企业完成一次安装、确认授权模式为
管理员授权、确认回调与应用管理员列表权限，再允许开启入口。现有康冠生产企业无需作为
“第二家企业”重复创建；如仅验证同一 CorpID，系统应走安全冲突分支而不是新建租户。

排障时只能记录粗粒度错误类型。禁止记录授权 code、state、suite ticket、suite token、
permanent code、CorpID、UserID 或完整回调 URL。
