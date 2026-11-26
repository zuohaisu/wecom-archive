# Qiniu Origin (Source) Domain HTTPS Certificate Runbook

**Scope:** the two Kodo object-storage **source domains** (源站域名) that Qiniu's
CDN HTTPS API cannot manage:

| Domain | Bucket | CNAME | Consumed by |
|---|---|---|---|
| `media-origin.crowntime.cn` | `365-wecom-media` | `iovip-z2.qiniuio.com` | `backend/.env` → `QINIU_DOMAIN`; user-facing media for the 365 frontend |
| `pdf-vault-files.crowntime.cn` | `pdf-vault-prod` | `iovip-z0.qiniuio.com` | `/srv/apps/pdf-vault/shared/runtime.env` → `PDFVAULT_QINIU_DOWNLOAD_DOMAIN` |

Both serve user traffic, so an expired certificate here breaks HTTPS media /
PDF downloads. Neither is covered by the CDN `httpsconf` API; both are bound in
the console only.

Behavior contract: [Wildcard SSL Renewal](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal)
(Wiki), GH-232 / GH-239 sections.

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
  (空间 → 域名管理 → 配置 HTTPS); no public API is documented
  (<https://developer.qiniu.com/kodo/8556/set-the-custom-source-domain-name>).
- acme.sh upstream issue <https://github.com/acmesh-official/acme.sh/issues/5086>:
  same 612 against source domains; maintainer closed it as "the qiniu deploy
  hook uses the CDN httpsconf API; object-storage source-station domains are
  not covered by that API."

Because binding is manual, `ssl-renew/renew-wildcard.sh` **fails closed**: it
`die`s (so `OnFailure=wecom-job-failure-alert@%n.service` pages) until the
operator has console-bound the current certificate and recorded the marker
below. That alert is the intended visible failure, not a false alarm.

## What GH-239 changed (read this before following any older copy)

| | Before GH-239 | Now |
|---|---|---|
| Order of operations | upload → bind CDN → origin 612 → die **without recording state** → the identical upload + CDN bind repeated on every later run (one Qiniu "certificate changed" email per day) | the origin marker is checked **before the first Qiniu write**; a missing/stale marker exits at `stage=origin_console_preflight` with **zero** upload/bind |
| Where the certID comes from | read `uploaded — certID=<id>` from the journal (the script had already uploaded) | **nowhere** — the API upload deliberately has not run yet; the operator uploads the certificate in the console first (step 2) |
| nginx | `nginx_deploy` sat after the origin gate, so nginx kept serving the previous certificate until someone copied files by hand | `deploy_cert_to_nginx` runs independently (own leaf comparison) before the Qiniu early-exit — nginx no longer waits on this runbook |

This runbook is now **console-first, script-second**. Do not look for an
uploaded certID in the journal; there is none.

## When to run this

- When `qiniu-ssl-renew-wildcard.service` fails with
  `stage=origin_console_preflight` — once per acme.sh renewal, because every new
  certificate invalidates the fingerprint-bound marker.
- For `pdf-vault-files.crowntime.cn`: there is no timer and no marker. Check it
  during any certificate audit, and rebind whenever its live `notAfter` is
  within ~30 days (see the dedicated section below).

## Procedure (media-origin.crowntime.cn)

### 1. Read the current fingerprint from the alert

```bash
journalctl -u qiniu-ssl-renew-wildcard.service -n 50 --no-pager | grep -A2 origin_console_preflight

sha256sum ~/.acme.sh/crowntime.cn/fullchain.cer      # the value the message quotes
openssl x509 -in ~/.acme.sh/crowntime.cn/fullchain.cer -noout -subject -dates
```

### 2. Upload the renewed certificate in the Qiniu console

**证书管理 / SSL 证书服务 → 上传证书**: paste the certificate bundle
(`~/.acme.sh/crowntime.cn/fullchain.cer`) and its private key
(`~/.acme.sh/crowntime.cn/crowntime.cn.key`), named recognizably, e.g.
`crowntime.cn-<unix-ts>`. This step exists because the API upload has not run.

> The private key must be read from the host file. Never paste it into chat,
> tickets, logs, or a shell command line (argv is visible in `ps`).

### 3. Bind it in the Kodo console

**对象存储 → 空间 `365-wecom-media` → 域名管理 → `media-origin.crowntime.cn` →
配置 HTTPS → 开启 HTTPS → 更换证书 →** select the certificate uploaded in step 2.

### 4. Verify the live edge, per resolved IP

The origin resolves to a pool of edge IPs; a single probe is not proof.

```bash
EXPECT=$(openssl x509 -in ~/.acme.sh/crowntime.cn/fullchain.cer -outform DER | sha256sum | cut -d' ' -f1)
for ip in $(getent ahosts media-origin.crowntime.cn | awk '{print $1}' | sort -u); do
  got=$(echo | openssl s_client -connect "$ip:443" -servername media-origin.crowntime.cn 2>/dev/null \
        | openssl x509 -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
  [ "$got" = "$EXPECT" ] && echo "OK   $ip" || echo "FAIL $ip ${got:0:12}"
done
curl -sS -o /dev/null -w 'http=%{http_code} tls_verify=%{ssl_verify_result}\n' https://media-origin.crowntime.cn/
```

Every IP must match and `tls_verify` must be `0`. An `http=401` is the
object-authorization response, not a TLS failure.

### 5. Record the console-binding marker

```bash
sha256sum ~/.acme.sh/crowntime.cn/fullchain.cer | awk '{print $1}' \
  > ~/.acme.sh/crowntime.cn/.origin_console_fp
```

The marker is the **fullchain file** hash (exactly what the script compares),
not the leaf DER hash used in step 4. Every new certificate invalidates it.

### 6. Re-run the service once to complete the deployment

This run performs exactly one API upload and one CDN bind (the console already
binds the same certificate content, so the live leaf does not change), excuses
the expected origin 612, and records `.deployed_fp` / `.deployed_certid`.

```bash
sudo systemctl start qiniu-ssl-renew-wildcard.service
systemctl show qiniu-ssl-renew-wildcard.service -p Result -p ExecMainStatus
awk '/Wildcard SSL renewal started/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' \
  /var/log/qiniu-ssl-renew/renew.log | tail -15
# expect: uploaded — certID=…  / bound to media.crowntime.cn (CDN)
#         origin … excused by the matching marker
#         [OK] wildcard deployment complete
```

### 7. Require a pure no-op on the next run

```bash
sudo systemctl start qiniu-ssl-renew-wildcard.service
# expect: nginx already serves the current certificate — no copy or reload
#         live CDN TLS leaf matches the current local certificate
#         origin console-binding marker matches the current cert
#         cert already deployed — nothing to do
```

If step 6 or 7 dies at `origin_console_preflight` again, the marker does not
match the current certificate — recheck step 5 (fullchain hash, not leaf hash).

## pdf-vault-files.crowntime.cn (bucket `pdf-vault-prod`)

Same console-only limitation, but **nothing in `ssl-renew/` manages it**: no
timer, no acme.sh entry, no marker. It is rebound by hand and therefore rides an
expiring certificate until someone notices — the 2026-11-25 audit found it still
serving a certificate that expired on 2026-12-17.

- Certificate source: the shared wildcard `~/.acme.sh/crowntime.cn/`
  (covers `*.crowntime.cn`). Do **not** issue a separate certificate for it.
- Procedure: steps 2–4 above with bucket `pdf-vault-prod` and domain
  `pdf-vault-files.crowntime.cn`, then verify the PDFVault download path
  end-to-end against `PDFVAULT_QINIU_DOWNLOAD_DOMAIN`.
- No marker and no service restart — there is no automation to unblock.
- Detection hint: this binding is only visible indirectly in the Qiniu
  certificate list, as `400911 证书已绑定存储域名 空间名:pdf-vault-prod` when
  deleting the certificate. If a certificate refuses to delete, something is
  still bound to it — find the domain before retrying.

## If Qiniu ships a source-domain HTTPS API later

Replace the origin branch of `renew-wildcard.sh`: bind via the new API, keep the
fail-closed `die` on failure, keep a **preflight** that runs before any write
(otherwise the GH-239 loop returns), drop the `.origin_console_fp` marker, and
let the early-exit origin check use the API verify unconditionally. The bats
suite (`ssl-renew/tests/12_renew_wildcard.bats`, GH-232 + GH-239 cases) and the
Wiki behavior contract must be updated in the same ticket.
