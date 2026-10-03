"""Helpers for the tests of lib/thirdparty.py and the tools built on it."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import thirdparty as tp

from conftest import without_repo_vars


def make_skill(
    root: Path, rel: str, body: str = "do the thing\n", desc: str = "Does a thing."
) -> Path:
    path = root / rel
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {path.name}\ndescription: {desc}\n---\n{body}"
    )
    return path


def commit(repo: Path) -> None:
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    env = without_repo_vars(os.environ)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run([*git, "add", "-A"], check=True, env=env)
    subprocess.run([*git, "commit", "-q", "-m", "c"], check=True, env=env)


def lock_for(clone: Path, rel: str) -> dict:
    return {"skillFolderHash": tp.tree_hash(clone, clone / rel)}


def isolate(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point thirdparty at temp folders; returns (installed, upstream clone)."""
    installed, clone = tmp_path / "installed", tmp_path / "clone"
    installed.mkdir()
    monkeypatch.setattr(tp, "INSTALLED", installed)
    monkeypatch.setattr(tp, "PATCHES", tmp_path / "skill-patches")
    monkeypatch.setattr(tp, "LOCK", tmp_path / "lock.json")
    monkeypatch.setattr(tp, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    return installed, clone
