# ADR-0002: Module Boundaries and Composition Root Enforcement

**状态**：草稿（待 Haisu 评审）  
**日期**: 2026-07-26  
**作者**: Claude Code  
**关联任务**: RND-224（父任务 RND-212）

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1 | 2026-07-26 | Initial draft - Architecture boundaries & composition root enforcement |

---

## 目录

1. [Context](#context)
2. [Decision](#decision)
3. [Consequences](#consequences)
4. [Alternatives Considered](#alternatives-considered)
5. [References](#references)

---

## 1. Context

The WeCom Archive codebase has evolved with a clear architectural intent expressed in `DEV_AGENT_RULES.md`:

- **Service layer** (`backend/app/services/`): Business logic, domain services, worker functions
- **Router layer** (`backend/app/routers/`): HTTP endpoints, request/response handling
- **Composition root** (`backend/app/main.py`): App bootstrap, dependency injection

However, this boundary is not enforced at the CI level. There's no automated check to detect:
1. Service modules importing from routers (reverse dependency)
2. Router modules importing from main (circular dependency)
3. main.py containing inline business logic, database queries, or HTML/CSS/JS

This leads to:
- Violation of separation of concerns
- Increased coupling between layers
- Difficulty in refactoring (e.g., RND-218~223 would be harder without explicit guards)
- "Main.py creep" where business logic accumulates in the composition root

**Why this matters beyond code hygiene** (motivation revision, 2026-07-24
11:49): the primary risk this ADR guards against is AI-agent context bloat,
not just style. RND-212 split business logic out of `main.py`/an all-in-one
router specifically so an AI agent working on one feature doesn't have to
load an oversized, unrelated module into context. If business logic flows
back into `main.py` or a reverse "service→router" dependency forms, that
regression re-inflates the context every future agent conversation has to
load — this ADR and its guardrail test exist to make that regression fail
CI instead of silently accumulating.

---

## 2. Decision

### 2.1 Architecture Boundary Rules

The following dependencies are **strictly forbidden**:

```
service/domain → routers ❌
routers → main.py ❌
main.py → business logic / DB queries / HTML-CSS-JS ❌
```

**Allowed patterns**:
```
routers → services/domain ✓
services/domain → db/models, schemas, other services/domain ✓
main.py → routers, services, db (composition root sees everything) ✓
```

**"Service/domain layer" is broader than `app/services/`.** The RND-212
chain landed a mixed layout: a `app/services/` package (`listing_service`,
`timeline_service`, `media_access`, `sync_worker`, `decrypt_worker`,
`media_worker`) plus a set of flat, single-file domain modules living
directly under `app/` (e.g. `media_download.py`, `media_storage.py`,
`structured_message_parser.py`, `message_type_registry.py`, `auth.py`,
`reachability_audit.py`, `wecom_contacts.py`, `web/`, `sdk/`). Both count as
"service/domain" for the reverse-dependency rule. The explicit, current
list lives in the `_FLAT_SERVICE_MODULES` constant in
`backend/tests/test_architecture_boundary.py` — that constant, not this
prose, is the source of truth when the two disagree.

Two module pairs share a basename across layers and must be classified by
full dotted path, never by basename: `app.auth` (service) vs.
`app.routers.auth` (router), and `app.reachability_audit` (service) vs.
`app.routers.reachability_audit` (router). The guardrail test's `layer_of()`
classifier and its unit tests exist specifically to keep this
disambiguation correct as the codebase grows.

**Exceptions**: a narrow, explicit `ALLOWED_EXCEPTIONS` set in the
guardrail test can whitelist a specific `(importing_module,
imported_module)` pair. It is empty by default; any addition requires
Haisu's approval, an inline reason in the test, and generally its own ADR.

### 2.2 Composition Root Purity

`main.py` must contain **only**:
1. FastAPI app instantiation
2. Dependency overrides/configuration
3. Health probe routes (`/health`, `/health/live`, `/health/ready`)

Any business route registration, database query, or HTML/JS embedding is **prohibited**.

### 2.3 What this deliberately does NOT restrict

Simple queries or simple CRUD directly inside a router are allowed — not
every endpoint needs a dedicated service. Splitting is driven by functional
cohesion (one domain = one service + interface), never by file size: no
line-count, function-length, or route-body-length threshold exists in the
guardrail test, and none should be added. This is a deliberate non-goal
(see the RND-224 ticket's explicit "no mechanical line-count gating"
requirement) — over-fragmenting a small router into services it doesn't
need would itself work against the context-budget motivation in §1, by
forcing an agent to load more files to understand one feature, not fewer.

### 2.4 Automated Enforcement

We implement **pure stdlib-only** architecture tests in `backend/tests/test_architecture_boundary.py`:

- Uses `ast` + `pathlib` — **no third-party dependencies**
- Classifies every module into a layer via an explicit `layer_of()` rule
  table (see §2.1), then scans the whole `app/` tree for reverse
  dependencies and scans `main.py` for route/query/inline-markup violations
- Includes synthetic positive/negative test cases (against real production
  functions, not a parallel reimplementation) to prove detection actually
  fires and doesn't false-positive on legitimate patterns

### 2.5 CI Integration

The test runs automatically via:
```bash
cd backend && python -m pytest tests/test_architecture_boundary.py -q
```

This is part of the full `make verify` command, ensuring CI catches violations before merge.

---

## 3. Consequences

### Positive Outcomes

1. **Architectural clarity**: The layered structure becomes hard-enforced, not just documented
2. **Refactoring safety**: Future refactors (like service extraction) have clear guardrails
3. **No new dependencies**: Pure stdlib implementation keeps the project lightweight
4. **Fast feedback loop**: Architects see boundary violations immediately in CI
5. **Documentation alignment**: `DEV_AGENT_RULES.md` now has corresponding executable spec

### Negative Trade-offs

1. **False positives risk**: Complex but legitimate patterns might be flagged (mitigated by test coverage)
2. **Explicit-list upkeep**: because the service/domain layer includes flat modules under `app/` (not just `app/services/`), a new flat domain module must be added to `_FLAT_SERVICE_MODULES` to be covered — an omission fails open (unenforced), not closed. This is a deliberate trade-off (see §2.1's "other" fallback) but means the list needs occasional maintenance as the codebase grows
3. **Limited expressiveness**: AST-based checking can't understand semantic relationships (acceptable given scope)

### Non-Breaking Guarantee

This task does **not**:
- Add any third-party dependencies
- Modify existing runtime behavior
- Require migration scripts
- Change API contracts

The existing codebase already conforms to these rules after RND-218~223; this simply codifies them.

---

## 4. Alternatives Considered

### A. Use Existing Tools (import-linter, grimp, pytest-arch)

**Approach**: Leverage established Python architecture testing tools.

**Rejected because**:
1. Requires adding third-party dependency to `requirements.txt`
2. Tool configuration overhead (YAML configs, profile definitions)
3. Overkill for simple dependency graph checking
4. Violates the principle of minimal dependencies

### B. Pre-commit Hooks Only

**Approach**: Run lint-checks via pre-commit, not CI.

**Rejected because**:
1. Local hooks don't catch PR submissions from forks
2. Not deterministic across developer environments
3. CI is the single source of truth for quality gates

### C. Code Review Enforcement Only

**Approach**: Rely on human review to catch violations.

**Rejected because**:
1. Not scalable as team grows
2. Human error inevitable
3. Inconsistent enforcement over time
4. Doesn't support the "shift-left" QA philosophy

### D. AST + Standard Library (Selected)

**Approach**: Custom pure-Python checks using `ast` and `pathlib`.

**Chosen because**:
1. Zero dependencies — aligns with project simplicity goal
2. Full control over rule definition
3. Transparent and understandable implementation
4. Fast execution (< 1s on entire backend)
5. Easy to extend with new patterns if needed

---

## 5. References

- **Linear**: RND-224 (this ADR), parent RND-212 (modularization chain)
- **Implementation**: `backend/tests/test_architecture_boundary.py`
- **Rule Documentation**: `DEV_AGENT_RULES.md` § Architecture Boundaries, `docs/AGENTS.md` § Architecture Boundaries, root `AGENTS.md`
- **Related Tasks**: RND-218 (legacy routing out of main), RND-219 (listing service), RND-220 (timeline resolution), RND-221 (media access service), RND-222 (worker functions), RND-223 (App Factory + typed settings — this ADR codifies the boundary the chain converged on)

---

_Last updated: 2026-07-26_
