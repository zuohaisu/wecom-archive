"""GH-155: exporter regression tests, intentionally absent from public snapshots."""

from pathlib import Path
import importlib.util
import os
import shutil
import subprocess

import pytest


EXPORTER = Path(__file__).resolve().parents[2] / "scripts/export_public_snapshot.sh"
ALLOWLIST = EXPORTER.parent / "public_allowlist.txt"
WIKI_PUBLISHER = EXPORTER.parent / "publish_public_wiki.py"
WIKI_ALLOWLIST = EXPORTER.parent / "public_wiki_allowlist.txt"


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


def test_renamed_repository_has_explicit_mapping_and_leak_gate():
    exporter = EXPORTER.read_text()
    assert "'s|zuohaisu/wecom-archive-365|your-org/wecom-archive|g'" in exporter
    assert "'s|zuohaisu/wecom-archive|your-org/wecom-archive|g'" in exporter
    assert "'s|wecom-archive-365|wecom-archive|g'" in exporter
    assert "name '*wecom-archive-365*' -print" in exporter
    gate_patterns = exporter.split("declare -a GATE_PATTERNS=(", 1)[1].split(")", 1)[0]
    assert "'wecom-archive-365'" in gate_patterns


def test_export_uses_unbranded_placeholders_and_preserves_vendor(snapshot_repo):
    result, content = export(
        snapshot_repo,
        "git@github.com:zuohaisu/wecom-archive.git\n"
        "/srv/apps/wecom-archive-365/current\n"
        "/srv/apps/wecom-archive-365-nonprod/current\n"
        "wecom-archive-365.service\n"
        "archive.crowntime.cn qwhhcd.crowntime.cn\n"
        "Crowntime WeCom Archive https://crowntime.cn\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Leak gate passed" in result.stdout
    assert "wecom-archive-365" not in content
    assert "zuohaisu" not in content
    assert content == (
        "git@github.com:your-org/wecom-archive.git\n"
        "/srv/apps/wecom-archive/current\n"
        "/srv/apps/wecom-archive-nonprod/current\n"
        "wecom-archive.service\n"
        "archive.crowntime.cn archive.example.com\n"
        "Crowntime WeCom Archive https://crowntime.cn\n"
    )


def test_export_normalizes_legacy_repository_names_in_snapshot_paths(snapshot_repo):
    repo, output = snapshot_repo
    old_file = repo / "deploy" / "systemd" / "wecom-archive-365-nonprod.service"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("Description=wecom-archive-365 non-production service\n")
    (repo / "scripts" / "public_allowlist.txt").write_text("deploy/systemd\n")
    subprocess.run(["git", "add", str(old_file)], cwd=repo, check=True)

    result = subprocess.run(
        ["bash", str(repo / "scripts" / EXPORTER.name), str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
    )

    renamed_file = output / "deploy" / "systemd" / "wecom-archive-nonprod.service"
    assert result.returncode == 0, result.stdout + result.stderr
    assert renamed_file.is_file()
    assert not (output / "deploy" / "systemd" / old_file.name).exists()
    assert "wecom-archive-365" not in renamed_file.read_text()
    assert not any("wecom-archive-365" in path.name for path in output.rglob("*"))


def test_export_fails_closed_if_legacy_repository_name_remains_in_path(snapshot_repo):
    repo, output = snapshot_repo
    old_file = repo / "deploy" / "systemd" / "wecom-archive-365-nonprod.service"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("Description=generic service\n")
    (repo / "scripts" / "public_allowlist.txt").write_text("deploy/systemd\n")
    subprocess.run(["git", "add", str(old_file)], cwd=repo, check=True)

    exporter = repo / "scripts" / EXPORTER.name
    script = exporter.read_text()
    path_rule = '\t\tsnapshot_path="${file//wecom-archive-365/wecom-archive}"\n'
    assert path_rule in script
    exporter.write_text(script.replace(path_rule, '\t\tsnapshot_path="${file}"\n'))

    result = subprocess.run(
        ["bash", str(exporter), str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "LEAK GATE FAILED — legacy repository name in snapshot path:" in result.stdout
    assert "Snapshot ready:" not in result.stdout
    assert (output / "deploy" / "systemd" / old_file.name).is_file()


def test_export_fails_closed_if_old_repository_name_is_not_sanitized(snapshot_repo):
    repo, _ = snapshot_repo
    exporter = repo / "scripts" / EXPORTER.name
    script = exporter.read_text()
    sanitizer_rule = "\t's|wecom-archive-365|wecom-archive|g'\n"
    assert sanitizer_rule in script
    exporter.write_text(script.replace(sanitizer_rule, ""))

    result, content = export(snapshot_repo, "wecom-archive-365.service\n")

    assert result.returncode != 0
    assert "LEAK GATE FAILED — pattern: wecom-archive-365" in result.stdout
    assert "Snapshot ready:" not in result.stdout
    assert content == "wecom-archive-365.service\n"


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


def test_public_snapshot_includes_product_docs_and_generic_checks_without_company_site():
    entries = {
        line.split("#", 1)[0].strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "docs/kb/public" in entries
    assert "docs/kb/customer" in entries
    # GH-208: website ownership moved to its independent private repository.
    assert not any(entry.startswith("static_site/") for entry in entries)
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


def test_wiki_allowlist_excludes_runtime_and_private_material():
    entries = {
        line.strip()
        for line in WIKI_ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "README.md" in entries
    assert "CONTRIBUTING.md" in entries
    assert "docs/kb/customer/faq.md" in entries
    assert not any(path.startswith(("docs/adr/", "docs/operations/", "docs/research/")) for path in entries)
    assert all(path.endswith(".md") and (EXPORTER.parents[1] / path).is_file() for path in entries)


def test_wiki_export_fails_closed_on_unreviewed_company_subdomain(snapshot_repo):
    repo, output = snapshot_repo
    (repo / "scripts/public_wiki_allowlist.txt").write_text("README.md\n")
    (repo / "README.md").write_text("https://private-test.crowntime.cn/internal\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    result = subprocess.run(
        ["bash", str(repo / "scripts" / EXPORTER.name), "--wiki", str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "unreviewed crowntime.cn subdomain" in result.stdout
    assert "Snapshot ready:" not in result.stdout


def test_wiki_render_preserves_existing_story_and_is_idempotent(tmp_path):
    spec = importlib.util.spec_from_file_location("publish_public_wiki", WIKI_PUBLISHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    staged = tmp_path / "staged"
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for path in WIKI_ALLOWLIST.read_text().splitlines():
        if not path or path.startswith("#"):
            continue
        destination = staged / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXPORTER.parents[1] / path, destination)
    home = "# Existing Home\n\n[产品演化史](Product-Evolution)\n"
    story = "# Original story\n\nDo not replace me.\n"
    (wiki / "Home.md").write_text(home)
    (wiki / "Product-Evolution.md").write_text(story)
    module.render(staged, wiki)
    first = {path.name: path.read_bytes() for path in wiki.iterdir()}
    module.render(staged, wiki)
    assert first == {path.name: path.read_bytes() for path in wiki.iterdir()}
    assert (wiki / "Product-Evolution.md").read_text() == story
    assert (wiki / "Home.md").read_text().startswith(home)
    assert "项目来时路" in (wiki / "_Sidebar.md").read_text()
    assert "docker compose up -d --build" in (wiki / "Installation.md").read_text()
    assert "docs/adr" in (wiki / "Architecture-Overview.md").read_text()
    assert not (wiki / "ADR-0008.md").exists()


def test_wiki_publish_to_local_remote_is_idempotent(tmp_path):
    bare = tmp_path / "wiki.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=master", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=master", str(seed)], check=True)
    (seed / "Home.md").write_text("# Existing Home\n\n[产品演化史](Product-Evolution)\n")
    (seed / "Product-Evolution.md").write_text("# Story\n\nOriginal story text.\n")
    subprocess.run(["git", "add", "Home.md", "Product-Evolution.md"], cwd=seed, check=True)
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Wiki Test",
        GIT_AUTHOR_EMAIL="wiki-test@example.invalid",
        GIT_COMMITTER_NAME="Wiki Test",
        GIT_COMMITTER_EMAIL="wiki-test@example.invalid",
    )
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=seed, env=env, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=seed, check=True)

    command = ["python3", str(WIKI_PUBLISHER), "--remote", str(bare)]
    first = subprocess.run(command, text=True, capture_output=True, env=env)
    second = subprocess.run(command, text=True, capture_output=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already up to date" in second.stdout
    count = subprocess.check_output(["git", "--git-dir", str(bare), "rev-list", "--count", "master"], text=True)
    assert count.strip() == "2"
    story = subprocess.check_output(["git", "--git-dir", str(bare), "show", "master:Product-Evolution.md"], text=True)
    assert story == "# Story\n\nOriginal story text.\n"
