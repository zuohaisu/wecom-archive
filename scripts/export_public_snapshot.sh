#!/usr/bin/env bash
#
# Build a local public-review snapshot from this repository.
#
# The snapshot is a leak-review and validation artifact used by
# `make public-verify`; it is not a publishing mechanism or a source/license
# boundary. First-party application code, including cloud code, remains in the
# unified AGPL-3.0 repository. The snapshot only includes the paths named in
# scripts/public_allowlist.txt for this bounded review run.
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

# Sanitizer rules are targeted rewrites, applied in order. The known
# environment-specific admin hostname below is narrowly sanitized in this
# review snapshot. This is not a confidentiality claim about crowntime.cn or
# a suffix rule: public product endpoints and other subdomains are not leaks
# merely because they share the domain suffix.
#
# Deliberately NOT rewritten:
#   RND-nnnn  1300+ occurrences carrying real design rationale in code
#             comments. Opaque to an outsider, but harmless — and stripping
#             them would gut the explanations they are attached to.
#   wecomarchive  a service account name a self-hoster will genuinely
#             create; replacing it with a placeholder makes the systemd
#             units worse as reference material.
#   crowntime.cn  the company identity, product endpoints, and domain are
#             public; rewriting them wholesale would remove legitimate
#             attribution and business references. Only the exact admin host
#             below is rewritten based on its concrete environment-specific
#             context. Never infer a leak from the suffix alone.
SANITIZERS=(
	's|qwhhcd\.crowntime\.cn|archive.example.com|g'
	's|zuohaisu/wecom-archive-365|your-org/wecom-archive|g'
	's|zuohaisu/wecom-archive|your-org/wecom-archive|g'
	's|zuohaisu|your-org|g'
	's|/srv/apps/wecom-archive-365|/srv/apps/wecom-archive|g'
	's|wecom-archive-365|wecom-archive|g'
)

# Patterns that must not survive into the snapshot. A hit here is a bug in
# the allowlist or the sanitizer, not something to wave through. Public
# company-registration labels are deliberately not blanket-blocked: the
# approved marketing pages publish the company's ICP/public-security records.
declare -a GATE_PATTERNS=(
	'qwhhcd\.crowntime\.cn'
	'/srv/apps/wecom-archive-365'
	'wecom-archive-365'
	'zuohaisu'
	'linear\.app'
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
normalized_paths=0
while IFS= read -r entry; do
	entry="${entry%%#*}"
	entry="$(echo "${entry}" | xargs || true)"
	[[ -z "${entry}" ]] && continue

	matched=0
	while IFS= read -r -d '' file; do
		snapshot_path="${file//wecom-archive-365/wecom-archive}"
		destination="${OUT_DIR}/${snapshot_path}"
		if [[ -e "${destination}" ]]; then
			echo "error: snapshot path collision after repository-name normalization: ${snapshot_path}" >&2
			exit 1
		fi
		mkdir -p "$(dirname "${destination}")"
		cp "${file}" "${destination}"
		copied=$((copied + 1))
		if [[ "${snapshot_path}" != "${file}" ]]; then
			normalized_paths=$((normalized_paths + 1))
		fi
		matched=$((matched + 1))
	done < <(git ls-files -z -- "${entry}")

	if [[ ${matched} -eq 0 ]]; then
		echo "    warning: allowlist entry matched no tracked file: ${entry}" >&2
	fi
done <"${ALLOWLIST}"

echo "==> Copied ${copied} files"
if [[ ${normalized_paths} -gt 0 ]]; then
	echo "==> Normalized ${normalized_paths} legacy repository-name path(s)"
fi

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
if old_paths="$(find "${OUT_DIR}" -name '*wecom-archive-365*' -print)" && [[ -n "${old_paths}" ]]; then
	echo ""
	echo "    LEAK GATE FAILED — legacy repository name in snapshot path:"
	echo "${old_paths}" | sed "s|${OUT_DIR}/|      |" | head -20
	violations=$((violations + 1))
fi
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
