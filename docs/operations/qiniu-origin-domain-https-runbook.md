# Qiniu Origin (Source) Domain HTTPS Certificate Runbook — GH-232

**Scope:** `media-origin.crowntime.cn` — the custom **source domain** （源站域名）
bound to object-storage bucket `365-wecom-media`, CNAME → `iovip-z2.qiniuio.com`.
Backend media signing (`backend/.env` → `QINIU_DOMAIN=https://media-origin.crowntime.cn`)
serves user-facing media over this domain, so an expired certificate here breaks
HTTPS media access for the 365 frontend.

## Why this runbook exists (the API limitation)

Qiniu's domain HTTPS API — `PUT/GET api.qiniu.com/domain/{domain}/httpsconf`,
exposed in the official SDK as `DomainManager.put_httpsconf` and used by
`ssl-renew/lib/qiniu.sh` — manages **CDN (fusion) domains only**. For an
object-storage source domain it returns:

```
HTTP 612: {"code":612,"error":"no such domain"}
```

This is confirmed by:

- Production logs (2026-09-19): CDN bind to `media.crowntime.cn` succeeded;
  origin bind to `media-origin.crowntime.cn` returned HTTP 612.
- Qiniu Kodo documentation: source-domain HTTPS is configured in the console
  (空间 → 域名管理 → 配置 HTTPS), no public API is documented
  (<https://developer.qiniu.com/kodo/8556/set-the-custom-source-domain-name>).
- acme.sh upstream issue <https://github.com/acmesh-official/acme.sh/issues/5086>:
  same 612 against source domains; maintainer closed it as "the qiniu deploy
  hook uses the CDN httpsconf API; object-storage source-station domains are
  not covered by that API."

Because binding is manual, `ssl-renew/renew-wildcard.sh` **fails closed** when
the origin bind fails (it `die`s, so `OnFailure=wecom-job-failure-alert@%n.service`
pages), records the uploaded `certID` in the error message and in
`~/.acme.sh/crowntime.cn/.deployed_certid`, and keeps re-alerting until the
origin actually serves the new certificate — the early-exit path verifies the
**live TLS certificate** before declaring "nothing to do".

## When to run this

- Every time `qiniu-ssl-renew-wildcard.service` alerts with
  `stage=qiniu_bind_origin` (each successful acme.sh renewal produces a new
  cert that must be bound to the origin domain manually), and
- Immediately, for the 2026-10-19 expiry of cert `6a5ef31fcb904e2c369b43fd`
  if the cert currently served is still the old one.

## Procedure (Qiniu console)

1. Get the certID to bind from the alert / journal:

   ```bash
   journalctl -u qiniu-ssl-renew-wildcard.service -n 50 --no-pager
   # look for: uploaded — certID=<id>   and   stage=qiniu_bind_origin ... certID=<id> is NOT bound
   ```

   The latest uploaded certID is also recorded on the host at
   `~/.acme.sh/crowntime.cn/.deployed_certid`.

2. Console path: **对象存储 Kodo → 空间管理 → `365-wecom-media` → 域名管理 →
   自定义源站域名 `media-origin.crowntime.cn` → 配置 HTTPS**.
   - 开启 HTTPS 访问 （访问控制 http/https → 切换/开启）.
   - 在 **更换证书** 列表中选择与上面 certID 对应的证书（证书已由其上传流程托管到
     SSL 证书服务，名称形如 `crowntime.cn-<unix-ts>`）。

3. Verify from any host:

   ```bash
   openssl s_client -servername media-origin.crowntime.cn -connect media-origin.crowntime.cn:443 </dev/null 2>/dev/null \
     | openssl x509 -noout -dates -fingerprint -sha256
   ```

   - `notBefore` must be later than the renewal date, and
   - the SHA256 fingerprint must match the local leaf fingerprint:
     `openssl x509 -in ~/.acme.sh/crowntime.cn/fullchain.cer -outform DER | sha256sum`.

4. Re-run the renewal service — it must now be a verified no-op:

   ```bash
   sudo systemctl start qiniu-ssl-renew-wildcard.service
   journalctl -u qiniu-ssl-renew-wildcard.service -n 20 --no-pager
   # expect: cert already deployed and verified on media.crowntime.cn (certID=...) and media-origin.crowntime.cn (live TLS) — nothing to do
   ```

   If it instead runs a full deployment and dies at `qiniu_bind_origin` again,
   the served certificate still does not match — recheck step 2/3.

## If Qiniu ships a source-domain HTTPS API later

Replace the origin branch of `renew-wildcard.sh`: bind via the new API, keep
the fail-closed `die` on failure, and switch the early-exit origin check from
the live-TLS probe to an API verify. The bats suite
(`ssl-renew/tests/12_renew_wildcard.bats`, GH-232 cases) must be updated in
the same ticket.
