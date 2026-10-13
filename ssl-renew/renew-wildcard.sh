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
# GH-231: the Qiniu origin-domain binding is opt-in. When ORIGIN_DOMAIN is
# set (e.g. media-origin.crowntime.cn) every run binds it on deploy and
# reconciles it — together with the CDN domain — against the newest local
# certificate. Empty/unset means NO origin activity at all (no deploy-path
# bind, no reconciliation queries), so environments without an origin domain
# behave exactly as before this feature existed.
ORIGIN_DOMAIN="${ORIGIN_DOMAIN:-}"
# GH-231: optional diagnostics context for reconciliation logs (which bucket
# the reconciled domains belong to). Never used in any API request.
QINIU_BUCKET="${QINIU_BUCKET:-}"
# GH-231: a bound certificate expiring within this many days is reconciled
# even when its fingerprint still matches the newest local cert.
ORIGIN_CERT_MIN_VALID_DAYS="${ORIGIN_CERT_MIN_VALID_DAYS:-14}"
case "$ORIGIN_CERT_MIN_VALID_DAYS" in
    ''|*[!0-9]*)
        warn "ORIGIN_CERT_MIN_VALID_DAYS='$ORIGIN_CERT_MIN_VALID_DAYS' is not a non-negative integer — using default 14"
        ORIGIN_CERT_MIN_VALID_DAYS=14
        ;;
esac
ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
# The wildcard flow's domain is a script constant, not the EnvironmentFile's
# convention-only DOMAIN value. common.sh uses this only for safe diagnostics.
export SSL_RENEW_ALERT_DOMAIN="$WILDCARD_DOMAIN"

# ── GH-231: reconcile Qiniu bindings against the newest local cert. ─────
# Runs on the common "fingerprint already deployed" pass (acme.sh skipped,
# nothing locally changed) — exactly the pass that historically exited
# before ever looking at what Qiniu was actually serving, which is how
# media-origin.crowntime.cn kept a July cert while the CDN domain moved on.
#
# Decision rule per reconciled domain (the reconciliation is opt-in via
# ORIGIN_DOMAIN; once enabled, both the CDN domain and the origin domain
# are checked): reconcile (re-upload + re-bind) when the
# bound certificate's leaf fingerprint differs from the newest local cert's,
# when the bound certID no longer resolves in the cert store, or when the
# bound cert expires within ORIGIN_CERT_MIN_VALID_DAYS days.
#
# Mode contracts (ssl-renew/README.md):
#   dry-run  — the queries are read-only GETs; every write step goes through
#              the lib/qiniu.sh dry-run guards, so zero external writes
#              happen and the plan is printed with the [DRY-RUN] prefix.
#   staging  — skipped entirely (staging certificates are never deployed).
#   production — the only mode that actually re-uploads / re-binds.
# Query failures are warn-only and skip this run's reconciliation: the
# renewal itself already succeeded, the condition is re-checked tomorrow,
# and a monitoring read must not fail the service (OnFailure would page).
reconcile_qiniu_bindings() {
    if [ -z "$ORIGIN_DOMAIN" ]; then
        # Feature opt-in: without ORIGIN_DOMAIN this flow behaves exactly as
        # before GH-231 — no binding queries, no writes, no extra API calls.
        return 0
    fi
    if is_staging; then
        log "[INFO] staging mode — skipping binding reconciliation (staging certs are never deployed)"
        return 0
    fi

    if ! command -v openssl >/dev/null 2>&1; then
        warn "required binding-reconcile runtime dependency is unavailable: openssl"
        return 0
    fi
    # Comparable counterpart of the helper's leaf fingerprint: DER of the
    # leaf certificate, hashed — NOT the fullchain file hash used for
    # .deployed_fp (a chain file and its leaf must not be compared).
    local leaf_fp
    if ! leaf_fp=$(openssl x509 -in "$WILDCARD_DIR/fullchain.cer" -outform DER 2>/dev/null | sha256sum | awk '{print $1}') \
       || [ -z "$leaf_fp" ]; then
        warn "failed to calculate local leaf-certificate fingerprint — binding reconciliation skipped"
        return 0
    fi
    log "[INFO] local leaf_fp=${leaf_fp:0:12}…"

    local domains=("$CDN_DOMAIN")
    if [ -n "$ORIGIN_DOMAIN" ]; then
        domains+=("$ORIGIN_DOMAIN")
    fi

    local now
    now=$(date +%s)

    local -a stale_domains=()
    local domain query_out rest cert_id bound_fp bind_notafter detail_missing decision days_left bind_fp_short
    for domain in "${domains[@]}"; do
        decision="in-sync"
        cert_id=""
        bound_fp=""
        bind_notafter=""
        detail_missing=""
        if ! query_out=$(query_domain_cert_binding "$domain"); then
            warn "binding query failed for $domain — reconciliation skipped for this domain this run"
            continue
        fi
        # query_domain_cert_binding emits certId|certFingerprint|notAfter|certDetailMissing.
        cert_id="${query_out%%|*}"
        rest="${query_out#*|}"
        bound_fp="${rest%%|*}"
        rest="${rest#*|}"
        bind_notafter="${rest%%|*}"
        rest="${rest#*|}"
        detail_missing="${rest%%|*}"

        # Decision precedence matters: certDetailMissing (a bound certID that
        # no longer resolves) is authoritative even though it leaves the
        # fingerprint empty — only a NULL certId means "no binding at all".
        if [ "$detail_missing" = "1" ]; then
            decision="bound-cert-missing"
        elif [ -z "$bound_fp" ]; then
            decision="no-binding"
        elif [ "$bound_fp" != "$leaf_fp" ]; then
            decision="stale-cert"
        fi

        days_left=""
        if [ "$decision" = "in-sync" ] && [ -n "$bind_notafter" ]; then
            case "$bind_notafter" in
                ''|*[!0-9]*)
                    # Malformed expiry from the API with a fingerprint that
                    # otherwise matches — cannot prove validity, so
                    # reconcile conservatively rather than trust it.
                    decision="near-expiry"
                    days_left="unknown"
                    ;;
                *)
                    days_left=$(((bind_notafter - now) / 86400))
                    if [ "$days_left" -lt "$ORIGIN_CERT_MIN_VALID_DAYS" ]; then
                        decision="near-expiry"
                    fi
                    ;;
            esac
        fi

        bind_fp_short="${bound_fp:-none}"
        if [ "${#bind_fp_short}" -gt 12 ]; then
            bind_fp_short="${bind_fp_short:0:12}…"
        fi
        local cert_id_short="${cert_id:-none}"
        if [ "${#cert_id_short}" -gt 12 ]; then
            cert_id_short="${cert_id_short:0:12}…"
        fi
        log "[INFO] binding check: domain=$domain${QINIU_BUCKET:+ bucket=$QINIU_BUCKET} decision=$decision bound_fp=$bind_fp_short local_fp=${leaf_fp:0:12}… bound_certID=$cert_id_short${bind_notafter:+ days_left=$days_left}"

        if [ "$decision" != "in-sync" ]; then
            stale_domains+=("$domain")
        fi
    done

    if [ "${#stale_domains[@]}" -eq 0 ]; then
        log "[INFO] all Qiniu bindings in sync with local_fp=${leaf_fp:0:12}…"
        return 0
    fi

    # Reconcile: ONE re-upload of the current cert, then re-bind only the
    # domains found stale (a healthy binding is left untouched — e.g. the
    # CDN domain when only the origin rots). deploy_to_qiniu /
    # bind_cert_to_domain are dry-run aware: in dry-run these calls print
    # the [DRY-RUN] plan and perform zero external writes.
    log "[INFO] reconciliation needed for: ${stale_domains[*]}"
    local certID
    if ! certID=$(deploy_to_qiniu "$WILDCARD_DOMAIN" "$WILDCARD_DIR"); then
        warn "binding reconciliation upload failed — stale bindings left as-is: ${stale_domains[*]}"
        return 0
    fi
    local d
    for d in "${stale_domains[@]}"; do
        if ! bind_err=$(bind_cert_to_domain "$d" "$certID" 2>&1); then
            warn "reconcile bind failed for $d: $bind_err"
        elif ! is_dry_run; then
            log "[OK] reconciled: bound to $d (certID=$certID)"
        fi
    done
    if is_dry_run; then
        log_dry "reconciliation plan complete — zero external writes performed"
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

deployed_fp="" && [ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")
if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
    # GH-231: the fingerprint only proves what THIS host deployed last — it
    # says nothing about what Qiniu is actually serving right now. Before
    # declaring the no-op, reconcile the live bindings (no-op when they are
    # already in sync; re-upload + re-bind when a domain still points at an
    # older or near-expiry certificate).
    CURRENT_STAGE="binding_reconcile"
    reconcile_qiniu_bindings
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
if [ -n "$ORIGIN_DOMAIN" ]; then
    if ! bind_err=$(bind_cert_to_domain "$ORIGIN_DOMAIN" "$certID" 2>&1); then
        warn "Origin bind failed (may need Qiniu Console config): $bind_err"
    else
        log "[INFO] bound to $ORIGIN_DOMAIN (origin)"
    fi
else
    log "[INFO] ORIGIN_DOMAIN not configured — skipping origin bind (GH-231)"
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
