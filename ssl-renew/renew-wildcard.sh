#!/usr/bin/env bash
#=============================================================================
# renew-wildcard.sh — Production wildcard *.crowntime.cn certificate renewal
# (GH-104 Follow-up B: captured from the already-proven production
# implementation introduced by the 2026-08-03 RND-261 domain-cutover — see
# docs/ops/rnd-261-domain-cutover-runbook.md — which had never been
# committed to this repository until now).
#
# Unlike renew.sh (a per-domain Qiniu Kodo CDN custom-domain cert bind,
# templated via qiniu-ssl-renew@<domain>), this script renews ONE wildcard
# certificate covering *.crowntime.cn and the apex, then deploys it to
# TWO destinations that consume the SAME certificate material:
#
#   1. Qiniu CDN/origin domain bindings (media.crowntime.cn and
#      media-origin.crowntime.cn) via the same qiniu_helper.py used by
#      renew.sh — no second signing/upload implementation.
#   2. The nginx-fronted vhosts on this host (crowntime.cn, www.crowntime.cn,
#      qwhhcd.crowntime.cn, staging-archive.crowntime.cn — see
#      docs/operations/wildcard-ssl-renewal.md for the authoritative,
#      Ops-confirmed list; archive.crowntime.cn currently listens on :80
#      only and is NOT served by this certificate, unchanged by this PR).
#
# Pipeline (preserve exactly — see GH-104 Follow-up B task for the full
# preserved-behavior contract):
#
#   acme.sh --renew (DNS-01, wildcard SAN)
#     -> exit 0: renewed;  exit 2: not due yet (successful no-op, nothing
#        further to do this run);  anything else: hard failure
#   -> local fullchain.cer sha256 fingerprint
#     -> matches the last recorded deployed fingerprint: already deployed,
#        exit success without re-binding/re-copying/reloading anything
#   -> Qiniu upload (one certID for the wildcard cert)
#     -> bind to CDN_DOMAIN (media.crowntime.cn): failure is a HARD FAILURE
#     -> bind to ORIGIN_DOMAIN (media-origin.crowntime.cn): failure is a
#        WARN ONLY (this is production's real, deliberate asymmetry — the
#        CDN domain is what end users hit; the origin domain binding is a
#        secondary path this script does not gate success on)
#   -> copy fullchain/privkey into the nginx-facing cert directory, chmod
#   -> sudo systemctl reload nginx: failure is a WARN ONLY (the certificate
#      is already deployed to disk; a stuck nginx process keeps serving the
#      previous cert until the next successful reload, which is safer than
#      treating a reload hiccup as a renewal failure)
#
# Deliberately NOT done here (see GH-104 Follow-up B — "capture, not
# rewrite"):
#   - no flock/PID locking on $LOCK_FILE. The variable exists (parity with
#     renew.sh's own $LOCK_FILE) but is never acquired — this mirrors
#     production's actual, currently-unused LOCK_FILE exactly. 41/41
#     observed production runs have never overlapped (a single daily timer
#     with RandomizedDelaySec, oneshot Type, systemd's own default
#     start-limit behavior). Locking is a legitimate future hardening
#     candidate — see docs/operations/wildcard-ssl-renewal.md — not this
#     ticket's scope.
#   - no HTTPS/chain re-verification step (renew.sh's verify_https.sh
#     integration is NOT part of this pipeline; only the four preserved
#     stages above are).
#   - no ACME provider, DNS provider, certificate-directory-layout, or
#     Qiniu-binding-architecture change of any kind.
#
# Requires (same runtime dependencies as renew.sh):
#   - acme.sh (installed in $HOME/.acme.sh/)
#   - openssl, sha256sum, sudo (for the exact, narrowly-scoped
#     `systemctl reload nginx` grant — see docs/operations/
#     wildcard-ssl-renewal.md's Sudoers Dependency section)
#   - python3 + the official `qiniu` SDK (ssl-renew/requirements.txt),
#     via lib/qiniu.sh -> qiniu_helper.py — identical to renew.sh
#   - Environment: QINIU_ACCESS_KEY / QINIU_SECRET_KEY
#   - acme.sh account.conf must contain DP_Id / DP_Key (DNSPod API token)
#
# Exit codes:
#   0  success (including the "not due yet" and "already deployed" no-ops)
#   1  generic runtime failure (acme/upload/CDN-bind/nginx-copy error)
#   2  usage / configuration error (missing required env)
#=============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/qiniu.sh
source "$SCRIPT_DIR/lib/qiniu.sh"

if ! load_config; then
	exit "$EXIT_USAGE_ERROR"
fi

# ── Domain scope (do not change — see GH-104 Follow-up B task §2) ─────────
# DOMAIN is the standard per-instance EnvironmentFile variable name shared
# with every other domain.env in this subsystem (see
# examples/domain.env.example); this script reads it into a wildcard-
# specific name because it also has CDN_DOMAIN/ORIGIN_DOMAIN as siblings.
WILDCARD_DOMAIN="${DOMAIN:-crowntime.cn}"
CDN_DOMAIN="${CDN_DOMAIN:-media.crowntime.cn}"
ORIGIN_DOMAIN="${ORIGIN_DOMAIN:-media-origin.crowntime.cn}"

if ! validate_domain "$WILDCARD_DOMAIN"; then
	exit "$EXIT_USAGE_ERROR"
fi
if ! validate_domain "$CDN_DOMAIN"; then
	exit "$EXIT_USAGE_ERROR"
fi
if ! validate_domain "$ORIGIN_DOMAIN"; then
	exit "$EXIT_USAGE_ERROR"
fi

if ! require_env QINIU_ACCESS_KEY QINIU_SECRET_KEY; then
	exit "$EXIT_USAGE_ERROR"
fi

# ── Setup ────────────────────────────────────────────────────────────────
ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
CERT_DIR="${CERT_DIR:-$HOME/.acme.sh/$WILDCARD_DOMAIN}"
FULLCHAIN_FILE="$CERT_DIR/fullchain.cer"
PRIVKEY_FILE="$CERT_DIR/$WILDCARD_DOMAIN.key"
FP_FILE="$CERT_DIR/.deployed_fp"
# Present for parity with renew.sh's own $LOCK_FILE — intentionally NOT
# flock'd/acquired. See the module docstring above; do not "fix" this in
# this ticket.
# shellcheck disable=SC2034
LOCK_FILE="$CERT_DIR/.renew.lock"

# nginx-facing deployment destination. Production's actual value comes from
# the EnvironmentFile below; this default matches the directory the
# RND-261 domain-cutover runbook put the certificate in
# (docs/ops/rnd-261-domain-cutover-runbook.md), writable by wecomarchive
# and readable by the nginx master (root).
NGINX_CERT_DIR="${NGINX_CERT_DIR:-/srv/apps/wecom-archive-365/shared/certs/wildcard.$WILDCARD_DOMAIN}"

export LOG_TAG="[$WILDCARD_DOMAIN]"

CURRENT_STAGE="init"
warn() {
	log "[WARN]  $*"
	CURRENT_STAGE="$CURRENT_STAGE" "$SCRIPT_DIR/notify.sh" WARN "$WILDCARD_DOMAIN" "$*" || true
}
die() {
	log "[ERROR] $*"
	CURRENT_STAGE="$CURRENT_STAGE" "$SCRIPT_DIR/notify.sh" ERROR "$WILDCARD_DOMAIN" "$*" || true
	exit "$EXIT_GENERIC_FAILURE"
}

mkdir -p "$CERT_DIR" 2>/dev/null || true

# ═════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════

log "[INFO] ====== Wildcard SSL renewal started ($WILDCARD_DOMAIN, *.$WILDCARD_DOMAIN) ======"

# ── Step 1: acme.sh --renew (DNS-01, wildcard SAN) ──────────────────────

CURRENT_STAGE="acme_renew"
log "[INFO] Running $ACME_SH --renew --dns $DNS_PROVIDER -d $WILDCARD_DOMAIN -d *.$WILDCARD_DOMAIN"
set +e
"$ACME_SH" --renew --dns "$DNS_PROVIDER" -d "$WILDCARD_DOMAIN" -d "*.$WILDCARD_DOMAIN" 2>&1 | filter_secrets
acme_exit=${PIPESTATUS[0]}
set -e

if [ "$acme_exit" -eq 2 ]; then
	log "[OK]  acme.sh reported certificate not due yet (exit=2) — successful no-op, nothing to deploy"
	exit "$EXIT_OK"
elif [ "$acme_exit" -ne 0 ]; then
	die "acme.sh --renew failed (exit=$acme_exit)"
fi
log "[INFO] acme.sh --renew completed"

# ── Step 2: local fingerprint / already-deployed short-circuit ─────────

CURRENT_STAGE="local_fingerprint"
if [ ! -f "$FULLCHAIN_FILE" ]; then
	die "fullchain.cer not found: $FULLCHAIN_FILE"
fi

local_fp=$(sha256sum "$FULLCHAIN_FILE" | awk '{print $1}')
log "[INFO] local_fp=$local_fp"

deployed_fp=""
[ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")

if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
	log "[OK]  cert already deployed (fp=$local_fp) — nothing to do"
	exit "$EXIT_OK"
fi

log "[INFO] local_fp != deployed_fp (local=$local_fp, deployed=${deployed_fp:-none}) — proceeding to deploy ..."

# ── Step 3: Qiniu upload + CDN bind (hard fail) + origin bind (warn only) ──

CURRENT_STAGE="qiniu_upload"
upload_output=$(deploy_to_qiniu "$WILDCARD_DOMAIN" "$CERT_DIR" 2>&1)
upload_status=$?
if [ "$upload_status" -ne 0 ]; then
	die "$upload_output"
fi
certID="$upload_output"
log "[INFO] wildcard certificate uploaded to Qiniu — certID=$certID"

CURRENT_STAGE="qiniu_bind_cdn"
if ! bind_err=$(bind_cert_to_domain "$CDN_DOMAIN" "$certID" 2>&1); then
	die "CDN domain bind failed for $CDN_DOMAIN: $bind_err"
fi
log "[INFO] certID=$certID bound to CDN domain $CDN_DOMAIN"

CURRENT_STAGE="qiniu_bind_origin"
if ! bind_err=$(bind_cert_to_domain "$ORIGIN_DOMAIN" "$certID" 2>&1); then
	# Deliberate asymmetry vs. the CDN bind above — see module docstring.
	warn "origin domain bind failed for $ORIGIN_DOMAIN (non-fatal): $bind_err"
else
	log "[INFO] certID=$certID bound to origin domain $ORIGIN_DOMAIN"
fi

# ── Step 4: deploy to the nginx-facing cert directory ───────────────────

CURRENT_STAGE="nginx_deploy"
if [ ! -f "$PRIVKEY_FILE" ]; then
	die "private key not found: $PRIVKEY_FILE"
fi
if ! mkdir -p "$NGINX_CERT_DIR"; then
	die "could not create nginx cert directory: $NGINX_CERT_DIR"
fi
if ! cp "$FULLCHAIN_FILE" "$NGINX_CERT_DIR/fullchain.pem"; then
	die "failed to copy fullchain.cer to $NGINX_CERT_DIR/fullchain.pem"
fi
if ! cp "$PRIVKEY_FILE" "$NGINX_CERT_DIR/privkey.pem"; then
	die "failed to copy private key to $NGINX_CERT_DIR/privkey.pem"
fi
chmod 640 "$NGINX_CERT_DIR/fullchain.pem" "$NGINX_CERT_DIR/privkey.pem" 2>/dev/null ||
	warn "chmod 640 on deployed nginx cert files failed — check ownership of $NGINX_CERT_DIR"
log "[INFO] certificate deployed to $NGINX_CERT_DIR"

# Record the fingerprint now that both Qiniu (CDN, hard-required) and the
# nginx-facing copy have succeeded — an origin-bind or nginx-reload warning
# does not block this; the certificate material itself is fully deployed.
echo "$local_fp" >"$FP_FILE"

# ── Step 5: nginx reload (warn only — do not fail the run) ──────────────

CURRENT_STAGE="nginx_reload"
set +e
sudo systemctl reload nginx 2>&1 | filter_secrets
reload_exit=${PIPESTATUS[0]}
set -e

if [ "$reload_exit" -eq 0 ]; then
	log "[INFO] nginx reloaded"
else
	warn "nginx reload failed (exit=$reload_exit) — certificate is already deployed at $NGINX_CERT_DIR; nginx keeps serving the previous certificate until the next successful reload"
fi

# ── Done ─────────────────────────────────────────────────────────────────

CURRENT_STAGE="done"
log "[OK]  wildcard deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
