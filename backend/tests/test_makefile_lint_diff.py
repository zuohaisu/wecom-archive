"""
Test for `make lint-diff`'s deleted-file handling (RND-206 final
verification fix).

Root cause this guards against: `git diff --name-only` lists a tracked
file that has been deleted from the working tree but not yet staged --
lint-diff fed that path straight to `ruff check`, which fails with
`E902 No such file or directory` on ANY unstaged deletion, not just a
specific filename. The fix adds `--diff-filter=d` to the `git diff`
invocation in the Makefile so deleted paths are excluded generically.

This test proves the fix against a throwaway `git worktree` of the real
repository (so it exercises the actual Makefile, not a reimplementation),
using a disposable dummy .py file rather than depending on any specific
file having been deleted in the working tree.

Run (from backend/):
    pytest tests/test_makefile_lint_diff.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="make not available in this environment")


@pytest.fixture
def repo_worktree(tmp_path):
    """A throwaway `git worktree` checked out from this repo's current
    HEAD, so the test exercises the real Makefile/git behavior without
    touching the actual working tree or requiring a commit."""
    worktree_dir = tmp_path / "lint-diff-worktree"
    branch = f"test-lint-diff-{tmp_path.name}"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_dir), "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    # `git worktree add` checks out HEAD (the last commit) -- it does NOT
    # carry over uncommitted working-tree changes. Copy the real,
    # possibly-modified Makefile in explicitly so this test exercises
    # whatever lint-diff logic is currently on disk, not a stale committed
    # copy.
    shutil.copyfile(REPO_ROOT / "Makefile", worktree_dir / "Makefile")
    try:
        yield worktree_dir
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch], cwd=REPO_ROOT, capture_output=True, text=True
        )


def _run_make(worktree_dir: Path, target: str) -> subprocess.CompletedProcess:
    backend_py = REPO_ROOT / ".venv" / "bin" / "python"
    return subprocess.run(
        [MAKE, f"BACKEND_PY={backend_py}", target],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_lint_diff_ignores_an_unstaged_deleted_python_file(repo_worktree) -> None:
    dummy = repo_worktree / "backend" / "tests" / "test_lint_diff_dummy_deleted_file.py"
    dummy.write_text("# temporary file for test_makefile_lint_diff.py\n")

    subprocess.run(["git", "add", str(dummy)], cwd=repo_worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "temp: add dummy file for lint-diff test", "--no-verify"],
        cwd=repo_worktree,
        check=True,
        capture_output=True,
        text=True,
    )

    # Delete it WITHOUT staging the deletion -- this is exactly the state
    # that previously broke lint-diff (E902), regardless of which file it
    # was.
    dummy.unlink()

    result = _run_make(repo_worktree, "lint-diff")
    assert result.returncode == 0, (
        f"make lint-diff failed on an unstaged-deleted .py file:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "E902" not in result.stdout
    assert "E902" not in result.stderr
    assert "No such file or directory" not in result.stdout
    assert "No such file or directory" not in result.stderr


def test_lint_diff_still_checks_a_modified_python_file_with_a_real_finding(repo_worktree) -> None:
    """The fix must not blind lint-diff to real findings in files that
    genuinely still exist -- only deleted paths should be skipped."""
    target = repo_worktree / "backend" / "tests" / "test_lint_diff_dummy_bad_file.py"
    target.write_text("import os\n\n\ndef test_noop():\n    assert True\n")
    subprocess.run(["git", "add", str(target)], cwd=repo_worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "temp: add dummy file for lint-diff test", "--no-verify"],
        cwd=repo_worktree,
        check=True,
        capture_output=True,
        text=True,
    )

    # Modify it in place (still exists on disk) to introduce a real,
    # unused-import lint finding.
    target.write_text("import os\nimport sys\n\n\ndef test_noop():\n    assert True\n")

    result = _run_make(repo_worktree, "lint-diff")
    assert result.returncode != 0
    assert "F401" in result.stdout or "F401" in result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
