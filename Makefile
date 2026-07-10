SHELL := /bin/bash

SSL_DIR := ssl-renew
SSL_SCRIPTS := $(SSL_DIR)/renew.sh $(SSL_DIR)/notify.sh $(SSL_DIR)/verify_https.sh $(SSL_DIR)/install.sh
SSL_LIB := $(SSL_DIR)/lib/common.sh $(SSL_DIR)/lib/qiniu.sh
SSL_TEST_HELPERS := $(SSL_DIR)/tests/systemd_static_check.sh $(SSL_DIR)/tests/test_helper/mock_curl.sh
SSL_PYTHON_FILES := $(SSL_DIR)/qiniu_helper.py $(SSL_DIR)/tests/test_qiniu_helper.py \
	$(SSL_DIR)/tests/test_helper/mock_server.py $(SSL_DIR)/tests/test_helper/mock_qiniu_helper.py
PYTHON ?= python3

# All targets below are offline-only by design: they never contact DNSPod,
# Let's Encrypt, or the real Qiniu API, and never touch production hosts.

.PHONY: ssl-lint ssl-test ssl-dry-run ssl-verify-systemd

## Shellcheck + shfmt over every ssl-renew script, plus a Python syntax
## check over qiniu_helper.py and its tests. Fails non-zero on any finding.
ssl-lint:
	@command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck not found — install via 'brew install shellcheck' or apt/yum equivalent" >&2; exit 1; }
	@command -v shfmt >/dev/null 2>&1 || { echo "shfmt not found — install via 'brew install shfmt' or apt/yum equivalent" >&2; exit 1; }
	shellcheck $(SSL_SCRIPTS) $(SSL_LIB) $(SSL_TEST_HELPERS)
	shfmt -d $(SSL_SCRIPTS) $(SSL_LIB) $(SSL_TEST_HELPERS)
	$(PYTHON) -m py_compile $(SSL_PYTHON_FILES)
	@echo "ssl-lint: OK"

## Full bats suite (unit + mock integration) plus the Python pytest suite
## (golden signing parity against the official Qiniu SDK, mocked-transport
## and real-local-server response handling, and runtime secret-safety
## checks). Fails non-zero on any test failure.
ssl-test:
	@command -v bats >/dev/null 2>&1 || { echo "bats not found — install via 'brew install bats-core' or see ssl-renew/Dockerfile" >&2; exit 1; }
	@$(PYTHON) -c "import qiniu" >/dev/null 2>&1 || { echo "python 'qiniu' package not found — run: pip install -r $(SSL_DIR)/requirements-dev.txt" >&2; exit 1; }
	@$(PYTHON) -c "import pytest" >/dev/null 2>&1 || { echo "python 'pytest' package not found — run: pip install -r $(SSL_DIR)/requirements-dev.txt" >&2; exit 1; }
	bats $(SSL_DIR)/tests/*.bats
	$(PYTHON) -m pytest $(SSL_DIR)/tests/test_qiniu_helper.py -v
	@echo "ssl-test: OK"

## Runs renew.sh in dry-run mode against a throwaway scratch HOME and a
## documentation-reserved example domain — prints the plan, touches
## nothing real, makes no network calls, and never uses production
## credentials or the real crowntime.cn domain.
ssl-dry-run:
	@tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	HOME="$$tmp" DRY_RUN=1 $(CURDIR)/$(SSL_DIR)/renew.sh example.com; \
	status=$$?; \
	echo "ssl-dry-run: exit=$$status"; \
	exit $$status

## Validates the shipped systemd unit files. Prefers the authoritative
## `systemd-analyze verify` (native on Linux, or via the Docker image on
## any host with Docker); falls back to a structural static check ONLY
## (clearly labeled as non-authoritative) when neither is available —
## this never silently reports success as if the real tool ran.
ssl-verify-systemd:
	@units="deploy/systemd/qiniu-ssl-renew@.service deploy/systemd/qiniu-ssl-renew@.timer"; \
	if command -v systemd-analyze >/dev/null 2>&1; then \
		echo "Using native systemd-analyze verify"; \
		systemd-analyze verify $$units; \
	elif command -v docker >/dev/null 2>&1; then \
		echo "Using systemd-analyze verify inside ssl-renew/Dockerfile"; \
		docker build -q -t ssl-renew-verify -f $(SSL_DIR)/Dockerfile . && \
		docker run --rm -v "$(CURDIR)":/workspace -w /workspace ssl-renew-verify \
			systemd-analyze verify $$units; \
	else \
		echo "WARNING: neither systemd-analyze nor docker is available." >&2; \
		echo "Falling back to a STRUCTURAL STATIC CHECK ONLY — this is NOT" >&2; \
		echo "a substitute for systemd-analyze verify. Install Docker (see" >&2; \
		echo "$(SSL_DIR)/Dockerfile) or run this target on Linux before deploying." >&2; \
		$(SSL_DIR)/tests/systemd_static_check.sh $$units; \
	fi
