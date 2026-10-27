"""wt-ship, against a bare remote, a main tree and a detached worktree beside it."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "wt-ship"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(cwd, path, text, message):
    with open(Path(cwd) / path, "a") as file:
        file.write(text)
    git(cwd, "add", path)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def ship(cwd, *args):
    return subprocess.run(
        [str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def configure(cwd):
    git(cwd, "config", "user.name", "t")
    git(cwd, "config", "user.email", "t@example.com")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A bare origin, a clone that pushed one commit with a worktree beside it, and a second clone, made
    once: each test copies them."""
    folder = tmp_path_factory.mktemp("template")
    origin = folder / "origin.git"
    git(folder, "init", "-q", "--bare", "-b", "main", str(origin))
    main = folder / "repo"
    git(folder, "clone", "-q", str(origin), str(main))
    configure(main)
    commit(main, "README", "start\n", "start")
    git(main, "push", "-q", "origin", "main")
    git(main, "worktree", "add", "-q", "--detach", str(folder / "repo-wt-topic"))
    other = folder / "other"
    git(folder, "clone", "-q", str(origin), str(other))
    configure(other)
    return folder


@pytest.fixture
def setup(template, tmp_path):
    shutil.copytree(template, tmp_path, symlinks=True, dirs_exist_ok=True)
    origin, main, wt, other = (
        tmp_path / name for name in ("origin.git", "repo", "repo-wt-topic", "other")
    )
    for clone in (main, other):
        git(clone, "remote", "set-url", "origin", str(origin))
    # The copies still name the template's paths
    git(main, "worktree", "repair", str(wt))
    return origin, main, wt, other


def remote_main(origin):
    return git(origin, "rev-parse", "main")


def test_it_pushes_the_worktrees_commits_and_prints_a_notice(setup):
    origin, _, wt, _ = setup
    old = remote_main(origin)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "true")

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new
    assert f"{old[:7]}..{new[:7]}" in done.stdout
    assert "Add a" in done.stdout and "a.py" in done.stdout
    assert "behind origin/main" in done.stdout  # the main tree hasn't pulled
    assert wt.exists()


def test_it_rebases_onto_another_sessions_push_first(setup):
    origin, _, wt, other = setup
    theirs = commit(other, "b.py", "y = 2\n", "Add b")
    git(other, "push", "-q", "origin", "main")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "true")

    assert done.returncode == 0, done.stderr
    assert git(origin, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]
    assert git(origin, "rev-parse", "main~1") == theirs


def test_a_rebase_conflict_stops_with_the_rebase_in_progress(setup):
    origin, _, wt, other = setup
    commit(other, "README", "theirs\n", "Theirs")
    git(other, "push", "-q", "origin", "main")
    before = remote_main(origin)
    commit(wt, "README", "ours\n", "Ours")

    done = ship(wt, "--check", "true")

    assert done.returncode == 1
    assert "conflicts" in done.stderr and "rebase --continue" in done.stderr
    assert remote_main(origin) == before
    assert (Path(git(wt, "rev-parse", "--absolute-git-dir")) / "rebase-merge").exists()


def test_leftover_conflict_markers_are_refused(setup):
    origin, _, wt, _ = setup
    before = remote_main(origin)
    commit(wt, "a.py", "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> abc\n", "Oops")

    done = ship(wt, "--check", "true")

    assert done.returncode == 1 and "conflict marker" in done.stderr
    assert remote_main(origin) == before


def test_a_failing_check_stops_the_push(setup):
    origin, _, wt, _ = setup
    before = remote_main(origin)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "echo broken; exit 3")

    assert done.returncode == 1 and "broken" in done.stdout + done.stderr
    assert remote_main(origin) == before


def test_the_default_check_is_the_repos_agent_pre_commit(setup):
    origin, main, wt, _ = setup
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    (hooks / "agent-pre-commit").write_text("#!/bin/sh\necho hook ran; exit 1\n")
    (hooks / "agent-pre-commit").chmod(0o755)
    before = remote_main(origin)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt)

    assert done.returncode == 1 and "hook ran" in done.stdout + done.stderr
    assert remote_main(origin) == before


def test_a_push_that_loses_a_race_rebases_and_retries_once(setup):
    origin, main, wt, other = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    # Another session pushes between wt-ship's fetch and its push.
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    flag = hooks.parent / "raced"
    (hooks / "pre-push").write_text(
        f'#!/bin/sh\n[ -e "{flag}" ] && exit 0\ntouch "{flag}"\n'
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX\n"
        f'cd "{other}" && echo z >> c.py && git add c.py && git commit -qm Race'
        " && git push -q origin main\n"
    )
    (hooks / "pre-push").chmod(0o755)

    done = ship(wt, "--check", "true")

    assert done.returncode == 0, done.stderr
    assert git(origin, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Race",
        "start",
    ]
    notice = done.stdout.split("Notice for other sessions:")[1]
    assert "Add a" in notice and "Race" not in notice and "c.py" not in notice


def test_uncommitted_changes_are_refused(setup):
    _, _, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    (wt / "a.py").write_text("changed\n")

    done = ship(wt, "--check", "true")

    assert done.returncode == 1 and "uncommitted" in done.stderr


def test_nothing_to_push_says_so(setup):
    _, _, wt, _ = setup

    done = ship(wt, "--check", "true")

    assert done.returncode == 0 and "nothing to push" in done.stdout


def test_remove_removes_the_worktree_after_the_push(setup):
    origin, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "true", "--remove")

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new and not wt.exists()
    assert str(wt) not in git(main, "worktree", "list")


def test_remove_is_refused_in_the_main_tree(setup):
    _, main, _, _ = setup
    commit(main, "a.py", "x = 1\n", "Add a")

    done = ship(main, "--check", "true", "--remove")

    assert done.returncode == 1 and "main tree" in done.stderr


def test_a_push_refused_for_another_reason_isnt_retried(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    count = hooks.parent / "pushes"
    (hooks / "pre-push").write_text(f'#!/bin/sh\necho x >> "{count}"\nexit 1\n')
    (hooks / "pre-push").chmod(0o755)

    done = ship(wt, "--check", "true")

    assert done.returncode == 1
    assert count.read_text().count("x") == 1
