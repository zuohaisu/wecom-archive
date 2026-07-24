"""Regression guard for the standalone search-results page (RND-229).

`_SEARCH_PAGE_HTML` is embedded as a Python *regular* string. A backslash
escaping mistake there previously produced invalid JavaScript that only
failed at runtime in the browser (the build gate only checked the standalone
`i18n.js`, not the embedded page script). This test extracts the real page
script and runs `node --check` on it so such regressions are caught in CI
instead of in production.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._rnd216_web_shims import search_page_html

_SEARCH_PAGE_HTML = search_page_html()


def _extract_page_script(html: str) -> str:
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    return html[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_search_page_script_parses():
    js = _extract_page_script(_SEARCH_PAGE_HTML)
    assert js.strip(), "extracted search-page script is empty"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    try:
        result = subprocess.run(
            ["node", "--check", path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"search-page JS failed `node --check`:\n{result.stderr}"
        )
    finally:
        Path(path).unlink(missing_ok=True)
