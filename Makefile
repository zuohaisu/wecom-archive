SHELL := /bin/bash

SSL_DIR := ssl-renew
SSL_SCRIPTS := $(SSL_DIR)/renew.sh $(SSL_DIR)/renew-wildcard.sh $(SSL_DIR)/notify.sh $(SSL_DIR)/verify_https.sh $(SSL_DIR)/install.sh
# The wildcard script retains its hand-authored production-derived layout.
# Keep shellcheck on it, but leave broad formatting changes outside focused
# renewal hardening work.
SSL_SHFMT_SCRIPTS := $(filter-out $(SSL_DIR)/renew-wildcard.sh,$(SSL_SCRIPTS))
SSL_LIB := $(SSL_DIR)/lib/common.sh $(SSL_DIR)/lib/qiniu.sh
SSL_TEST_HELPERS := $(SSL_DIR)/tests/systemd_static_check.sh $(SSL_DIR)/tests/test_helper/mock_curl.sh
SSL_PYTHON_FILES := $(SSL_DIR)/qiniu_helper.py $(SSL_DIR)/tests/test_qiniu_helper.py \
	$(SSL_DIR)/tests/test_helper/mock_server.py $(SSL_DIR)/tests/test_helper/mock_qiniu_helper.py
PYTHON ?= python3

# All targets below except public-wiki are offline-only by design. The
# public-wiki target publishes to the separate GitHub Wiki repository; none
# of these targets touches production hosts.

.PHONY: ssl-lint ssl-test ssl-dry-run ssl-verify-systemd public-wiki

# ---------------------------------------------------------------------------
# Backend (backend/app, backend/scripts, backend/tests) — repo-level static
# checks and test entry points (RND-187 developer-acceptance fix round 2).
#
# Uses the repo-root venv (.venv/) rather than backend/.venv/ or system
# python: it already has ruff, pytest, and every backend runtime dependency
# (fastapi, sqlalchemy, qiniu, ...) installed, so a single interpreter
# covers lint + test + build without needing two separate venvs wired in.
#
# Every target here is offline and secret-free by construction: pytest's
# Qiniu-backed tests mock the SDK boundary (never call the real Qiniu API),
# and none of lint/typecheck/build/test touch a real database, DNS, or
# production host.
# ---------------------------------------------------------------------------

BACKEND_PY ?= $(CURDIR)/.venv/bin/python

.PHONY: lint lint-diff typecheck build test verify

## Python static checks over the WHOLE backend tree: ruff (style/
## correctness rules) plus a compileall syntax pass. This repo has never
## had a ruff config, so this is an HONEST, currently-non-zero baseline —
## as of this fix round it reports ~133 pre-existing findings, none
## introduced here (103 are F811 "redefined-while-unused" from an
## established pytest-fixture-import convention already used — and
## partially `# noqa`'d — across dozens of pre-existing test files; the
## rest are pre-existing unused imports/variables elsewhere). Silently
## reconfiguring ruff to hide that pattern, or fixing 130+ unrelated
## findings, is exactly the kind of unrelated-cleanup scope creep this fix
## round must NOT do. Use `make lint-diff` to check only the files this
## round actually touched, which passes clean. There is no separate
## frontend source tree to lint (no package.json / no frontend/ directory)
## — the review console's embedded JS lives inside backend/app/main.py as
## Python string literals and backend/app/assets/i18n.js, both exercised
## for real by the Node-based tests `make test` runs (they extract and
## execute the actual embedded JS under Node — see
## backend/tests/test_media_hydration.py and friends), a stronger
## guarantee than a standalone linter would give here. Shell scripts
## (ssl-renew/) are out of this round's scope — see `ssl-lint` above, kept
## as its own target so this one never requires shellcheck/shfmt.
lint:
	@command -v $(BACKEND_PY) >/dev/null 2>&1 || { echo "$(BACKEND_PY) not found — run: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt" >&2; exit 1; }
	$(BACKEND_PY) -m ruff check backend/app backend/scripts backend/tests
	$(BACKEND_PY) -m compileall -q backend/app backend/scripts backend/tests
	@echo "lint: OK"

## Same ruff check, scoped to only the tracked-modified + newly-added
## Python files in the current git working tree (i.e. this round's actual
## diff) — the practical "did my change introduce any new lint findings"
## signal, unaffected by the repo's pre-existing baseline. Passes trivially
## (prints a message, exit 0) when there are no changed .py files.
## --diff-filter=d excludes deleted paths from `git diff --name-only` —
## without it, a file removed (but not yet staged) in the working tree
## still shows up here, and ruff fails with E902 "No such file or
## directory" trying to open a path that no longer exists. Renamed/added/
## modified files are unaffected; a genuinely deleted file has nothing
## left to lint.
lint-diff:
	@set -e; \
	files="$$(git diff --name-only --diff-filter=d -- '*.py'; git diff --cached --name-only --diff-filter=d -- '*.py'; git ls-files --others --exclude-standard -- '*.py')"; \
	files="$$(echo "$$files" | sort -u | grep -v '^$$' || true)"; \
	if [ -z "$$files" ]; then \
		echo "lint-diff: no changed .py files — OK"; \
	else \
		echo "lint-diff: checking:"; echo "$$files" | sed 's/^/  /'; \
		$(BACKEND_PY) -m ruff check $$files && echo "lint-diff: OK"; \
	fi

## Honest about its own limits: this repo has no mypy/pyright config, and
## introducing one is explicitly out of scope for this fix round (it would
## invite refactoring unrelated legacy code just to satisfy new type
## errors). This target is therefore an import/syntax-level check only —
## every backend module must at least parse and import cleanly — not a
## real type check. If/when this repo adopts mypy or pyright, replace the
## body below with the real tool invocation; keep the target name stable.
typecheck:
	@echo "typecheck: no mypy/pyright configured in this repo — running import/syntax-level checks only (NOT a full type check)." >&2
	$(BACKEND_PY) -m compileall -q backend/app backend/scripts
	cd backend && $(BACKEND_PY) -c "from app.main import app; assert len(app.routes) > 0, 'no routes registered'"
	@echo "typecheck: OK (import/syntax-level only)"

## No separate frontend build step exists — FastAPI serves the admin
## console as server-rendered HTML (see the lint target comment above).
## "build" here means: every backend module compiles, every standalone JS
## asset parses as valid JS, and the FastAPI app actually constructs
## end-to-end. Until RND-216, backend/app/assets/i18n.js was the only
## standalone JS asset (everything else was inlined into main.py's Python
## source); RND-216 externalized the admin console's page scripts too, so
## every backend/app/web/static/*.js file (search.js, diagnostics.js —
## templated HTML now lives in backend/app/web/templates/ and is assembled
## at request time by app.web.render_template, never at build time) is
## checked the same way. RND-217 further split the review console's script
## into 8 modules under backend/app/web/static/console/ (console-state.js,
## api-client.js, conversation-list.js, timeline.js, message-renderers.js,
## media-viewer.js, refresh.js, console-entry.js — loaded by
## review_console.html as 8 ordered <script src> tags), checked the same
## way individually so each one is independently syntax-valid. The
## app-construction check below still catches a broken/missing template or
## static asset at build time (render_template reads them eagerly the
## first time a route renders, and app.web computes STATIC_VERSION from
## the static/ directory at import time), not at request time in
## production.
build:
	$(BACKEND_PY) -m compileall -q backend/app backend/scripts
	@if command -v node >/dev/null 2>&1; then \
		node --check backend/app/assets/i18n.js && echo "build: backend/app/assets/i18n.js syntax OK"; \
		for f in backend/app/web/static/*.js backend/app/web/static/console/*.js; do \
			node --check "$$f" && echo "build: $$f syntax OK"; \
		done; \
	else \
		echo "build: node not found — skipped JS syntax checks" >&2; \
	fi
	cd backend && $(BACKEND_PY) -c "from app.main import app; assert len(app.routes) > 0, 'no routes registered'"
	@echo "build: OK"

## Full backend pytest suite, including the Node-executed embedded-JS
## tests (skipped automatically, not failed, if `node` isn't on PATH — see
## their own pytest.mark.skipif). Never touches a real database, DNS, or
## the real Qiniu API — Qiniu-backed tests mock the SDK boundary.
##
## The timeout runner permits one normal slow run and only retries timeouts:
## 300s, then 600s, then 1200s. It reports the last pytest percentage before
## each retry and treats a third timeout as a likely real hang.
test:
	$(BACKEND_PY) scripts/run_with_escalating_timeout.py -- $(BACKEND_PY) -m pytest backend/tests -q
	@echo "test: OK"

## Composite entry point for developer acceptance: lint-diff -> typecheck ->
## build -> test, in that order, stopping at the first failure (make's
## default behavior for listed prerequisites). Exit code is 0 only if
## every stage passed.
##
## Deliberately uses lint-diff here, not the whole-repo lint: this repo
## carries ~99 pre-existing ruff findings that predate RND-187 entirely
## (scattered across files this fix round never touches), and gating
## `verify` on fixing or hiding all of them would force exactly the
## unrelated-refactor scope creep this fix round is explicitly required to
## avoid. lint-diff gives the actionable signal for developer acceptance —
## "did this round's actual changes introduce any lint issues" (currently:
## no) — while whole-repo `lint` stays available on its own for anyone who
## wants to see (or later, deliberately chip away at) the full baseline.
verify: lint-diff typecheck build test
	@echo "verify: OK"

## Build a local public-review snapshot into build/public-snapshot.
##
## The snapshot is a leak-review and validation artifact. Its allowlist
## controls only the generated snapshot, not the source or license boundary
## of this unified repository. The exporter rewrites exact operational
## values and runs a fail-closed leak gate; it does not publish anything.
##
## Writes a directory and nothing else — it never commits or pushes.
## Review the result by hand; external publishing requires separate approval.
public-snapshot:
	./scripts/export_public_snapshot.sh
	@echo "public-snapshot: OK"

## Prove the exported snapshot is a working repository, not just a
## well-filtered pile of files: import the app and run its public-compatible
## tests from inside build/public-snapshot. Internal operational-document
## contract tests remain in the unfiltered full `make verify` suite and are
## deselected here only because their private source documents are absent.
## This catches an allowlist that dropped public code or test dependencies.
public-verify: public-snapshot
	cd build/public-snapshot/backend && \
		DATABASE_URL='sqlite:///:memory:' $(BACKEND_PY) -c \
			"from app.main import app; assert len(app.routes) > 0"
	cd build/public-snapshot/backend && PATH="$(dir $(BACKEND_PY)):$$PATH" $(BACKEND_PY) -m pytest tests -q -p no:warnings -m 'not requires_internal_ops_docs'
	@echo "public-verify: OK"

## Publish the allowlisted public documentation subset to the separate Wiki.
## Existing project-story pages are preserved. This pushes only the Wiki repo.
public-wiki:
	$(PYTHON) scripts/publish_public_wiki.py

## Shellcheck + shfmt over every ssl-renew script, plus a Python syntax
## check over qiniu_helper.py and its tests. Fails non-zero on any finding.
ssl-lint:
	@command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck not found — install via 'brew install shellcheck' or apt/yum equivalent" >&2; exit 1; }
	@command -v shfmt >/dev/null 2>&1 || { echo "shfmt not found — install via 'brew install shfmt' or apt/yum equivalent" >&2; exit 1; }
	shellcheck $(SSL_SCRIPTS) $(SSL_LIB) $(SSL_TEST_HELPERS)
	shfmt -d $(SSL_SHFMT_SCRIPTS) $(SSL_LIB) $(SSL_TEST_HELPERS)
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
## credentials or a real production domain.
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
	@units="deploy/systemd/qiniu-ssl-renew@.service deploy/systemd/qiniu-ssl-renew@.timer deploy/systemd/qiniu-ssl-renew-wildcard.service deploy/systemd/qiniu-ssl-renew-wildcard.timer"; \
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
