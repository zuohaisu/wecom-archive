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
CERTID_FILE="$WILDCARD_DIR/.deployed_certid"
TLS_OK_FILE="$WILDCARD_DIR/.tls_verified"
MISMATCH_FILE="$WILDCARD_DIR/.tls_mismatch_days"
# shellcheck disable=SC2034 # production parity: declared but intentionally unused
LOCK_FILE="$WILDCARD_DIR/.renew.lock"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"
# Bucket the origin source domain is bound to — used in operator-facing
# runbook guidance only (GH-232: the origin domain is an object-storage
# source domain, which Qiniu's httpsconf API cannot manage).
ORIGIN_BUCKET="365-wecom-media"
# GH-232: operator-recorded console-binding marker. Content: the first field
# of `sha256sum fullchain.cer`, written by the operator AFTER binding the
# current certificate to the origin domain via the Kodo console. While it
# matches the current certificate, origin API failures (612 on bind/verify)
# are excused; any new certificate invalidates it and re-requires the runbook.
CONSOLE_FP_FILE="$WILDCARD_DIR/.origin_console_fp"
ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
# The wildcard flow's domain is a script constant, not the EnvironmentFile's
# convention-only DOMAIN value. common.sh uses this only for safe diagnostics.
export SSL_RENEW_ALERT_DOMAIN="$WILDCARD_DOMAIN"

# origin_console_binding_confirmed <local_fp> — true when the operator has
# recorded the CURRENT certificate's fingerprint in .origin_console_fp after
# console-binding it to the origin domain. This is the only available
# confirmation for a Kodo source domain: Qiniu's CDN httpsconf API returns
# HTTP 612 "no such domain" for source domains on bind AND verify (GH-232).
# Fails closed: a missing/empty/stale marker is never treated as confirmed.
origin_console_binding_confirmed() {
    local local_fp="$1" marker=""
    [ -f "$CONSOLE_FP_FILE" ] && marker=$(cat "$CONSOLE_FP_FILE")
    [ -n "$marker" ] && [ "$marker" = "$local_fp" ]
}

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
    # GH-232: a matching fingerprint alone proved nothing — the 2026-09-19
    # run uploaded + CDN-bound the new cert while the origin bind failed and
    # was only warned on, after which every daily run early-exited here and
    # the drift stayed invisible. Reload the recorded certID and re-verify
    # BOTH live Qiniu bindings before trusting the fingerprint; any doubt
    # falls through to a full (idempotent) deployment instead of masking
    # drift.
    CURRENT_STAGE="qiniu_verify_deployed"
    verify_failures=""
    deployed_certid="" && [ -f "$CERTID_FILE" ] && deployed_certid=$(cat "$CERTID_FILE")
    if [ -z "$deployed_certid" ]; then
        verify_failures="no recorded certID ($CERTID_FILE missing) — cannot verify Qiniu bindings"
    else
        if ! cdn_verify_err=$(verify_certID_on_domain "$CDN_DOMAIN" "$deployed_certid" 2>&1); then
            verify_failures="CDN binding verify failed: $cdn_verify_err"
        fi
        if origin_console_binding_confirmed "$local_fp"; then
            log "[INFO] origin console-binding marker matches the current cert — origin API verify skipped (source domains answer HTTP 612)"
        elif ! origin_verify_err=$(verify_certID_on_domain "$ORIGIN_DOMAIN" "$deployed_certid" 2>&1); then
            verify_failures="${verify_failures:+$verify_failures; }origin binding verify failed (no matching console marker): $origin_verify_err"
        fi
    fi
    if [ -z "$verify_failures" ]; then
        log "[INFO] cert already deployed — nothing to do"
        exit "$EXIT_OK"
    fi
    log "[INFO] fingerprint matches but Qiniu binding verification failed ($verify_failures) — continuing with full deployment"
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
    # Fail closed (GH-232): a silent warn here is exactly how the origin
    # cert drifted to 5 days from expiry in production. Qiniu's httpsconf
    # API manages CDN domains only — object-storage source domains answer
    # HTTP 612 "no such domain" — so the console is the only supported bind
    # path. The failure is excused only while the operator-recorded console
    # marker (.origin_console_fp) matches the current certificate.
    if origin_console_binding_confirmed "$local_fp"; then
        warn "Origin API bind failed (expected for Kodo source domains — HTTP 612) but .origin_console_fp matches the current cert; continuing on the documented console binding: $bind_err"
    else
        die "Origin bind failed for $ORIGIN_DOMAIN — uploaded certID=$certID is NOT bound. " \
            "Qiniu's httpsconf API cannot manage object-storage source domains (HTTP 612 'no such domain'). " \
            "Console-bind certID=$certID per docs/operations/qiniu-origin-domain-https-runbook.md " \
            "(Kodo bucket $ORIGIN_BUCKET > 域名管理 > $ORIGIN_DOMAIN > 配置 HTTPS), then record the current cert fingerprint into $CONSOLE_FP_FILE. " \
            "Underlying error: $bind_err"
    fi
else
    log "[INFO] bound to $ORIGIN_DOMAIN (origin)"
fi

# Record the Qiniu deployment only now: CDN bind succeeded and the origin
# bind either succeeded or was console-marker-excused. An origin hard
# failure above records nothing (GH-232).
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
