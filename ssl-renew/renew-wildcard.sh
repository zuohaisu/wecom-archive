#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/qiniu.sh
source "$SCRIPT_DIR/lib/qiniu.sh"

WILDCARD_DOMAIN="crowntime.cn"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"

ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
CERT_DIR="${CERT_DIR:-$HOME/.acme.sh/$WILDCARD_DOMAIN}"
FULLCHAIN_FILE="$CERT_DIR/fullchain.cer"
PRIVKEY_FILE="$CERT_DIR/$WILDCARD_DOMAIN.key"
FP_FILE="$CERT_DIR/.deployed_fp"
TLS_OK_FILE="$CERT_DIR/.tls_verified"
MISMATCH_FILE="$CERT_DIR/.tls_mismatch_days"
# shellcheck disable=SC2034
LOCK_FILE="$CERT_DIR/.renew.lock"
NGINX_CERT_DIR="${NGINX_CERT_DIR:-/srv/apps/wecom-archive-365/shared/certs/wildcard.$WILDCARD_DOMAIN}"

export LOG_TAG="[$WILDCARD_DOMAIN]"

log "[INFO] ====== Wildcard SSL renewal started ($WILDCARD_DOMAIN) ======"

set +e
"$ACME_SH" --renew --dns "$DNS_PROVIDER" -d "$WILDCARD_DOMAIN" -d "*.$WILDCARD_DOMAIN" 2>&1 | filter_secrets
acme_exit=${PIPESTATUS[0]}
set -e

if [ "$acme_exit" -eq 0 ]; then
	log "[INFO] acme.sh --renew completed"
elif [ "$acme_exit" -eq 2 ]; then
	log "[INFO] acme.sh --renew: certificate not due yet (exit=2)"
else
	die "acme.sh --renew failed (exit=$acme_exit)"
fi

if [ ! -f "$FULLCHAIN_FILE" ]; then
	die "fullchain.cer not found: $FULLCHAIN_FILE"
fi

local_fp=$(sha256sum "$FULLCHAIN_FILE" | awk '{print $1}')
log "[INFO] local_fp=$local_fp"

deployed_fp=""
[ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")

if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then
	log "[OK] cert already deployed (fp=$local_fp) — nothing to do"
	exit "$EXIT_OK"
fi

log "[INFO] local_fp != deployed_fp (local=$local_fp, deployed=${deployed_fp:-none}) — proceeding to deploy ..."

upload_output=$(deploy_to_qiniu "$WILDCARD_DOMAIN" "$CERT_DIR" 2>&1)
upload_status=$?
if [ "$upload_status" -ne 0 ]; then
	die "$upload_output"
fi
certID="$upload_output"
log "[INFO] certID=$certID"

if ! bind_err=$(bind_cert_to_domain "$CDN_DOMAIN" "$certID" 2>&1); then
	die "CDN domain bind failed for $CDN_DOMAIN: $bind_err"
fi
log "[INFO] bound to CDN domain $CDN_DOMAIN"

if ! bind_err=$(bind_cert_to_domain "$ORIGIN_DOMAIN" "$certID" 2>&1); then
	warn "origin domain bind failed for $ORIGIN_DOMAIN: $bind_err"
else
	log "[INFO] bound to origin domain $ORIGIN_DOMAIN"
fi

if [ -d "$NGINX_CERT_DIR" ] && [ -f "$FULLCHAIN_FILE" ] && [ -f "$PRIVKEY_FILE" ]; then
	cp -f "$FULLCHAIN_FILE" "$NGINX_CERT_DIR/fullchain.pem"
	cp -f "$PRIVKEY_FILE" "$NGINX_CERT_DIR/privkey.pem"
	chmod 640 "$NGINX_CERT_DIR/fullchain.pem" "$NGINX_CERT_DIR/privkey.pem"
	sudo -n /usr/bin/systemctl reload nginx
else
	warn "nginx cert directory or source files missing — skipped nginx deployment"
fi

echo "$local_fp" >"$FP_FILE"
rm -f "$TLS_OK_FILE"
rm -f "$MISMATCH_FILE"

log "[OK] wildcard deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
