"""lib/maintree.py: the directory each part of a Bash command runs in, what git says a
directory is, and paperkit's copy of the main-tree test agreeing with it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import without_repo_vars

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import maintree
from paperkit.git import Repo

CWD = Path("/w/r")


def git_dirs(command, cwd=CWD):
    return [
        None if call.directory is None else str(call.directory)
        for call in maintree.git_calls(command, cwd)
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", ["/w/r"]),
        ("cd ../r-wt-x && git commit", ["/w/r-wt-x"]),
        ("cd ../r-wt-x; git commit", ["/w/r-wt-x"]),
        ("cd /abs\ngit commit", ["/abs"]),
        ("(cd ../r-wt-x && git commit) && git log", ["/w/r-wt-x", "/w/r"]),
        ("( (cd a; git x); git y ); git z", ["/w/r/a", "/w/r", "/w/r"]),
        ("{ cd a; }; git x", ["/w/r/a"]),
        ("pushd ../w && git commit; popd; git log", ["/w/w", None]),
        ("cd - && git commit", [None]),
        ("cd && git commit", [None]),
        ('cd "$WT" && git commit', [None]),
        ("cd ~/x && git commit", [None]),
        ("cd $(pwd)/x && git commit", [None]),
        ("cd a* && git commit", [None]),
        ("cd -P .. && git commit", ["/w"]),
        ("cd -- ../x && git commit", ["/w/x"]),
        ("git -C ../a -C b commit", ["/w/a/b"]),
        ("git -C $D commit", [None]),
        ("git --git-dir=.git commit", [None]),
        ("git --work-tree /x commit", [None]),
        ("GIT_DIR=x git commit", [None]),
        ("GIT_WORK_TREE=x; git commit", [None]),
        ("export GIT_DIR=/a; git commit", [None]),
        ("env -C /x git commit", [None]),
        ("env --chdir=/x git commit", [None]),
        ("cd a | cat; git commit", ["/w/r"]),
        ("cd a & git commit", ["/w/r"]),
        ("cd a && git x & git y", ["/w/r/a", "/w/r"]),
        ("cd /wt && sleep 9 & p=$!; git reset --hard", ["/w/r"]),
        ("cd a || exit & git y", ["/w/r"]),
        ("cd a && git x; git y", ["/w/r/a", "/w/r/a"]),
        ("bgrun --wait -o log -C ../w -- git commit", ["/w/w"]),
        ("bgrun -o log --cwd=../w -- git commit", ["/w/w"]),
        ("heavy --few-seconds -- git commit", ["/w/r"]),
        ("timeout 60 nice git commit", ["/w/r"]),
        ("bash -c 'cd ../w && git commit'; git log", ["/w/w", "/w/r"]),
        ("echo $(git rev-parse HEAD)", [None]),
        (". ./env.sh; git commit", [None]),
        ("source x && git commit", [None]),
        ("f() { cd /main; }; git commit", [None]),
        ("function f { cd /x; }; git commit", [None]),
        ("git commit -m 'f() is gone'", ["/w/r"]),
        ("git commit -F - <<'EOF'\ncd /x\ngit push\nEOF\ngit log", ["/w/r", "/w/r"]),
    ],
)
def test_each_git_call_gets_the_directory_it_runs_in(command, expected):
    assert git_dirs(command) == expected


@pytest.mark.parametrize(
    ("command", "directory", "guess"),
    [
        ("echo m | (cd ../w && git commit)", None, "/w/w"),
        ("{ cd ../w && git commit; } | tail", None, "/w/w"),
        ("(cd ../w && git x) | tail; git commit", "/w/r", "/w/r"),
        ("source x && git commit", None, "/w/r"),
        ("f() { :; }; cd ../w && git commit", None, "/w/w"),
        ("GIT_DIR=.git git -C ../w commit", None, "/w/w"),
        ("git --git-dir=.git commit", None, "/w/r"),
        ("env -C /x git commit", None, "/w/r"),
        ('cd "$WT" && git commit', None, None),
    ],
)
def test_the_guess_follows_plain_cds_where_the_directory_is_unsure(
    command, directory, guess
):
    call = maintree.git_calls(command, CWD)[-1]
    as_text = [None if d is None else str(d) for d in (call.directory, call.guess)]
    assert as_text == [directory, guess]


def test_with_no_cwd_nothing_is_certain():
    assert git_dirs("git commit; cd /abs && git log", None) == [None, "/abs"]


def test_git_calls_give_the_subcommand_args_and_prefix_settings():
    [call] = maintree.git_calls("A=1 B=x bgrun -o l -- C=2 git -c x=y commit -m m", CWD)
    assert (call.subcommand, call.args) == ("commit", ["-m", "m"])
    assert call.step.assigned == {"A": "1", "B": "x", "C": "2"}


@pytest.mark.parametrize(
    ("command", "loose"),
    [
        ("cd /x && git reset --hard", None),
        ("cd /x; git reset --hard", ["cd", "/x"]),
        ("cd /x\ngit reset --hard", ["cd", "/x"]),
        ("cd /x; cd y && git reset --hard", ["cd", "/x"]),
        ("(cd /x; true); git reset --hard", None),
    ],
)
def test_a_step_names_an_earlier_cd_whose_failure_would_not_stop_it(command, loose):
    assert maintree.git_calls(command, CWD)[-1].step.loose_cd == loose


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    )


def make_repo(path, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@t")
    if commit:
        git(path, "commit", "-q", "--allow-empty", "-m", "a")
    return path


@pytest.fixture
def layouts(tmp_path):
    """{name: directory} for each layout the workspace has."""
    main = make_repo(tmp_path / "r")
    (main / "lib").mkdir()
    git(main, "worktree", "add", "-q", "--detach", str(tmp_path / "r-wt-x"))
    inner = make_repo(tmp_path / "inner")
    subprocess.run(
        ["git", "-C", str(main), "-c", "protocol.file.allow=always"]
        + ["submodule", "add", "-q", str(inner), "sub"],
        check=True,
        capture_output=True,
        env=without_repo_vars(os.environ),
    )
    return {
        "main": main,
        "subfolder": main / "lib",
        "worktree": tmp_path / "r-wt-x",
        "submodule": main / "sub",
    }


@pytest.mark.parametrize(
    ("name", "main", "exempt"),
    [
        ("main", True, None),
        ("subfolder", True, None),
        ("worktree", False, None),
        ("submodule", True, "a submodule"),
    ],
)
def test_place_tells_a_main_tree_from_a_worktree(layouts, name, main, exempt):
    where = maintree.place(layouts[name])
    assert (where.main, where.exempt) == (main, exempt)
    assert where.top == layouts[name if name != "subfolder" else "main"]


def test_paperkits_copy_of_the_main_tree_test_agrees(layouts):
    for directory in layouts.values():
        repo = Repo.of(directory)
        assert repo.is_main_worktree() == maintree.place(directory).main, directory


def test_place_names_the_branch_or_none_when_detached(layouts):
    assert maintree.place(layouts["main"]).branch == "main"
    assert maintree.place(layouts["worktree"]).branch is None


def test_an_unborn_head_is_exempt(tmp_path):
    assert maintree.place(make_repo(tmp_path / "r", commit=False)).exempt == (
        "an unborn HEAD"
    )


@pytest.mark.parametrize(
    ("marker", "what"),
    [
        ("MERGE_HEAD", "a merge in progress"),
        ("CHERRY_PICK_HEAD", "a cherry-pick in progress"),
        ("REVERT_HEAD", "a revert in progress"),
        ("rebase-merge", "a rebase in progress"),
        ("rebase-apply", "a rebase in progress"),
        ("sequencer", "a cherry-pick or revert of several commits in progress"),
    ],
)
def test_an_operation_in_progress_is_exempt(tmp_path, marker, what):
    repo = make_repo(tmp_path / "r")
    (repo / ".git" / marker).mkdir()
    assert maintree.place(repo).exempt == what


def test_a_bare_repo_is_exempt(tmp_path):
    bare = tmp_path / "b.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    assert maintree.place(bare).exempt == "a bare repo"


@pytest.mark.parametrize("where", ["missing", "plain", "dotgit", "none"])
def test_place_is_none_where_git_cant_answer(tmp_path, where):
    repo = make_repo(tmp_path / "r")
    directory = {
        "missing": tmp_path / "missing",
        "plain": tmp_path,
        "dotgit": repo / ".git",
        "none": None,
    }[where]
    # tmp_path sits in no repo: conftest strips GIT_* and pytest's tmp root is outside.
    assert maintree.place(directory) is None


def test_place_ignores_a_git_dir_the_environment_names(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "r")
    other = make_repo(tmp_path / "o")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert maintree.place(repo).top == repo
