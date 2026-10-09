#!/usr/bin/env python3
"""Publish a reviewed subset of repository docs to the separate GitHub Wiki."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/zuohaisu/wecom-archive"
WIKI_URL = f"{REPO_URL}/wiki"
REMOTE = "git@github.com:zuohaisu/wecom-archive.wiki.git"
PAGES = {
    "CONTRIBUTING.md": "Developer-Guide.md",
    "docs/ARCHITECTURE.md": "Architecture-Overview.md",
    "docs/kb/customer/configuration.md": "Configuration.md",
    "docs/kb/customer/faq.md": "FAQ.md",
}
SOURCES = {"README.md", "docs/kb/customer/self-hosting-guide.md", *PAGES}
MANAGED = ("Home.md", "_Sidebar.md", "Installation.md", *PAGES.values())
START = "<!-- public-wiki:start -->"
END = "<!-- public-wiki:end -->"
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def rewrite_links(content: str, source: str) -> str:
    """Make repository-relative links work from the separate Wiki repository."""

    def replace(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith(("#", "https://", "http://", "mailto:")):
            return match.group(0)
        path, separator, anchor = target.partition("#")
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if normalized.startswith("../") or normalized == "..":
            raise ValueError(f"link escapes repository in {source}: {target}")
        if normalized in PAGES:
            url = f"{WIKI_URL}/{PAGES[normalized][:-3]}"
        else:
            kind = "blob" if Path(ROOT / normalized).is_file() else "tree"
            url = f"{REPO_URL}/{kind}/main/{quote(normalized, safe='/')}"
        if separator:
            url += f"#{anchor}"
        return f"[{label}]({url})"

    return LINK.sub(replace, content)


def source(staged: Path, path: str) -> str:
    content = (staged / path).read_text(encoding="utf-8")
    return rewrite_links(content, path)


def managed_block(existing: str, block: str) -> str:
    replacement = f"{START}\n{block.strip()}\n{END}"
    if START in existing or END in existing:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise ValueError("Wiki navigation has malformed managed markers")
        return re.sub(
            re.escape(START) + r".*?" + re.escape(END),
            replacement,
            existing,
            flags=re.DOTALL,
        )
    prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
    return prefix + replacement + "\n"


def render(staged: Path, wiki: Path) -> None:
    found = {path.relative_to(staged).as_posix() for path in staged.rglob("*.md")}
    if found != SOURCES:
        raise ValueError(f"Wiki source set differs from the reviewed allowlist: {sorted(found ^ SOURCES)}")
    readme = source(staged, "README.md")
    start = readme.find("### Five-minute self-host quickstart")
    end = readme.find("### Prerequisites", start)
    if start < 0 or end < 0:
        raise ValueError("README Compose quickstart boundaries not found")
    quickstart = readme[start:end].replace("### Five-minute", "## Five-minute", 1).strip()
    selfhost = source(staged, "docs/kb/customer/self-hosting-guide.md")
    selfhost = re.sub(r"^# [^\n]+", "## 自托管版本与升级", selfhost, count=1)
    (wiki / "Installation.md").write_text(
        "# 安装与升级\n\n" + quickstart + "\n\n" + selfhost.rstrip() + "\n",
        encoding="utf-8",
    )
    for original, page in PAGES.items():
        (wiki / page).write_text(source(staged, original).rstrip() + "\n", encoding="utf-8")

    home = wiki / "Home.md"
    if not home.exists():
        raise ValueError("Existing Wiki Home.md is required to preserve project history")
    home_block = """## 使用与开发文档

- [安装与升级](Installation)
- [配置说明](Configuration)
- [常见问题](FAQ)
- [架构概览](Architecture-Overview)
- [开发者指南](Developer-Guide)
- [康冠时代云托管版](https://www.crowntime.cn/)

以上页面镜像仓库当前文档；[README 快速开始](https://github.com/zuohaisu/wecom-archive#quick-start)、[版本发布](https://github.com/zuohaisu/wecom-archive/releases) 和仓库中的 ADR 仍以源码仓库为准。
"""
    home.write_text(managed_block(home.read_text(encoding="utf-8"), home_block), encoding="utf-8")

    sidebar = wiki / "_Sidebar.md"
    sidebar_block = """**使用文档**

- [安装与升级](Installation)
- [配置说明](Configuration)
- [常见问题](FAQ)

**开发者文档**

- [架构概览](Architecture-Overview)
- [开发者指南](Developer-Guide)
- [源码 README](https://github.com/zuohaisu/wecom-archive#readme)
- [架构决策记录](https://github.com/zuohaisu/wecom-archive/tree/main/docs/adr)

**项目来时路**

- [产品演化史](Product-Evolution)
- [第一个独立中型软件项目](康冠时代会话存档：我的第一个独立中型软件项目)
"""
    existing = sidebar.read_text(encoding="utf-8") if sidebar.exists() else ""
    sidebar.write_text(managed_block(existing, sidebar_block), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default=os.environ.get("PUBLIC_WIKI_REMOTE", REMOTE))
    parser.add_argument("--preview-dir", type=Path, help="write generated pages here without publishing")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="public-wiki-") as temp:
        temporary = Path(temp)
        staged = temporary / "sources"
        run("bash", str(ROOT / "scripts/export_public_snapshot.sh"), "--wiki", str(staged))
        if args.preview_dir:
            if args.preview_dir.exists():
                raise ValueError(f"preview directory already exists: {args.preview_dir}")
            wiki = args.preview_dir
            wiki.mkdir(parents=True)
            run("git", "clone", "--depth", "1", args.remote, str(temporary / "existing"))
            for path in (temporary / "existing").iterdir():
                if path.is_file() and path.name != ".git":
                    shutil.copy2(path, wiki / path.name)
            render(staged, wiki)
            print(f"Wiki preview ready: {wiki}")
            return

        wiki = temporary / "wiki"
        run("git", "clone", "--depth", "1", args.remote, str(wiki))
        branch = run("git", "branch", "--show-current", cwd=wiki)
        if not branch:
            raise ValueError("Wiki default branch could not be determined")
        render(staged, wiki)
        run("git", "add", "--", *MANAGED, cwd=wiki)
        changed = run("git", "diff", "--cached", "--name-only", cwd=wiki).splitlines()
        if not changed:
            print("public-wiki: already up to date")
            return
        if not set(changed) <= set(MANAGED):
            raise ValueError(f"Unexpected staged Wiki paths: {changed}")
        run("git", "diff", "--cached", "--check", cwd=wiki)
        print("Wiki pages changed: " + ", ".join(changed))
        run("git", "commit", "-m", "docs(wiki): publish public documentation (GH-175)", cwd=wiki)
        run("git", "push", "origin", f"HEAD:{branch}", cwd=wiki)
        print(f"public-wiki: published {run('git', 'rev-parse', 'HEAD', cwd=wiki)}")


if __name__ == "__main__":
    main()
