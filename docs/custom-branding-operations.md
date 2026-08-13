# 云托管付费白标运维说明（RND-259）

> 这是 **Crowntime 云托管付费套餐/附加项** 能力，不属于开源自部署版本。
> 开源使用者仍可在遵守许可证的前提下自行修改源码、静态资源和反向代理域名；公开版本不提供租户级图形化白标、域名验证或平台代管证书服务。

## 商业与安全边界

- `custom_branding` 控制企业 Logo 和可选 Favicon；`custom_domain` 控制一个主访问域名。两项均由现有 `plan_entitlements` + 当前有效订阅在后端判定。
- 停止权益、订阅到期、取消或降级时，上传数据和域名记录不会被删除；自定义 Logo/Favicon 和自有域名立即不再生效，平台品牌与 `ADMIN_DOMAIN` 是恢复入口。
- 系统不会移除法律主体、隐私政策、服务条款、商标说明或其他必要的平台归属信息。
- 首期仅支持一个 Logo、一个可选 Favicon 和一个主机名。不能上传 HTML、CSS、JavaScript、SVG 或 TLS 私钥。

## 租户管理员流程

1. 在 **设置 / 品牌** 确认套餐权益；无权益时页面展示升级入口，所有写 API 仍会返回明确的 `*_upgrade_required` 拒绝。
2. 上传 Logo（PNG/JPEG/WebP，最多 2 MB）和可选 Favicon（PNG/JPEG/WebP/ICO，最多 1 MB）。服务端检查实际解码格式、声明 MIME、尺寸和像素数，并重新编码；它不相信文件名或扩展名。
3. 输入完整主机名，例如 `archive.example.com`。IP、通配符、`localhost`、内部/文档保留后缀和平台域名会被拒绝。
4. 页面仅在保存时显示一次 TXT 值。添加：

   ```text
   _crowntime-verify.archive.example.com  TXT  <页面显示的值>
   ```

   原始 TXT 值不写入数据库、审计记录或日志；数据库仅保留 SHA-256 摘要。
5. 点击 DNS 验证。验证成功只会把域名推进到“已验证 / TLS 处理中”；在证书已签发且平台确认部署前，域名绝不会被路由。
6. TLS 显示“已签发”后，管理员可点击“启用已签发域名”。若需要回退，点击“停用”或“解绑”；平台默认域名始终可用。

## 平台 TLS 控制器（生产启用门禁）

应用层不签发、上传或接收私钥。生产环境必须经过正式变更审批，使用受批准的受管边缘/ACME 控制器完成：DNS/路由预配、签发、部署、续期和实际 HTTPS 探测。控制器只能在完成实际操作后，以独立的 PlatformAdmin 凭证调用：

```text
POST /api/platform/tenants/{tenant_id}/branding/certificate-status
{
  "status": "issued",
  "expires_at": "2027-01-01T00:00:00Z"
}
```

可报告的安全状态是 `pending`、`issued`、`failed`、`expired`。`failed`/`expired` 会立即停用域名路由；控制器只能提供固定的小写失败码，不能提交证书、私钥、ACME 响应、验证值或任意错误文本。租户会话不能调用该接口。

**当前仓库没有一个可获批准的多租户边缘/ACME 提供商配置，也不会以手工 Nginx 配置冒充自动化。** 因此，在已接入并演练上述控制器、DNS 验证、自动续期和证书失败回退以前，生产启用必须保持关闭；应用层会安全地停留在 `pending`，不会错误路由。该外部控制器接入及生产变更审批是 RND-259 上线的显式依赖，而不是可绕过的待办。

## Host、回调和 URL 规则

- `BrandingHostMiddleware` 只接受 `ADMIN_DOMAIN`（以及开发本机）或数据库中**精确匹配**、已验证、已签发、已启用且仍有 `custom_domain` 权益的域名。未知、多 Host、未验证、已解绑、证书失败或权益失效请求以 `421 Misdirected Request` 失败关闭。
- 禁止以 `Host`、`X-Forwarded-Host` 或浏览器输入构造绝对 URL。`tenant_public_base_url()` 仅在所有自定义域名门禁满足时返回已批准的 `https://<custom-domain>`，否则只返回配置的 `ADMIN_DOMAIN`。
- 企业微信 OAuth、会话存档回调和第三方应用指令回调继续使用各自注册的固定平台域名和原有配置；白标域名不得替换这些安全回调。
- Logo/Favicon 响应使用 `private, no-store`、`nosniff` 与 host/session 解析，避免 CDN 或多标签缓存把一个租户的品牌展示给另一个租户。

## 观测、审计与故障演练

审计记录包含上传/恢复、域名配置、验证、启用、停用、解绑和平台证书状态变更；域名在审计中只保留短摘要，TXT 值、私钥和原始控制器报错均不记录。PlatformAdmin 专用的 `GET /api/platform/branding/domain-metrics` 只返回待验证、待签发、30 天内到期、失败和无效绑定的聚合计数；`GET /api/platform/branding/domains` 只提供 TLS 控制器所需的非敏感工作列表。

上线前至少演练：两个租户两个域名的正反隔离、重复域名拒绝、Host 篡改、订阅降级恢复、DNS 验证失败、证书失败/到期、默认域名恢复、OAuth 跳转和媒体访问。平台应对 `pending` 数、临近到期、`failed`/`expired` 及无效绑定建立不含敏感值的运营指标和告警。
