#!/usr/bin/env bash
#=============================================================================
# Mock `gpg` used by backup_once.bats. Reads the passphrase from fd 0 (as
# the real invocation does via --passphrase-fd 0) and "encrypts" by
# prefixing a marker line — enough to assert the passphrase was actually
# piped in (never present in argv) and that -o/<infile> wiring is correct,
# without a real GPG keyring.
#
# Controlled via:
#   MOCK_GPG_MODE   ok | fail (default ok)
#   MOCK_GPG_LOG    if set, the full argv is appended here (never the
#                   passphrase itself, which only ever arrives on stdin)
#=============================================================================
if [ -n "${MOCK_GPG_LOG:-}" ]; then
	printf 'gpg %s\n' "$*" >>"$MOCK_GPG_LOG"
fi

passphrase="$(cat)"

if [ "${MOCK_GPG_MODE:-ok}" = "fail" ]; then
	echo "gpg: error: encryption failed" >&2
	exit 1
fi

outfile=""
prev=""
for a in "$@"; do
	if [ "$prev" = "-o" ]; then outfile="$a"; fi
	prev="$a"
done
infile="${*: -1}"

if [ -z "$passphrase" ]; then
	echo "gpg: error: no passphrase received on stdin" >&2
	exit 1
fi

{
	echo "MOCKGPG-ENCRYPTED"
	cat "$infile"
} >"$outfile"
exit 0
