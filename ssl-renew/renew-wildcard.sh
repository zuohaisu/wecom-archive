#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="/srv/apps/wecom-archive-365/current/ssl-renew"
if ! source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null; then
    printf '[ERROR] [bootstrap] stage=bootstrap domain=crowntime.cn message=unable-to-load-lib-common.sh\n' >&2
    exit 1
fi
CURRENT_STAGE="bootstrap"
if ! source "$SCRIPT_DIR/lib/qiniu.sh" 2>/dev/null; then
    die "unable to load required helper: lib/qiniu.sh"
fi

WILDCARD_DOMAIN="crowntime.cn"
WILDCARD_DIR="${CERT_DIR:-$HOME/.acme.sh/$WILDCARD_DOMAIN}"
FP_FILE="$WILDCARD_DIR/.deployed_fp"
TLS_OK_FILE="$WILDCARD_DIR/.tls_verified"
MISMATCH_FILE="$WILDCARD_DIR/.tls_mismatch_days"
# shellcheck disable=SC2034 # production parity: declared but intentionally unused
LOCK_FILE="$WILDCARD_DIR/.renew.lock"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"
ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"

# State files that make the idempotency check verifiable against Qiniu
# itself (GH-232): the fingerprint alone cannot prove both domains still
# bind the certificate — the 2026-09-19 production incident showed a bind
# failure being recorded as deployed. CERTID_FILE remembers which uploaded
# Qiniu certificate the fingerprint corresponds to so a matching run can
# re-verify the live bindings via lib/qiniu.sh::verify_certID_on_domain.
CERTID_FILE="$WILDCARD_DIR/.deployed_certid"
# Qiniu's CDN-domain HTTPS API cannot manage Kodo source domains at all —
# they answer HTTP 612 "no such domain" (media-origin.crowntime.cn is one,
# CNAME → iovip-z2.qiniuio.com). They are console-managed only. After an
# operator binds the current wildcard certificate to the origin domain in
# the Qiniu Console, they record the certificate fingerprint here so this
# script can distinguish a confirmed console binding from the
# silent-swallow bug. The marker is fingerprint-bound: any new certificate
# invalidates it and re-requires the console runbook.
ORIGIN_CONSOLE_FP_FILE="$WILDCARD_DIR/.origin_console_fp"

# Matches the Qiniu CDN-API signature for "domain is not in the CDN domain
# list" — Kodo source domains always answer this way (GH-232).
_qiniu_origin_api_invisible() {
    case "$1" in
        *"HTTP 612"* | *"no such domain"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Console runbook for the one binding this flow cannot automate. Referenced
# by every origin-bind/verify failure so the journald log is actionable.
_ORIGIN_CONSOLE_RUNBOOK="Qiniu Kodo source domains are console-managed only. Runbook: Qiniu Console → 对象存储 Kodo → 空间管理 → 空间 365-wecom-media → 域名管理 → 自定义源站域名 → $ORIGIN_DOMAIN → 配置 HTTPS → 更换证书 (select the just-uploaded *.crowntime.cn certificate), confirm with: openssl s_client -connect $ORIGIN_DOMAIN:443 -servername $ORIGIN_DOMAIN, then record the fingerprint: sha256sum $WILDCARD_DIR/fullchain.cer | cut -d' ' -f1 > $ORIGIN_CONSOLE_FP_FILE"
# The wildcard flow's domain is a script constant, not the EnvironmentFile's
# convention-only DOMAIN value. common.sh uses this only for safe diagnostics.
export SSL_RENEW_ALERT_DOMAIN="$WILDCARD_DOMAIN"

log "===== Wildcard SSL renewal started ====="

CURRENT_STAGE="runtime_dependency"
# acme.sh output is untrusted command output and must be filtered before it
# reaches the journal. Refuse to run it if filtering is not available.
if ! command -v sed >/dev/null 2>&1; then
    die "required secret filtering runtime dependency is unavailable"
fi

CURRENT_STAGE="acme_renew"
log "[INFO] Running acme.sh --renew for $WILDCARD_DOMAIN (*.crowntime.cn)"
set +e
"$ACME_SH" --renew --dns "$DNS_PROVIDER" -d "$WILDCARD_DOMAIN" -d "*.crowntime.cn" 2>&1 | filter_secrets
acme_exit=${PIPESTATUS[0]}
set -e
if [ "$acme_exit" -eq 0 ]; then log "[INFO] acme.sh --renew completed"
elif [ "$acme_exit" -eq 2 ]; then log "[INFO] acme.sh --renew skipped (not due yet)"
else die "acme.sh --renew failed (exit=$acme_exit)"
fi

CURRENT_STAGE="local_fingerprint"
[ -f "$WILDCARD_DIR/fullchain.cer" ] || die "fullchain.cer not found: $WILDCARD_DIR/fullchain.cer"
if ! command -v sha256sum >/dev/null 2>&1 || ! command -v awk >/dev/null 2>&1; then
    die "required fingerprint runtime dependency is unavailable"
fi
if ! local_fp=$(sha256sum "$WILDCARD_DIR/fullchain.cer" | awk '{print $1}'); then
    die "failed to calculate local certificate fingerprint"
fi
[ -n "$local_fp" ] || die "local certificate fingerprint is empty"
log "[INFO] local_fp=$local_fp"

deployed_fp="" && [ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")
if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
    # Fingerprint match is NOT sufficient proof of a healthy deployment —
    # verify the live Qiniu bindings before declaring "nothing to do"
    # (GH-232: a silent origin-bind failure was masked here for weeks).
    CURRENT_STAGE="qiniu_verify"
    if ! command -v "$QINIU_HELPER_PYTHON" >/dev/null 2>&1 \
       || [ ! -f "$QINIU_HELPER_SCRIPT" ] \
       || ! command -v jq >/dev/null 2>&1; then
        die "required Qiniu helper runtime dependency is unavailable"
    fi

    deployed_certid="" && [ -f "$CERTID_FILE" ] && deployed_certid=$(cat "$CERTID_FILE")
    if [ -z "$deployed_certid" ]; then
        # Legacy state from before GH-232 recorded no certID — re-deploy so
        # the Qiniu bindings get verified and the state file is written.
        log "[INFO] cert fingerprint matches but no recorded certID — re-deploying to verify Qiniu bindings"
    elif ! verify_err=$(verify_certID_on_domain "$CDN_DOMAIN" "$deployed_certid" 2>&1); then
        log "[INFO] $CDN_DOMAIN no longer binds certID=$deployed_certid ($verify_err) — re-deploying"
    elif ! verify_err=$(verify_certID_on_domain "$ORIGIN_DOMAIN" "$deployed_certid" 2>&1); then
        if _qiniu_origin_api_invisible "$verify_err"; then
            if [ -f "$ORIGIN_CONSOLE_FP_FILE" ] && [ "$(cat "$ORIGIN_CONSOLE_FP_FILE")" = "$local_fp" ]; then
                log "[INFO] $ORIGIN_DOMAIN is not visible to the Qiniu CDN API (HTTP 612); console binding confirmed via $ORIGIN_CONSOLE_FP_FILE"
                log "[INFO] cert already deployed — nothing to do"
                exit "$EXIT_OK"
            fi
            die "$ORIGIN_DOMAIN is not manageable via the Qiniu CDN API and no console binding is recorded. $_ORIGIN_CONSOLE_RUNBOOK"
        fi
        log "[INFO] $ORIGIN_DOMAIN no longer binds certID=$deployed_certid ($verify_err) — re-deploying"
    else
        log "[INFO] cert already deployed — nothing to do"
        exit "$EXIT_OK"
    fi
fi

CURRENT_STAGE="qiniu_upload"
if ! command -v "$QINIU_HELPER_PYTHON" >/dev/null 2>&1 \
   || [ ! -f "$QINIU_HELPER_SCRIPT" ] \
   || ! command -v jq >/dev/null 2>&1; then
    die "required Qiniu helper runtime dependency is unavailable"
fi
upload_output=$(deploy_to_qiniu "$WILDCARD_DOMAIN" "$WILDCARD_DIR" 2>&1) && upload_status=$? || upload_status=$?
if [ "$upload_status" -ne 0 ]; then die "$upload_output"; fi
certID="$upload_output"
log "[INFO] uploaded — certID=$certID"

CURRENT_STAGE="qiniu_bind_cdn"
if ! bind_err=$(bind_cert_to_domain "$CDN_DOMAIN" "$certID" 2>&1); then
    die "CDN bind failed: $bind_err"
fi
log "[INFO] bound to $CDN_DOMAIN (CDN)"

CURRENT_STAGE="qiniu_bind_origin"
if ! bind_err=$(bind_cert_to_domain "$ORIGIN_DOMAIN" "$certID" 2>&1); then
    if [ -f "$ORIGIN_CONSOLE_FP_FILE" ] && [ "$(cat "$ORIGIN_CONSOLE_FP_FILE")" = "$local_fp" ]; then
        log "[INFO] origin bind via API failed ($bind_err); console binding recorded in $ORIGIN_CONSOLE_FP_FILE — continuing"
    else
        die "Origin bind failed: $bind_err. $_ORIGIN_CONSOLE_RUNBOOK"
    fi
else
    log "[INFO] bound to $ORIGIN_DOMAIN (origin)"
fi

echo "$local_fp" >"$FP_FILE"
echo "$certID" >"$CERTID_FILE"
rm -f "$TLS_OK_FILE" "$MISMATCH_FILE"

CURRENT_STAGE="nginx_deploy"
# Deploy the same wildcard cert to the local nginx (RND-261 wildcard consolidation).
NGINX_CERT_DIR="${NGINX_CERT_DIR:-/srv/apps/wecom-archive-365/shared/certs/wildcard.crowntime.cn}"
if [ -d "$NGINX_CERT_DIR" ] && [ -f "$WILDCARD_DIR/fullchain.cer" ] && [ -f "$WILDCARD_DIR/crowntime.cn.key" ]; then
    if cp -f "$WILDCARD_DIR/fullchain.cer" "$NGINX_CERT_DIR/fullchain.pem" \
       && cp -f "$WILDCARD_DIR/crowntime.cn.key" "$NGINX_CERT_DIR/privkey.pem"; then
        chmod 640 "$NGINX_CERT_DIR/fullchain.pem" "$NGINX_CERT_DIR/privkey.pem" 2>/dev/null || true
        if sudo -n /usr/bin/systemctl reload nginx 2>/dev/null; then
            log "[INFO] nginx cert deployed + reloaded"
        else
            warn "nginx reload failed (sudo) — cert files updated, manual reload needed"
        fi
    else
        warn "nginx cert copy failed — files NOT updated"
    fi
else
    warn "nginx deploy skipped (dir or source files missing)"
fi

# shellcheck disable=SC2034 # production parity: retained diagnostic assignment
CURRENT_STAGE="done"
log "[OK] wildcard deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
