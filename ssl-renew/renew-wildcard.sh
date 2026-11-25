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
# Local nginx certificate directory (RND-261 wildcard consolidation). Deployed
# independently of the Qiniu path — see deploy_cert_to_nginx (GH-239).
NGINX_CERT_DIR="${NGINX_CERT_DIR:-/srv/apps/wecom-archive-365/shared/certs/wildcard.crowntime.cn}"
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

# leaf_cert_sha256 <pem-file> — DER SHA-256 of the FIRST (leaf) certificate in
# a PEM bundle. Deliberately not a whole-file hash: fullchain.cer and nginx's
# fullchain.pem may carry different chain members while serving the same leaf.
leaf_cert_sha256() {
    local pem_file="$1"
    openssl x509 -in "$pem_file" -outform DER 2>/dev/null | sha256sum | awk '{print $1}' || true
}

# deploy_cert_to_nginx — copy the current wildcard cert into the local nginx
# cert directory and reload nginx, but only when nginx does not already serve
# that leaf certificate.
#
# GH-239: this is a purely local operation with its own fingerprint, so it is
# invoked before the Qiniu early-exit and before the Qiniu origin gate. It must
# never be gated on a provider precondition: doing so left nginx serving the
# previous certificate for a week while every run retried the CDN write.
# Warn-only by contract — a copy or reload failure never aborts the run.
deploy_cert_to_nginx() {
    local source_chain="$WILDCARD_DIR/fullchain.cer"
    local source_key="$WILDCARD_DIR/$WILDCARD_DOMAIN.key"
    local target_chain="$NGINX_CERT_DIR/fullchain.pem"
    local target_key="$NGINX_CERT_DIR/privkey.pem"
    local source_leaf target_leaf

    CURRENT_STAGE="nginx_deploy"

    if is_dry_run; then
        log_dry "would compare the nginx leaf certificate in $target_chain against $source_chain; would copy $source_chain -> $target_chain and $source_key -> $target_key, then reload nginx, only when the leaves differ"
        return 0
    fi

    if [ ! -d "$NGINX_CERT_DIR" ] || [ ! -f "$source_key" ]; then
        warn "nginx deploy skipped (dir or source files missing)"
        return 0
    fi

    # A missing target file (or key) is not a comparison — it is a deploy.
    if [ -f "$target_chain" ] && [ -f "$target_key" ]; then
        if ! command -v openssl >/dev/null 2>&1; then
            warn "nginx deploy skipped (openssl unavailable — cannot compare the served leaf certificate)"
            return 0
        fi
        source_leaf=$(leaf_cert_sha256 "$source_chain")
        target_leaf=$(leaf_cert_sha256 "$target_chain")
        if [ -n "$source_leaf" ] && [ "$source_leaf" = "$target_leaf" ]; then
            log "[INFO] nginx already serves the current certificate — no copy or reload"
            return 0
        fi
    fi

    if cp -f "$source_chain" "$target_chain" \
       && cp -f "$source_key" "$target_key"; then
        chmod 640 "$target_chain" "$target_key" 2>/dev/null || true
        if sudo -n /usr/bin/systemctl reload nginx 2>/dev/null; then
            log "[INFO] nginx cert deployed + reloaded"
        else
            warn "nginx reload failed (sudo) — cert files updated, manual reload needed"
        fi
    else
        warn "nginx cert copy failed — files NOT updated"
    fi
}

log "===== Wildcard SSL renewal started ====="

CURRENT_STAGE="runtime_dependency"
# acme.sh output is untrusted command output and must be filtered before it
# reaches the journal. Refuse to run it if filtering is not available.
if ! command -v sed >/dev/null 2>&1; then
    die "required secret filtering runtime dependency is unavailable"
fi

CURRENT_STAGE="acme_renew"
if is_dry_run; then
    # DRY_RUN must not mix simulation with real execution (GH-235 follow-up):
    # acme.sh performs real DNS-01 mayhem and can really renew a certificate,
    # so in dry-run the plan is printed and acme.sh is never started.
    log_dry "would run: $ACME_SH --renew --dns $DNS_PROVIDER -d $WILDCARD_DOMAIN -d *.crowntime.cn"
    log_dry "  (acme.sh, DNSPod, and the ACME CA are never contacted in dry-run)"
else
    log "[INFO] Running acme.sh --renew for $WILDCARD_DOMAIN (*.crowntime.cn)"
    set +e
    "$ACME_SH" --renew --dns "$DNS_PROVIDER" -d "$WILDCARD_DOMAIN" -d "*.crowntime.cn" 2>&1 | filter_secrets
    acme_exit=${PIPESTATUS[0]}
    if [ "$acme_exit" -eq 0 ]; then log "[INFO] acme.sh --renew completed"
    elif [ "$acme_exit" -eq 2 ]; then log "[INFO] acme.sh --renew skipped (not due yet)"
    else die "acme.sh --renew failed (exit=$acme_exit)"
    fi
fi
# Production parity: errexit is enabled from here on in BOTH modes.
set -e

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

# GH-239: deploy the local nginx copy FIRST — before the Qiniu early-exit, so
# a converged Qiniu binding can never skip it, and before the Qiniu origin
# gate, so an unrelated provider precondition can never starve it.
deploy_cert_to_nginx

deployed_fp="" && [ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")
if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
    CURRENT_STAGE="qiniu_verify_deployed"
    verify_failures=""
    if is_dry_run; then
        log_dry "would verify the CDN binding via the Qiniu API and, if that API is unavailable (HTTP 404), via an independent live TLS leaf comparison" >&2
    fi
    deployed_certid="" && [ -f "$CERTID_FILE" ] && deployed_certid=$(cat "$CERTID_FILE")
    if [ -z "$deployed_certid" ]; then
        verify_failures="no recorded certID ($CERTID_FILE missing) — cannot verify Qiniu bindings"
    else
        cdn_verify_err=""
        if cdn_verify_err=$(verify_certID_on_domain "$CDN_DOMAIN" "$deployed_certid" 2>&1); then
            cdn_verify_status=0
        else
            cdn_verify_status=$?
        fi
        case "$cdn_verify_status" in
        0) ;;
        1) verify_failures="CDN API reports a certID mismatch: $cdn_verify_err" ;;
        2)
            # GH-235: this API answers HTTP 404 for the CDN domain in
            # production, so it can never prove the binding either way. That
            # is an UNKNOWN state, not drift — falling through to a full
            # redeploy here is what rebound the same certificate every day.
            # Require independent evidence of what the domain actually serves
            # before either exiting or rewriting the binding.
            log "[INFO] Qiniu CDN verify API returned HTTP 404 (binding not confirmable via API) — requiring independent live TLS evidence"
            CURRENT_STAGE="qiniu_live_tls_verify"
            live_tls_err=""
            if live_tls_err=$(verify_live_tls_cert "$CDN_DOMAIN" "$WILDCARD_DIR/fullchain.cer" 2>&1); then
                log "[INFO] live CDN TLS leaf matches the current local certificate — binding treated as confirmed"
            else
                live_tls_status=$?
                case "$live_tls_status" in
                1) verify_failures="live CDN TLS leaf does not match the current certificate: $live_tls_err" ;;
                *) die "live CDN TLS verification failed or returned invalid evidence; refusing to assume the binding is correct or to rebind: $live_tls_err" ;;
                esac
            fi
            CURRENT_STAGE="qiniu_verify_deployed"
            ;;
        *) die "CDN binding API verification failed; refusing to rebind without confirmed live certificate drift: $cdn_verify_err" ;;
        esac

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
    log "[INFO] fingerprint matches but live binding verification failed ($verify_failures) — continuing with full deployment"
fi

# GH-239: the operator-recorded origin console marker is a PRECONDITION of the
# deployment path, not a step inside it. Qiniu's httpsconf API cannot bind a
# Kodo object-storage source domain (HTTP 612 "no such domain"), so the origin
# step can only pass when an operator has console-bound the CURRENT certificate
# and recorded its fingerprint. Checking it after the upload + CDN bind left a
# partial deployment with NO recorded state, which re-uploaded and re-bound the
# same certificate on every following run (one provider notification per day
# for a week). Check it before the first Qiniu write instead: no upload, no
# bind, no notification.
CURRENT_STAGE="qiniu_upload"
if ! command -v "$QINIU_HELPER_PYTHON" >/dev/null 2>&1 \
   || [ ! -f "$QINIU_HELPER_SCRIPT" ] \
   || ! command -v jq >/dev/null 2>&1; then
    die "required Qiniu helper runtime dependency is unavailable"
fi

CURRENT_STAGE="origin_console_preflight"
if ! origin_console_binding_confirmed "$local_fp"; then
    die "Origin console-binding preflight failed: local certificate fingerprint $local_fp is not recorded in $CONSOLE_FP_FILE, and Qiniu's httpsconf API cannot bind a Kodo object-storage source domain (HTTP 612 'no such domain'). " \
        "Console-bind this certificate to $ORIGIN_DOMAIN per docs/operations/qiniu-origin-domain-https-runbook.md " \
        "(Kodo bucket $ORIGIN_BUCKET > 域名管理 > $ORIGIN_DOMAIN > 配置 HTTPS), then record fingerprint $local_fp in $CONSOLE_FP_FILE and re-run. " \
        "Nothing was uploaded and no CDN binding was changed."
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
if is_dry_run; then
    # Dry-run must never write deployment state (GH-235 follow-up): a
    # simulated upload returns the placeholder certID, and recording that
    # would corrupt the very state the next real run verifies against.
    log_dry "would record the current fingerprint and the uploaded certID in $FP_FILE and $CERTID_FILE, then clear the TLS state markers"
else
    echo "$local_fp" >"$FP_FILE"
    echo "$certID" >"$CERTID_FILE"
    rm -f "$TLS_OK_FILE" "$MISMATCH_FILE"
fi

# The local nginx copy is deployed earlier (deploy_cert_to_nginx, GH-239) so a
# failing Qiniu gate can never starve it; there is deliberately no second copy
# or reload here.

# shellcheck disable=SC2034 # production parity: retained diagnostic assignment
CURRENT_STAGE="done"
log "[OK] wildcard deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
