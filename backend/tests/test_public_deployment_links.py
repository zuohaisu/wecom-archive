"""Public deployment documentation must not link to withheld internal files."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCS = (
    Path("docs/DEPLOYMENT.md"),
    Path("docs/OPERATIONS.md"),
    Path("docs/adr/0004-annual-plan-wechat-pay-gates.md"),
    Path("ssl-renew/README.md"),
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\((<[^>]+>|[^)]+)\)")
EXTERNAL_SCHEMES = ("https://", "http://", "mailto:", "tel:", "data:")


def test_public_deployment_related_markdown_links_resolve() -> None:
    missing: list[str] = []

    for relative_doc in PUBLIC_DOCS:
        doc = ROOT / relative_doc
        if not doc.is_file():
            missing.append(f"{relative_doc}: document is absent")
            continue

        text = doc.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            target = match.group(1).strip("<>").split()[0]
            if target.startswith(EXTERNAL_SCHEMES) or target.startswith("#"):
                continue

            path = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path:
                continue
            resolved = (doc.parent / path).resolve()
            if not resolved.exists():
                line = text.count("\n", 0, match.start()) + 1
                missing.append(f"{relative_doc}:{line}: {target}")

    assert not missing, "public documentation has unresolved local links:\n" + "\n".join(missing)
