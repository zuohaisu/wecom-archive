"""GH-155: exporter regression tests, intentionally absent from public snapshots."""

from pathlib import Path
import shutil
import subprocess

import pytest


EXPORTER = Path(__file__).resolve().parents[2] / "scripts/export_public_snapshot.sh"


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
        "archive.example.com archive.example.com\n"
        "Crowntime WeCom Archive https://crowntime.cn\n"
    )


def test_export_still_fails_closed_on_unsanitized_internal_reference(snapshot_repo):
    result, content = export(snapshot_repo, "https://linear.app/synthetic-team\n")
    assert result.returncode != 0
    assert "LEAK GATE FAILED" in result.stdout
    assert "Snapshot ready:" not in result.stdout
    assert content == "https://linear.app/synthetic-team\n"