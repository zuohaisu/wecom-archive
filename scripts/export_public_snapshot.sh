#!/usr/bin/env bash
#
# Export the public open-source snapshot from this private repository.
#
# The project ships under a two-repository model (decided 2026-07-27): the
# private repo keeps every internal artifact — Linear ticket files, agent
# automation, design sources, roadmaps — so development machines can sync
# them over git, and the public repo receives only a curated snapshot.
# This script IS that curation step. Until it existed the two-repo model
# was a decision with no mechanism behind it.
#
# Three stages, in order:
#   1. COPY      every tracked file matching scripts/public_allowlist.txt
#   2. SANITIZE  rewrite production hostnames / deploy paths / account
#                names to placeholders
#   3. GATE      scan the result for anything that must never ship; any
#                hit aborts with a non-zero exit and the snapshot is left
#                in place for inspection
#
# The gate runs AFTER sanitizing on purpose: it is there to catch what the
# sanitizer missed, not to duplicate it.
#
# Usage:
#   scripts/export_public_snapshot.sh [output-directory]
#   make public-snapshot
#
# This script never commits, pushes, or touches a remote. It writes a
# directory; publishing it is a separate, human decision.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOWLIST="${REPO_ROOT}/scripts/public_allowlist.txt"
OUT_DIR="${1:-${REPO_ROOT}/build/public-snapshot}"

cd "${REPO_ROOT}"

if [[ ! -f "${ALLOWLIST}" ]]; then
	echo "error: allowlist not found: ${ALLOWLIST}" >&2
	exit 1
fi

# Sanitizer rules, applied in order. Longest patterns first so that
# archive.crowntime.cn is rewritten before the bare crowntime.cn rule can
# turn it into archive.example.com's malformed cousin.
#
# Deliberately NOT rewritten:
#   RND-nnnn  1300+ occurrences carrying real design rationale in code
#             comments. Opaque to an outsider, but harmless — and stripping
#             them would gut the explanations they are attached to.
#   wecomarchive  a service account name a self-hoster will genuinely
#             create; replacing it with a placeholder makes the systemd
#             units worse as reference material.
#   crowntime.cn  the bare company domain stays. Crowntime's identity is
#             deliberately public — it is the named vendor of this product
#             and the host of the managed cloud offering, so rewriting it
#             to example.com would break the very links the README needs.
#             Only the operational subdomains below get scrubbed: those
#             point at a live production instance and are nobody's
#             business but the operator's.
SANITIZERS=(
	's|qwhhcd\.crowntime\.cn|archive.example.com|g'
	's|archive\.crowntime\.cn|archive.example.com|g'
	's|zuohaisu/wecom-archive-365|your-org/crowntime-wecom-archive|g'
	's|zuohaisu|your-org|g'
	's|/srv/apps/wecom-archive-365|/srv/apps/crowntime-wecom-archive|g'
)

# Patterns that must not survive into the snapshot. A hit here is a bug in
# the allowlist or the sanitizer, not something to wave through.
declare -a GATE_PATTERNS=(
	'qwhhcd\.crowntime\.cn'
	'archive\.crowntime\.cn'
	'/srv/apps/wecom-archive-365'
	'zuohaisu'
	'linear\.app'
	'粤ICP备'
	'网安备'
	'甄宇航'
)

echo "==> Exporting public snapshot"
echo "    source: ${REPO_ROOT}"
echo "    target: ${OUT_DIR}"

if [[ -n "$(git status --porcelain)" ]]; then
	echo "    note:   working tree has uncommitted changes; exporting from" \
		"the working tree, not HEAD"
fi

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

# ── Stage 1: copy ───────────────────────────────────────────────────
copied=0
while IFS= read -r entry; do
	entry="${entry%%#*}"
	entry="$(echo "${entry}" | xargs || true)"
	[[ -z "${entry}" ]] && continue

	matched=0
	while IFS= read -r -d '' file; do
		mkdir -p "${OUT_DIR}/$(dirname "${file}")"
		cp "${file}" "${OUT_DIR}/${file}"
		copied=$((copied + 1))
		matched=$((matched + 1))
	done < <(git ls-files -z -- "${entry}")

	if [[ ${matched} -eq 0 ]]; then
		echo "    warning: allowlist entry matched no tracked file: ${entry}" >&2
	fi
done <"${ALLOWLIST}"

echo "==> Copied ${copied} files"

# ── Stage 2: sanitize ───────────────────────────────────────────────
# perl rather than sed: GNU and BSD sed disagree on in-place editing, and
# this repo is developed on macOS and built on Linux.
sanitized=0
while IFS= read -r -d '' file; do
	# Skip binaries — rewriting bytes inside an image or a .ico corrupts it.
	if ! grep -Iq . "${file}" 2>/dev/null; then
		continue
	fi
	before="$(cksum <"${file}")"
	for rule in "${SANITIZERS[@]}"; do
		perl -pi -e "${rule}" "${file}"
	done
	after="$(cksum <"${file}")"
	[[ "${before}" != "${after}" ]] && sanitized=$((sanitized + 1))
done < <(find "${OUT_DIR}" -type f -print0)

echo "==> Sanitized ${sanitized} files"

# ── Stage 3: leak gate ──────────────────────────────────────────────
echo "==> Running leak gate"
violations=0
for pattern in "${GATE_PATTERNS[@]}"; do
	if hits="$(grep -rIn --binary-files=without-match -E "${pattern}" "${OUT_DIR}" 2>/dev/null)"; then
		echo ""
		echo "    LEAK GATE FAILED — pattern: ${pattern}"
		echo "${hits}" | sed "s|${OUT_DIR}/|      |" | head -20
		violations=$((violations + 1))
	fi
done

# Real private keys, as opposed to the many test fixtures that legitimately
# mention one. A literal "BEGIN PRIVATE KEY" match is useless here: the suite
# is full of assertions that a key does NOT appear, plus placeholder PEMs
# like "-----BEGIN PRIVATE KEY-----\ntest-rnd311\n-----END...". What
# distinguishes a real key is the payload — hundreds of characters of base64
# between the markers. 200 is comfortably above every fixture in the tree and
# far below the ~1600 of a 2048-bit RSA key.
# shellcheck disable=SC2016  # $ARGV is perl's, not the shell's — must not expand
if key_hits="$(find "${OUT_DIR}" -type f -print0 \
	| xargs -0 perl -0777 -ne \
		'print "$ARGV\n" if /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s]*[A-Za-z0-9+\/=\s]{200,}-----END/' \
		2>/dev/null | sort -u)" && [[ -n "${key_hits}" ]]; then
	echo ""
	echo "    LEAK GATE FAILED — file contains a real private key body:"
	echo "${key_hits//${OUT_DIR}\//      }"
	violations=$((violations + 1))
fi

# A real .env must never appear; .env.example is expected and fine.
if find "${OUT_DIR}" -name '.env' -o -name '*.pem' -o -name '*.key' | grep -q .; then
	echo ""
	echo "    LEAK GATE FAILED — secret-bearing file present:"
	find "${OUT_DIR}" \( -name '.env' -o -name '*.pem' -o -name '*.key' \) \
		| sed "s|${OUT_DIR}/|      |"
	violations=$((violations + 1))
fi

echo ""
if [[ ${violations} -gt 0 ]]; then
	echo "==> FAILED: ${violations} leak-gate violation(s)."
	echo "    Snapshot left at ${OUT_DIR} for inspection. Do NOT publish it."
	echo "    Fix scripts/public_allowlist.txt or the SANITIZERS list, then rerun."
	exit 1
fi

echo "==> Leak gate passed"
echo "==> Snapshot ready: ${OUT_DIR} (${copied} files)"
echo ""
echo "    Review it by hand before publishing. This script does not push."
