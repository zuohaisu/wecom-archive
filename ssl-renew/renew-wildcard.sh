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
ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
TLS_PROBE_TIMEOUT="${TLS_PROBE_TIMEOUT:-10}"
# Test hook: override the "host:port" the live-certificate probe dials.
ORIGIN_TLS_CONNECT="${ORIGIN_TLS_CONNECT:-$ORIGIN_DOMAIN:443}"
# The wildcard flow's domain is a script constant, not the EnvironmentFile's
# convention-only DOMAIN value. common.sh uses this only for safe diagnostics.
export SSL_RENEW_ALERT_DOMAIN="$WILDCARD_DOMAIN"

# verify_live_tls_cert <domain> <expected_leaf_sha256> [connect host:port]
# Compares the certificate a domain actually serves against the local leaf.
# This is the ONLY available post-deploy check for the Qiniu object-storage
# source domain: the CDN httpsconf API returns HTTP 612 "no such domain"
# for source domains, so neither bind nor verify_certID_on_domain can work
# there (GH-232). Fails closed: any probe/parse problem counts as
# "not verified", never as success.
verify_live_tls_cert() {
    local domain="$1" expected_fp="$2" connect="${3:-$1:443}"

    if is_dry_run; then
        log_dry "would probe $domain (connect $connect) and compare the served leaf fingerprint"
        return 0
    fi

    local served served_fp
    if ! served=$(run_with_timeout "$TLS_PROBE_TIMEOUT" \
        openssl s_client -servername "$domain" -connect "$connect" </dev/null 2>/dev/null); then
        echo "TLS probe of $domain failed or timed out"
        return 1
    fi
    # DER never round-trips through a shell variable (NUL bytes) — keep the
    # served PEM text only, and hash via an uninterrupted pipeline.
    if ! printf '%s\n' "$served" | grep -q -- '-----BEGIN CERTIFICATE-----'; then
        echo "no certificate served by $domain"
        return 1
    fi
    served_fp=$(printf '%s\n' "$served" | openssl x509 -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
    if [ "$served_fp" != "$expected_fp" ]; then
        echo "live certificate mismatch for $domain — served_fp=$served_fp expected_fp=$expected_fp"
        return 1
    fi
    return 0
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

# sha256 of the leaf certificate in DER form — comparable against the
# certificate a TLS endpoint actually serves (the PEM-file sha256 above is
# not). Required by the origin-domain early-exit verification (GH-232).
if ! command -v openssl >/dev/null 2>&1; then
    die "required fingerprint runtime dependency is unavailable"
fi
if ! local_leaf_fp=$(openssl x509 -in "$WILDCARD_DIR/fullchain.cer" -outform DER 2>/dev/null | sha256sum | awk '{print $1}'); then
    die "failed to calculate local leaf certificate fingerprint"
fi
[ -n "$local_leaf_fp" ] || die "local leaf certificate fingerprint is empty"

deployed_fp="" && [ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")
if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
    # GH-232: a matching fingerprint alone proved nothing — the 2026-09-19
    # run uploaded + CDN-bound the new cert while the origin bind failed and
    # was only warned on, after which every daily run early-exited here and
    # the drift stayed invisible. Verify both Qiniu-side bindings before
    # trusting the fingerprint; any doubt falls through to a full
    # (idempotent) deployment instead of masking drift.
    CURRENT_STAGE="qiniu_verify_deployed"
    verify_failures=""
    deployed_certid="" && [ -f "$CERTID_FILE" ] && deployed_certid=$(cat "$CERTID_FILE")
    if [ -z "$deployed_certid" ]; then
        verify_failures="deployed certID unknown ($CERTID_FILE missing) — cannot verify CDN binding"
    elif ! cdn_verify_err=$(verify_certID_on_domain "$CDN_DOMAIN" "$deployed_certid" 2>&1); then
        verify_failures="CDN binding verify failed: $cdn_verify_err"
    fi
    if ! origin_tls_err=$(verify_live_tls_cert "$ORIGIN_DOMAIN" "$local_leaf_fp" "$ORIGIN_TLS_CONNECT" 2>&1); then
        verify_failures="${verify_failures:+$verify_failures; }origin live-certificate verify failed: $origin_tls_err"
    fi
    if [ -z "$verify_failures" ]; then
        log "[INFO] cert already deployed and verified on $CDN_DOMAIN (certID=$deployed_certid) and $ORIGIN_DOMAIN (live TLS) — nothing to do"
        exit "$EXIT_OK"
    fi
    log "[INFO] fingerprint matches but deployment verification failed ($verify_failures) — continuing with full deployment"
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

# Record "uploaded + CDN-bound" state BEFORE the origin step. The early-exit
# path above re-verifies both domains (API certID for CDN, live TLS for the
# origin) before trusting these files, so recording state here cannot mask a
# failed origin bind — it is what lets the next run converge once the origin
# cert gets bound (manually, per the runbook below) instead of re-uploading
# and re-alerting forever.
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

# Origin bind is deliberately LAST: it is expected to fail for this
# object-storage source domain (see below), and that failure must not block
# the independent local nginx deployment above.
CURRENT_STAGE="qiniu_bind_origin"
if ! bind_err=$(bind_cert_to_domain "$ORIGIN_DOMAIN" "$certID" 2>&1); then
    # Fail closed (GH-232): a silent warn here is exactly how the origin
    # cert drifted to 5 days from expiry in production. Qiniu's httpsconf
    # API manages CDN domains only — for object-storage source domains it
    # returns HTTP 612 "no such domain" — so this bind is expected to keep
    # failing until Qiniu ships source-domain API support. The uploaded
    # certID must instead be bound manually via the console runbook;
    # OnFailure=wecom-job-failure-alert@%n.service turns this die into an
    # operator alert, and the early-exit live-TLS verify stops the alert
    # once the console bind is done.
    die "Origin bind failed for $ORIGIN_DOMAIN — uploaded certID=$certID is NOT bound. " \
        "Qiniu's httpsconf API does not manage object-storage source domains (HTTP 612 'no such domain'). " \
        "Bind certID=$certID manually: Qiniu console > Kodo bucket $ORIGIN_BUCKET > 域名管理 > $ORIGIN_DOMAIN > 配置 HTTPS " \
        "(runbook: docs/operations/qiniu-origin-domain-https-runbook.md). " \
        "Underlying error: $bind_err"
fi
log "[INFO] bound to $ORIGIN_DOMAIN (origin)"

# shellcheck disable=SC2034 # production parity: retained diagnostic assignment
CURRENT_STAGE="done"
log "[OK] wildcard deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
