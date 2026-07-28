"""
RND-224 — architecture boundary guardrails.

Purpose (see docs/adr/0002-module-boundaries-and-composition-root.md and
DEV_AGENT_RULES.md > Architecture Boundaries): prevent business logic from
flowing back into `app/main.py` or into a "service→router" reverse
dependency, which is exactly how this codebase used to accumulate an
oversized, all-in-one router/main before the RND-212 refactor chain split
it apart. Letting that regress would re-inflate the context an AI agent has
to load to work on any one feature.

This module is intentionally stdlib-only (ast + pathlib) — no import-linter,
grimp, or pytest-arch. All checkers are plain functions that take a path or
source string, so the "real repo" tests and the synthetic self-proving
tests below call the exact same code.

What this file does NOT enforce (see DEV_AGENT_RULES.md for the full
rationale): no file/function/route line-count thresholds, no requirement
that every router endpoint be backed by a service — simple CRUD directly in
a router is allowed. This file only enforces *dependency direction* and
*composition-root purity*.
"""

from __future__ import annotations

import ast
from pathlib import Path

# ── Project roots ───────────────────────────────────────────────────────

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_APP_ROOT = _BACKEND_ROOT / "app"
_MAIN_FILE = _APP_ROOT / "main.py"

# ── Layer taxonomy ──────────────────────────────────────────────────────
#
# layer_of() classifies a dotted module path (e.g. "app.media_download" or
# "app.routers.conversations") into one of the layers below, using explicit
# rule tables rather than a directory walk, so the classification is easy
# to audit and to extend as new modules are added.
#
# Dependency direction allowed by this layer model:
#   main      -> routers, services, schemas, db          (composition root sees everything)
#   routers   -> services, schemas, db                   (NOT main, see below)
#   services  -> db, schemas, other services/domain modules (NOT routers, NOT main)
#   schemas   -> (leaf; may be depended on by routers/services)
#   db        -> (leaf; may be depended on by services)
#   other     -> unclassified; not subject to the router/main reverse-dependency
#                rules below (kept narrow on purpose so unrelated modules can't
#                silently widen what's enforced)

_MAIN_MODULE = "app.main"

# Packages: every submodule underneath is that layer.
_LAYER_PACKAGES = {
    "app.routers": "router",
    "app.services": "service",
    "app.schemas": "schema",
    "app.db": "db",
}

# Flat, single-file domain/service modules living directly under app/ (not
# inside app/services/) as of the RND-212 refactor chain landing
# (RND-218..223). This is a "mixed" layout on purpose — the chain extracted
# focused modules without forcing every one of them into app/services/ — so
# this list is the explicit source of truth for which flat modules count as
# "service/domain" for the reverse-dependency rule. A new flat domain
# module must be added here (or moved into app/services/) to be covered;
# anything left off falls into "other" and is unenforced.
_FLAT_SERVICE_MODULES = {
    "app.auth",  # OAuth/session helpers -- NOT app.routers.auth (router of the same short name)
    "app.conversation_membership",
    "app.display_names",
    "app.email",
    "app.html_helpers",
    "app.i18n_assets",
    "app.media_classification",
    "app.media_download",
    "app.media_event_dispatch",
    "app.media_storage",
    "app.media_thumbnails",
    "app.message_type_registry",
    "app.qiniu_storage",
    "app.reachability_audit",  # flat domain module -- NOT app.routers.reachability_audit
    "app.revoke_reconciliation",
    "app.structured_message_parser",
    "app.thumbnail_pipeline",
    "app.voice_playback_pipeline",
    "app.voice_transcode",
    "app.wecom_contacts",
    "app.sdk",
    "app.sdk.wecom_sdk",
    "app.session_lifecycle",
    "app.web",  # templating/static-asset helpers (render_template, STATIC_VERSION)
}

# Narrow, explicit exceptions to the reverse-dependency rules below. Empty
# by default -- any addition must be a deliberate, reviewed decision, not a
# workaround for a violation the checker happened to find. Record the
# reason inline and cross-reference an ADR if one is added.
#
# Shape: frozenset of (importing_module, imported_module) pairs, e.g.
#   frozenset({("app.services.foo", "app.routers.bar")})
ALLOWED_EXCEPTIONS: "frozenset[tuple[str, str]]" = frozenset()

# The only routes the composition root (app/main.py) may register directly.
# Anything else registered via @app.get/post/put/patch/delete/... is a
# violation -- business routes belong in app/routers/*, wired in via
# app.include_router(...).
_HEALTH_PROBE_PATHS = {"/health", "/health/live", "/health/ready"}

_ROUTE_DECORATOR_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_QUERY_CALL_ATTRS = {"query", "execute"}


def layer_of(module: str) -> str:
    """Classify a dotted module path into a layer.

    Order matters: the main/package-prefix checks must run before the flat
    "app.<name>" module set lookup. app.auth (service) and app.routers.auth
    (router) share a basename but must never be confused with each other --
    the full dotted path is what's classified, never a basename.
    """
    if module == _MAIN_MODULE or module.startswith(_MAIN_MODULE + "."):
        return "main"
    for prefix, layer in _LAYER_PACKAGES.items():
        if module == prefix or module.startswith(prefix + "."):
            return layer
    if module in _FLAT_SERVICE_MODULES:
        return "service"
    return "other"


# ── AST helpers (pure functions -- operate on a source string) ──────────


def imported_modules(py_source: str) -> set[str]:
    """Return the set of top-level dotted module paths this source imports.

    Only absolute imports are resolved (this codebase uses absolute
    `app.x` imports throughout, verified against the real tree); a bare
    relative import (`from . import x`) resolves to level > 0 and is
    dropped rather than guessed at.
    """
    tree = ast.parse(py_source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def _route_decorator_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every @xxx.get(...)/@xxx.post(...)/... decorator call node,
    for any decorated function at any nesting depth. Route registration in
    this codebase happens inside create_app(), not at module scope, so this
    must walk the full tree rather than just module.body."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        decorator_list = getattr(node, "decorator_list", None)
        if not decorator_list:
            continue
        for decorator in decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _ROUTE_DECORATOR_METHODS
            ):
                calls.append(decorator)
    return calls


def _decorator_path_argument(call: ast.Call) -> "str | None":
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def find_composition_root_route_violations(py_source: str) -> list[str]:
    """Route decorators registered directly in composition-root source that
    are not on the health-probe whitelist. Business routes belong in
    app/routers/*, registered via app.include_router(...)."""
    tree = ast.parse(py_source)
    violations = []
    for call in _route_decorator_calls(tree):
        path = _decorator_path_argument(call)
        if path not in _HEALTH_PROBE_PATHS:
            shown = path if path is not None else "<dynamic/unknown path>"
            violations.append(
                f"composition root registers route {shown!r} directly "
                f"(only {sorted(_HEALTH_PROBE_PATHS)} are allowed here) -- "
                f"move this endpoint into app/routers/* and register it via include_router()"
            )
    return violations


def find_composition_root_query_violations(py_source: str) -> list[str]:
    """Direct SQLAlchemy ORM/Core query calls in composition-root source.
    DB access belongs in the db layer or a service, reached from the
    composition root only indirectly (e.g. via a router/service)."""
    tree = ast.parse(py_source)
    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _QUERY_CALL_ATTRS
        ):
            violations.append(
                f"composition root calls .{node.func.attr}(...) directly (line {node.lineno}) -- "
                f"database access belongs in app/db or a service module, not in main.py/create_app()"
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "select":
            violations.append(
                f"composition root calls select(...) directly (line {node.lineno}) -- "
                f"database access belongs in app/db or a service module, not in main.py/create_app()"
            )
    return violations


_INLINE_MARKUP_MARKERS = ("<!doctype", "<html", "<head", "<body", "<style", "<script", "</div>")


def find_composition_root_inline_markup_violations(py_source: str) -> list[str]:
    """Inline HTML/CSS/JS string literals in composition-root source.
    Frontend assets belong in app/web/templates + app/web/static, served
    via the template renderer / StaticFiles mount, not as inline strings."""
    tree = ast.parse(py_source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if any(marker in lowered for marker in _INLINE_MARKUP_MARKERS):
                violations.append(
                    f"composition root contains an inline HTML/CSS/JS string literal (line {node.lineno}) -- "
                    f"frontend assets belong in app/web/templates or app/web/static, not inline in main.py"
                )
    return violations


def find_composition_root_violations(py_source: str) -> list[str]:
    return (
        find_composition_root_route_violations(py_source)
        + find_composition_root_query_violations(py_source)
        + find_composition_root_inline_markup_violations(py_source)
    )


def _module_name_for(app_root: Path, path: Path) -> str:
    rel = path.relative_to(app_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def find_reverse_dependency_violations(app_root: Path) -> list[str]:
    """Scan every .py file under app_root and report:
      - any service/domain-layer module importing an app.routers.* module
      - any router-layer module importing app.main
    Exceptions require an explicit, reviewed entry in ALLOWED_EXCEPTIONS.
    """
    violations = []
    for path in sorted(app_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name_for(app_root, path)
        layer = layer_of(module)
        if layer not in ("service", "router"):
            continue
        imports = imported_modules(path.read_text(encoding="utf-8"))
        for imported in imports:
            if (module, imported) in ALLOWED_EXCEPTIONS:
                continue
            imported_layer = layer_of(imported)
            if layer == "service" and imported_layer == "router":
                violations.append(
                    f"{module} imports {imported} -- service/domain layer must not depend on "
                    f"the router layer; push the shared logic down into a service/domain or db module instead"
                )
            if layer == "router" and imported == _MAIN_MODULE:
                violations.append(
                    f"{module} imports {imported} -- routers must not depend on the composition root "
                    f"(app.main); anything a router needs from main should be passed in via FastAPI "
                    f"dependency injection instead"
                )
    return violations


# ── Real-repo assertions ──────────────────────────────────────────────────


class TestRealRepoHasNoReverseDependencies:
    def test_no_service_or_router_reverse_dependencies(self) -> None:
        violations = find_reverse_dependency_violations(_APP_ROOT)
        assert not violations, "Architecture boundary violation(s):\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestRealCompositionRootIsClean:
    def test_main_has_no_unlisted_routes_queries_or_inline_markup(self) -> None:
        source = _MAIN_FILE.read_text(encoding="utf-8")
        violations = find_composition_root_violations(source)
        assert not violations, "Composition root violation(s) in app/main.py:\n" + "\n".join(
            f"  - {v}" for v in violations
        )

    def test_health_probe_whitelist_matches_main_py_registered_routes(self) -> None:
        """Guards against the whitelist and the real code silently drifting
        apart in either direction (e.g. a probe removed from main.py but
        left in the whitelist, masking a future unrelated addition)."""
        source = _MAIN_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        registered_paths = {_decorator_path_argument(call) for call in _route_decorator_calls(tree)}
        assert registered_paths == _HEALTH_PROBE_PATHS


# ── Self-proving synthetic tests ──────────────────────────────────────────
#
# These call the exact same functions exercised against the real repo
# above, against small synthetic sources/trees, to prove the checkers
# actually detect what they claim to (and don't false-positive on
# legitimate patterns). Required by the RND-224 acceptance contract: "CI
# must be able to detect violations", not just "CI has zero violations
# today" (which could just mean the checker is a no-op).


def _make_synthetic_app(tmp_path: Path, files: dict) -> Path:
    """Build a throwaway app/ package under tmp_path with the given
    {relative_path: source} files and return its root, so
    find_reverse_dependency_violations (which walks a real directory tree)
    can be exercised without touching the real repository."""
    app_root = tmp_path / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "__init__.py").write_text("", encoding="utf-8")
    for rel_path, content in files.items():
        full_path = app_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        init_file = full_path.parent / "__init__.py"
        if full_path.parent != app_root and not init_file.exists():
            init_file.write_text("", encoding="utf-8")
        full_path.write_text(content, encoding="utf-8")
    return app_root


class TestReverseDependencyDetection:
    def test_detects_service_importing_router(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "services/__init__.py": "",
                "services/widgets.py": "from app.routers.widgets import router\n",
                "routers/__init__.py": "",
                "routers/widgets.py": "router = None\n",
            },
        )
        violations = find_reverse_dependency_violations(app_root)
        assert any("app.services.widgets" in v and "app.routers.widgets" in v for v in violations)

    def test_detects_flat_domain_module_importing_router(self, tmp_path: Path) -> None:
        # app.auth is in _FLAT_SERVICE_MODULES; exercise that path specifically
        # since it's the one most likely to be miscategorized by name collision
        # with app.routers.auth.
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "auth.py": "from app.routers.auth import router\n",
                "routers/__init__.py": "",
                "routers/auth.py": "router = None\n",
            },
        )
        violations = find_reverse_dependency_violations(app_root)
        assert any("app.auth" in v and "app.routers.auth" in v for v in violations)

    def test_detects_router_importing_main(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "main.py": "app = None\n",
                "routers/__init__.py": "",
                "routers/widgets.py": "from app.main import app\n",
            },
        )
        violations = find_reverse_dependency_violations(app_root)
        assert any("app.routers.widgets" in v and "app.main" in v for v in violations)

    def test_does_not_flag_router_importing_service(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "services/__init__.py": "",
                "services/widgets.py": "x = 1\n",
                "routers/__init__.py": "",
                "routers/widgets.py": "from app.services.widgets import x\n",
            },
        )
        assert find_reverse_dependency_violations(app_root) == []

    def test_does_not_flag_service_importing_db_or_other_service(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "db/__init__.py": "",
                "db/models.py": "x = 1\n",
                "services/__init__.py": "",
                "services/a.py": "from app.db.models import x\n",
                "services/b.py": "from app.services.a import x\n",
            },
        )
        assert find_reverse_dependency_violations(app_root) == []

    def test_does_not_flag_main_importing_routers_and_services(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "main.py": (
                    "from app.routers.widgets import router\n"
                    "from app.services.widgets import helper\n"
                ),
                "routers/__init__.py": "",
                "routers/widgets.py": "router = None\n",
                "services/__init__.py": "",
                "services/widgets.py": "def helper(): pass\n",
            },
        )
        assert find_reverse_dependency_violations(app_root) == []

    def test_allowed_exceptions_suppress_a_listed_pair(self, tmp_path: Path) -> None:
        app_root = _make_synthetic_app(
            tmp_path,
            {
                "services/__init__.py": "",
                "services/widgets.py": "from app.routers.widgets import router\n",
                "routers/__init__.py": "",
                "routers/widgets.py": "router = None\n",
            },
        )
        assert find_reverse_dependency_violations(app_root) != []
        global ALLOWED_EXCEPTIONS
        original = ALLOWED_EXCEPTIONS
        try:
            ALLOWED_EXCEPTIONS = frozenset({("app.services.widgets", "app.routers.widgets")})
            assert find_reverse_dependency_violations(app_root) == []
        finally:
            ALLOWED_EXCEPTIONS = original


class TestCompositionRootDetection:
    def test_detects_business_route_registration(self) -> None:
        source = '@app.get("/api/widgets")\ndef list_widgets():\n    return []\n'
        violations = find_composition_root_route_violations(source)
        assert any("/api/widgets" in v for v in violations)

    def test_allows_health_probe_routes(self) -> None:
        source = (
            '@app.get("/health")\n'
            "def health():\n"
            "    return {'status': 'ok'}\n"
            '@app.get("/health/live")\n'
            "def health_live():\n"
            "    return {'status': 'ok'}\n"
            '@app.get("/health/ready")\n'
            "def health_ready():\n"
            "    return {'status': 'ok'}\n"
        )
        assert find_composition_root_route_violations(source) == []

    def test_detects_direct_query_call(self) -> None:
        source = "def handler(db):\n    return db.query(Model).all()\n"
        violations = find_composition_root_query_violations(source)
        assert any(".query(" in v for v in violations)

    def test_detects_session_execute_call(self) -> None:
        source = "def handler(session):\n    return session.execute(stmt)\n"
        violations = find_composition_root_query_violations(source)
        assert any(".execute(" in v for v in violations)

    def test_detects_bare_select_call(self) -> None:
        source = "from sqlalchemy import select\ndef handler():\n    return select(Model)\n"
        violations = find_composition_root_query_violations(source)
        assert any("select(" in v for v in violations)

    def test_does_not_flag_unrelated_dict_or_object_get(self) -> None:
        """.get(...) is not in _QUERY_CALL_ATTRS -- deliberately narrow to
        the ORM/Core query surface named in the ticket (.query/.execute/
        select()), not every attribute access, to avoid false positives on
        ordinary dict/object .get()."""
        source = "def handler(db, widget_id):\n    return db.get(Widget, widget_id)\n"
        assert find_composition_root_query_violations(source) == []

    def test_detects_inline_script_tag(self) -> None:
        source = 'HTML = "<script>alert(1)</script>"\n'
        assert find_composition_root_inline_markup_violations(source)

    def test_detects_inline_doctype(self) -> None:
        source = 'PAGE = "<!DOCTYPE html><html><body>hi</body></html>"\n'
        assert find_composition_root_inline_markup_violations(source)

    def test_does_not_flag_plain_docstrings_or_comments(self) -> None:
        source = (
            '"""This module wires up the FastAPI app. See docs/ARCHITECTURE.md."""\n'
            "# not html: <config value>\n"
            "x = 1\n"
        )
        assert find_composition_root_violations(source) == []


class TestLayerOfClassifier:
    def test_router_and_service_with_same_basename_are_distinguished(self) -> None:
        assert layer_of("app.auth") == "service"
        assert layer_of("app.routers.auth") == "router"
        assert layer_of("app.reachability_audit") == "service"
        assert layer_of("app.routers.reachability_audit") == "router"

    def test_main_module(self) -> None:
        assert layer_of("app.main") == "main"

    def test_services_package_and_flat_modules(self) -> None:
        assert layer_of("app.services.media_access") == "service"
        assert layer_of("app.media_download") == "service"

    def test_db_and_schemas(self) -> None:
        assert layer_of("app.db.models") == "db"
        assert layer_of("app.schemas.messages") == "schema"

    def test_unknown_module_is_other_and_unenforced(self) -> None:
        assert layer_of("app.settings") == "other"
        assert layer_of("app.some_future_module_nobody_classified_yet") == "other"


class TestNoLineCountThresholds:
    def test_this_file_asserts_no_size_based_rule(self) -> None:
        """Documents the RND-224 non-goal directly in the suite: no
        assertion anywhere in this module may key off line/statement
        counts. Grep-based self-check so a future edit accidentally adding
        one gets caught here rather than relying on code review alone. The
        scan excludes this method's own body, since it necessarily names
        the forbidden substrings it's checking for."""
        source = Path(__file__).read_text(encoding="utf-8")
        scannable = source.split("class TestNoLineCountThresholds", 1)[0]
        forbidden_substrings = ("len(lines)", "line_count", "max_lines", "MAX_LINES", "too many lines")
        for forbidden in forbidden_substrings:
            assert forbidden not in scannable
