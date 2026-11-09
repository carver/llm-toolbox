"""lib/commitcheck.py's holds_check: whether a path in a commit's tree leads to an
executable file there, links followed as the file system would follow them once the
commit is checked out. wt-ship's tests cover what a landing does with the answer."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
import commitcheck

# A file name that isn't UTF-8, as the file system reads it.
LATIN = os.fsdecode(b"caf\xe9")
# Links: each path in the commit, and the target it names.
LINKS = {
    "hooks/latin": f"../bin/{LATIN}",
    "hooks/direct": "../bin/check",
    "hooks/chain": "direct",
    "hooks/via-dir-link": "../tools/check",
    "hooks/up-from-dir-link": "../up/../check",
    # At the top, where /bin/check read as relative, or from the top, is bin/check.
    "absolute": "/bin/check",
    "hooks/above-the-top": "../../bin/check",
    "hooks/to-plain": "../bin/plain",
    "hooks/to-dir": "../bin",
    "hooks/missing": "../bin/gone",
    "hooks/through-a-file": "../bin/check/x",
    "hooks/loop-a": "loop-b",
    "hooks/loop-b": "loop-a",
    "tools": "bin",
    "up": "nested/inner",
    # A chain of 41 links: chain/l0 -> l1 -> ... -> l40 -> ../bin/check.
    **{f"chain/l{n}": f"l{n + 1}" for n in range(40)},
    "chain/l40": "../bin/check",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A repo and the SHA of its one commit, which holds LINKS and the files they
    name. Then every file but .git leaves the work tree, so only the commit holds
    them; and an executable sits just above the repo, where the link above-the-top
    leads, so reading the machine's files instead of the commit would find it."""
    folder = tmp_path_factory.mktemp("tree")
    write(folder / "bin" / "check", "#!/bin/sh\n", 0o755)
    repo = folder / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    # ls-tree prints such names as they are, not quoted.
    git(repo, "config", "core.quotePath", "false")
    for path, mode in [
        ("bin/check", 0o755),
        (f"bin/{LATIN}", 0o755),
        ("bin/plain", 0o644),
        ("nested/check", 0o755),
        ("nested/inner/file", 0o644),
        ("hooks/real", 0o755),
    ]:
        write(repo / path, "#!/bin/sh\n", mode)
    for path, target in LINKS.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).symlink_to(target)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "The tree")
    for entry in repo.iterdir():
        if entry.name != ".git":
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("path", "holds"),
    [
        ("hooks/real", True),
        ("hooks/direct", True),
        # A link to a link that leads to the file.
        ("hooks/chain", True),
        # A target whose name isn't UTF-8 reaches git byte for byte.
        ("hooks/latin", True),
        # A directory link on the way is followed too.
        ("hooks/via-dir-link", True),
        # `..` after a directory link climbs from where the link leads, nested/inner,
        # to nested, as the kernel does: nested/check, not a top-level check.
        ("hooks/up-from-dir-link", True),
        # Out of the tree, though the tree has bin/check: an absolute target isn't
        # read from the top, and a climb above the top doesn't stop there. The
        # machine has an executable where above-the-top leads, too.
        ("absolute", False),
        ("hooks/above-the-top", False),
        ("hooks/to-plain", False),
        ("hooks/to-dir", False),
        ("hooks/missing", False),
        ("hooks/through-a-file", False),
        ("hooks/loop-a", False),
        ("hooks/gone", False),
        ("bin", False),
        # 40 links, Linux's limit, then the file.
        ("chain/l1", True),
        # 41 links: one past it.
        ("chain/l0", False),
    ],
)
def test_holds_check_follows_links_within_the_commit(tree, monkeypatch, path, holds):
    repo, commit = tree
    monkeypatch.chdir(repo)

    assert commitcheck.holds_check(commit, path) == holds


def test_holds_check_fails_on_a_commit_git_cant_read(tree, monkeypatch):
    repo, _ = tree
    monkeypatch.chdir(repo)

    with pytest.raises(commitcheck.GitError):
        commitcheck.holds_check("0" * 40, "hooks/real")


def test_holds_check_reads_paths_from_the_top_in_a_subdirectory(tree, monkeypatch):
    repo, commit = tree
    (repo / "sub").mkdir(exist_ok=True)
    monkeypatch.chdir(repo / "sub")

    assert commitcheck.holds_check(commit, "hooks/direct")
