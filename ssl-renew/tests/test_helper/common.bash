#!/usr/bin/env bash
#=============================================================================
# Shared bats test helpers. `load test_helper/common` from any .bats file.
# Everything here is offline-only: mock curl, a local Python HTTP(S) mock
# server, and self-signed test certificate generation. No test may reach a
# real DNSPod, Qiniu, Let's Encrypt, or any other production endpoint.
#=============================================================================

SSL_RENEW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

common_setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/ssl-renew-test.XXXXXX")"
	export TEST_TMPDIR
	export HOME="$TEST_TMPDIR/home"
	mkdir -p "$HOME"
	ORIGINAL_PATH="$PATH"
	MOCK_SERVER_PIDS=()

	# Tests only ever talk to 127.0.0.1. A dev/CI machine's system proxy
	# must never be in the loop — it would make "offline" tests depend on
	# an external service and can outright break localhost TLS handshakes.
	unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
	export NO_PROXY="*"
	export no_proxy="*"
}

common_teardown() {
	for pid in "${MOCK_SERVER_PIDS[@]:-}"; do
		[ -n "$pid" ] && kill "$pid" >/dev/null 2>&1
	done
	PATH="$ORIGINAL_PATH"
	[ -n "${TEST_TMPDIR:-}" ] && rm -rf "$TEST_TMPDIR"
}

# ── Mock curl (for Qiniu API tests) ──────────────────────────────────────
# use_mock_curl <mode> [status] [body] [logfile]
#   mode: success | http_error | timeout | connect_fail
use_mock_curl() {
	local mode="$1" status="${2:-200}" body="${3:-}" logfile="${4:-}"
	[ -z "$body" ] && body='{}'
	local bindir="$TEST_TMPDIR/mockbin"
	mkdir -p "$bindir"
	cp "$TEST_HELPER_DIR/mock_curl.sh" "$bindir/curl"
	chmod +x "$bindir/curl"
	export MOCK_CURL_MODE="$mode"
	export MOCK_CURL_STATUS="$status"
	export MOCK_CURL_BODY="$body"
	export MOCK_CURL_LOG="$logfile"
	PATH="$bindir:$ORIGINAL_PATH"
	export PATH
}

# ── Mock qiniu_helper.py (for fast, network-free lib/qiniu.sh tests) ────
# use_mock_qiniu_helper <mode> [status] [logfile]
#   mode: success | http_error | network_error
# Real network/crypto correctness is NOT this mock's job — see
# tests/test_qiniu_helper.py (golden parity) and the QINIU_API_HOST-based
# real-helper integration tests for that.
use_mock_qiniu_helper() {
    local mode="$1" status="${2:-500}" logfile="${3:-}"
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$TEST_HELPER_DIR/mock_qiniu_helper.py"
    export MOCK_QINIU_MODE="$mode"
    export MOCK_QINIU_STATUS="$status"
    export MOCK_QINIU_LOG="$logfile"
}

# ── Free TCP port on 127.0.0.1 ───────────────────────────────────────────
find_free_port() {
	python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"
}

# ── Self-signed test certificates (never committed; generated per-test) ──
# gen_cert <cn> <certfile> <keyfile> [not_before] [not_after]
# Dates in the form YYYYMMDDHHMMSSZ. Defaults to a cert valid for 1 year.
gen_cert() {
	local cn="$1" certfile="$2" keyfile="$3"
	local not_before="${4:-}" not_after="${5:-}"
	local -a extra=()
	if [ -n "$not_before" ] && [ -n "$not_after" ]; then
		extra=(-not_before "$not_before" -not_after "$not_after")
	else
		extra=(-days 365)
	fi
	openssl req -x509 -newkey rsa:2048 -nodes \
		-keyout "$keyfile" -out "$certfile" \
		-subj "/CN=$cn" -addext "subjectAltName=DNS:$cn" \
		"${extra[@]}" >/dev/null 2>&1
}

gen_expired_cert() {
	local cn="$1" certfile="$2" keyfile="$3"
	gen_cert "$cn" "$certfile" "$keyfile" 20190101000000Z 20200101000000Z
}

gen_soon_expiring_cert() {
	local cn="$1" certfile="$2" keyfile="$3" days="${4:-2}"
	local not_before not_after
	not_before=$(date -u -v-1d +%Y%m%d%H%M%SZ 2>/dev/null || date -u -d '1 day ago' +%Y%m%d%H%M%SZ)
	not_after=$(date -u -v+"${days}"d +%Y%m%d%H%M%SZ 2>/dev/null || date -u -d "+${days} days" +%Y%m%d%H%M%SZ)
	gen_cert "$cn" "$certfile" "$keyfile" "$not_before" "$not_after"
}

# ── Mock HTTP(S) server (webhook + HTTPS verification tests) ────────────
# start_mock_server <port> [--tls --cert C --key K] [--status N] [--delay S]
#                    [--log-file F]
# Returns once the server is confirmed listening. Registers PID for teardown.
start_mock_server() {
	local port="$1"
	shift
	local ready_file="$TEST_TMPDIR/ready-$port"
	rm -f "$ready_file"
	python3 "$TEST_HELPER_DIR/mock_server.py" --port "$port" --ready-file "$ready_file" "$@" &
	local pid=$!
	MOCK_SERVER_PIDS+=("$pid")

	local waited=0
	while [ ! -f "$ready_file" ]; do
		sleep 0.1
		waited=$((waited + 1))
		if [ "$waited" -gt 50 ]; then
			echo "mock server on port $port did not become ready" >&2
			return 1
		fi
		if ! kill -0 "$pid" 2>/dev/null; then
			echo "mock server on port $port exited early" >&2
			return 1
		fi
	done
	echo "$pid"
}

# ── openssl s_server (TLS-version-restricted mock, for the TLS1.2 negative
#    test). Python's ssl.SSLContext.minimum_version support is inconsistent
#    across platform builds; s_server's -tls1_3 flag is portable.
start_tls13_only_server() {
	local port="$1" cert="$2" key="$3"
	openssl s_server -accept "$port" -cert "$cert" -key "$key" -tls1_3 -quiet -naccept 100 \
		>/dev/null 2>&1 &
	local pid=$!
	MOCK_SERVER_PIDS+=("$pid")

	# fd 3 is reserved by bats internals in some versions; use fd 8 for
	# this readiness probe to avoid an unrelated "Bad file descriptor".
	local waited=0
	while ! (exec 8<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
		exec 8<&- 2>/dev/null || true
		sleep 0.1
		waited=$((waited + 1))
		if [ "$waited" -gt 50 ]; then
			echo "openssl s_server on port $port did not become ready" >&2
			return 1
		fi
		if ! kill -0 "$pid" 2>/dev/null; then
			echo "openssl s_server on port $port exited early" >&2
			return 1
		fi
	done
	exec 8<&- 2>/dev/null || true
	echo "$pid"
}
