#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="/srv/apps/wecom-archive-365/current/ssl-renew"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/qiniu.sh"

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

log "===== Wildcard SSL renewal started ====="

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
[ -f "$WILDCARD_DIR/fullchain.cer" ] || die "fullchain.cer not found"
local_fp=$(sha256sum "$WILDCARD_DIR/fullchain.cer" | awk '{print $1}')
log "[INFO] local_fp=$local_fp"

deployed_fp="" && [ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")
if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
    log "[INFO] cert already deployed — nothing to do"
    exit "$EXIT_OK"
fi

CURRENT_STAGE="qiniu_upload"
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
