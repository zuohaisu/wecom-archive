"""GH-155: exporter regression tests, intentionally absent from public snapshots."""

from pathlib import Path
import shutil
import subprocess

import pytest


EXPORTER = Path(__file__).resolve().parents[2] / "scripts/export_public_snapshot.sh"
ALLOWLIST = EXPORTER.parent / "public_allowlist.txt"


@pytest.fixture
def snapshot_repo(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(EXPORTER, scripts / EXPORTER.name)
    (scripts / "public_allowlist.txt").write_text("README.md\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo, tmp_path / "snapshot"


def export(snapshot_repo, content):
    repo, output = snapshot_repo
    (repo / "README.md").write_text(content)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    result = subprocess.run(
        ["bash", str(repo / "scripts" / EXPORTER.name), str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    return result, (output / "README.md").read_text()


def test_renamed_repository_has_an_explicit_mapping():
    assert (
        "'s|zuohaisu/wecom-archive|your-org/wecom-archive|g'"
        in EXPORTER.read_text()
    )


def test_export_uses_unbranded_placeholders_and_preserves_vendor(snapshot_repo):
    result, content = export(
        snapshot_repo,
        "git@github.com:zuohaisu/wecom-archive.git\n"
        "/srv/apps/wecom-archive-365/current\n"
        "/srv/apps/wecom-archive-365-nonprod/current\n"
        "archive.crowntime.cn qwhhcd.crowntime.cn\n"
        "Crowntime WeCom Archive https://crowntime.cn\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Leak gate passed" in result.stdout
    assert content == (
        "git@github.com:your-org/wecom-archive.git\n"
        "/srv/apps/wecom-archive/current\n"
        "/srv/apps/wecom-archive-nonprod/current\n"
        "archive.crowntime.cn archive.example.com\n"
        "Crowntime WeCom Archive https://crowntime.cn\n"
    )


def test_export_preserves_company_subdomains_and_mail_domain(snapshot_repo):
    input_content = (
        "https://foo.crowntime.cn/health\n"
        "notifications@mail.crowntime.cn\n"
    )
    result, content = export(snapshot_repo, input_content)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Leak gate passed" in result.stdout
    assert content == input_content


def test_mail_domain_is_not_a_snapshot_path_allowlist_entry():
    entries = [
        line.split("#", 1)[0].strip()
        for line in ALLOWLIST.read_text().splitlines()
    ]
    assert all("mail.crowntime.cn" not in entry for entry in entries)


def test_public_snapshot_includes_product_docs_site_and_generic_checks():
    entries = {
        line.split("#", 1)[0].strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "docs/kb/public" in entries
    assert "docs/kb/customer" in entries
    assert "static_site/company_homepage/index.html" in entries
    assert "static_site/company_homepage/pricing.html" in entries
    assert "static_site/company_homepage/demo" in entries
    assert "static_site/company_homepage/README.md" not in entries
    assert "docs/DEPLOYMENT.md" in entries
    assert "scripts/assert_scheduled_workloads.sh" in entries
    assert ".github/workflows/uptime-check.yml" in entries


def test_public_snapshot_keeps_production_ops_runbooks_private():
    entries = {
        line.split("#", 1)[0].strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "docs/wecom_archive_worker_runbook.md" not in entries
    assert "docs/operations" not in entries


def test_public_company_registration_labels_are_not_blanket_blocked(snapshot_repo):
    # The approved public marketing pages disclose the company's public
    # registration records; generic labels alone are not a leak signal.
    public_records = "粤ICP备12345678号\n粤公网安备12345678901234号\n"
    result, content = export(snapshot_repo, public_records)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Leak gate passed" in result.stdout
    assert content == public_records


def test_export_still_fails_closed_on_unsanitized_internal_reference(snapshot_repo):
    result, content = export(snapshot_repo, "https://linear.app/synthetic-team\n")
    assert result.returncode != 0
    assert "LEAK GATE FAILED" in result.stdout
    assert "Snapshot ready:" not in result.stdout
    assert content == "https://linear.app/synthetic-team\n"


def test_export_still_fails_closed_on_private_key_body(snapshot_repo):
    private_key_body = "A" * 256
    content = (
        "-----BEGIN PRIVATE KEY-----\n"
        f"{private_key_body}\n"
        "-----END PRIVATE KEY-----\n"
    )
    result, exported = export(snapshot_repo, content)
    assert result.returncode != 0
    assert "real private key body" in result.stdout
    assert "Snapshot ready:" not in result.stdout
    assert exported == content
