"""RND-358 (T4) AC-7 — 'all tools stay read-only; an architecture test blocks
a new write tool from bypassing approval'. AST-based, stdlib-only, mirroring
the style of test_architecture_boundary.py: no write-shaped call may appear
anywhere under app/services/ai_tools/.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "app" / "services" / "ai_tools"

# Any of these attribute/call names appearing anywhere in the package is a
# write-shaped operation and therefore forbidden.
_FORBIDDEN_ATTR_CALLS = {"add", "commit", "delete", "merge", "flush", "execute_write"}
_FORBIDDEN_NAME_CALLS = {"eval", "exec", "system"}
_FORBIDDEN_MODULES = {"subprocess.Popen", "os.system", "os.popen"}
_FORBIDDEN_SQL_KEYWORDS = ("insert ", "update ", "delete ", "drop ", "alter ", "truncate ")

# registry.py's _write_invocation is the one sanctioned exception: it is the
# audit-log write every invocation goes through (recording only tool name /
# field names / status, never tool data), not a diagnostic capability.
_ALLOWED_WRITE_FUNCTIONS = {"_write_invocation"}


def _iter_source_files():
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


def test_package_exists_and_has_files() -> None:
    files = _iter_source_files()
    assert files, "expected app/services/ai_tools/*.py to exist"


def test_no_write_shaped_calls_outside_the_audit_log_writer() -> None:
    violations = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function_stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                function_stack.append(node.name)
                self.generic_visit(node)
                function_stack.pop()

            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                current_function = function_stack[-1] if function_stack else "<module>"
                if current_function in _ALLOWED_WRITE_FUNCTIONS:
                    self.generic_visit(node)
                    return

                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_ATTR_CALLS:
                    violations.append(f"{path.name}:{node.lineno} db.{func.attr}(...) in {current_function}")
                if isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAME_CALLS:
                    violations.append(f"{path.name}:{node.lineno} {func.id}(...) in {current_function}")
                self.generic_visit(node)

        Visitor().visit(tree)

    assert violations == [], f"write-shaped calls found outside _write_invocation: {violations}"


def test_no_subprocess_shell_or_arbitrary_process_execution() -> None:
    violations = []
    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                dotted = None
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    dotted = f"{func.value.id}.{func.attr}"
                if dotted in _FORBIDDEN_MODULES:
                    violations.append(f"{path.name}:{node.lineno} {dotted}(...)")
                if isinstance(func, ast.Attribute) and func.attr == "run" and isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                    # subprocess.run is allowed ONLY with a fixed argv list
                    # containing "git" — this is handlers.py's cached version
                    # lookup, never a shell string or caller-controlled args.
                    if node.args and isinstance(node.args[0], ast.List):
                        elements = node.args[0].elts
                        if elements and isinstance(elements[0], ast.Constant) and elements[0].value == "git":
                            continue
                    violations.append(f"{path.name}:{node.lineno} subprocess.run(...) with non-fixed argv")

    assert violations == [], f"process-execution calls found: {violations}"


def test_no_raw_sql_write_keywords() -> None:
    violations = []
    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8").lower()
        for keyword in _FORBIDDEN_SQL_KEYWORDS:
            if keyword in source:
                violations.append(f"{path.name}: contains {keyword.strip()!r}")

    assert violations == [], f"raw SQL write keywords found: {violations}"


def test_registered_tools_have_no_write_capable_scope_escape() -> None:
    """Every ToolSpec's handler must be one of the module-level functions
    defined in handlers.py (not a lambda wrapping a service call that
    itself writes) — a cheap static check that the registry can't be
    handed a closure smuggling in extra behavior."""
    import ast as _ast

    handlers_path = _PACKAGE_ROOT / "handlers.py"
    tree = _ast.parse(handlers_path.read_text(encoding="utf-8"))
    register_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "register"
    ]
    assert register_calls, "expected at least one register(...) call in handlers.py"
    for call in register_calls:
        toolspec_call = call.args[0]
        handler_kwarg = next(kw for kw in toolspec_call.keywords if kw.arg == "handler")
        assert isinstance(handler_kwarg.value, ast.Name), "tool handler must be a plain named function, not a lambda/closure"
