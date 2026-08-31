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
    log "[INFO] cert already deployed — nothing to do"
    exit "$EXIT_OK"
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
    warn "Origin bind failed (may need Qiniu Console config): $bind_err"
else
    log "[INFO] bound to $ORIGIN_DOMAIN (origin)"
fi

echo "$local_fp" >"$FP_FILE"
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
