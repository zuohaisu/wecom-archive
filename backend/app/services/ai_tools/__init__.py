"""Read-only diagnostic tool registry for AI support (RND-358 / T4).

Every module in this package must stay strictly read-only — see
backend/tests/test_ai_tools_read_only_boundary.py, an AST-based hard gate
that rejects any write-shaped call (db.add/commit/merge, row removal, raw
SQL mutation statements, subprocess/os.system/eval/exec) anywhere under
this package. The model can only ever call something registered in
registry.py's tool table — never an arbitrary shell/SQL/HTTP request.
"""
