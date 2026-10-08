from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ISSUE_TEMPLATES = ROOT / ".github/ISSUE_TEMPLATE"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        data = yaml.safe_load(source)
    assert isinstance(data, dict)
    return data


def test_contribution_guide_uses_dco_not_cla() -> None:
    contributing = _read("CONTRIBUTING.md")

    assert "Developer Certificate of Origin" in contributing
    assert "Version 1.1" in contributing
    assert "git commit -s" in contributing
    assert "does not transfer copyright" in contributing
    assert "CLA" in contributing
    assert (ROOT / "CONTRIBUTING.md").is_file()
    assert (ROOT / "CODE_OF_CONDUCT.md").is_file()
    assert not (ROOT / "CLA.md").exists()


def test_code_of_conduct_has_reporting_contact_and_attribution() -> None:
    conduct = _read("CODE_OF_CONDUCT.md")

    assert "[INSERT CONTACT METHOD]" not in conduct
    assert "repository owner's GitHub profile" in conduct
    assert "version 2.1" in conduct
    assert "Mozilla's code of conduct enforcement ladder" in conduct
    assert "public issues or discussions" in conduct


def test_issue_forms_are_valid_yaml_with_expected_templates() -> None:
    bug = _load_yaml(ISSUE_TEMPLATES / "bug_report.yml")
    feature = _load_yaml(ISSUE_TEMPLATES / "feature_request.yml")
    config = _load_yaml(ISSUE_TEMPLATES / "config.yml")

    assert bug["name"] == "Bug report"
    assert feature["name"] == "Feature request"
    assert config["blank_issues_enabled"] is True
    assert {item["name"] for item in config["contact_links"]} == {
        "Security policy",
        "Community discussions",
    }

    for template in (bug, feature):
        body = template["body"]
        assert any(item.get("type") == "markdown" for item in body)
        assert any(item.get("id") == "edition" for item in body)
        assert any("security" in str(item).lower() for item in body)


def test_community_templates_and_pr_checklist_are_in_public_snapshot() -> None:
    pull_request = _read(".github/PULL_REQUEST_TEMPLATE.md")

    assert ISSUE_TEMPLATES.is_dir()
    assert (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").is_file()
    assert "## Ticket → commit mapping" in pull_request
    assert "exactly one commit" in pull_request
    assert "## Validation" in pull_request
    assert "make verify" in pull_request
    assert "Signed-off-by" in pull_request
    assert "customer data" in pull_request


def test_public_contribution_entrypoints_use_the_current_edition_contract() -> None:
    readme = _read("README.md")
    contributing = _read("CONTRIBUTING.md")
    architecture = _read("docs/ARCHITECTURE.md")
    adr_0003 = _read("docs/adr/0003-product-strategy-hosted-only.md")
    adr_0007 = _read("docs/adr/0007-runtime-edition-policies.md")

    assert "[CONTRIBUTING.md](CONTRIBUTING.md)" in readme
    assert "[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)" in contributing
    assert "architecture/current-state.md" not in architecture
    assert "AGPL-3.0" in architecture
    assert "ADR-0007" in architecture
    assert "Superseded" in adr_0003.splitlines()[2]
    assert "adr-0003 remains as a historical decision record" in adr_0007.lower()

    # These private historical guides are excluded from the public snapshot;
    # check their supersession notices in the full source checkout when present.
    for path in ("docs/DEVELOPMENT.md", "docs/architecture/current-state.md"):
        source = ROOT / path
        if source.is_file():
            text = source.read_text(encoding="utf-8").lower()
            assert "superseded" in text
            assert "adr-0007" in text or "contributing.md" in text
