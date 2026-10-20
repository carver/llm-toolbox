"""bin/build-state, run against a throwaway repo."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "build-state"


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.com")
    commit(tmp_path, "README", "start")
    return tmp_path


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def commit(repo, path, message, *flags):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    with open(repo / path, "a") as file:
        file.write(f"{message}\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message, *flags)
    return git(repo, "rev-parse", "HEAD").strip()


def build_state(repo, *args):
    return subprocess.run(
        [str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, check=False
    )


def subjects(repo, since):
    return git(repo, "log", "--format=%s", "--reverse", f"{since}..HEAD").splitlines()


def test_finish_folds_each_fixup_into_its_own_commit_and_keeps_the_rest(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    assert build_state(repo, "start").returncode == 0
    parser = commit(repo, "parser.py", "Parse dates")
    commit(repo, "cli.py", "Add --since")
    commit(repo, "parser.py", "fix parser", f"--fixup={parser}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["Parse dates", "Add --since"]
    assert git(repo, "show", "--format=", "--name-only", "HEAD~1").split() == [
        "parser.py"
    ]
    assert "Parse dates" in done.stdout and "Add --since" in done.stdout


def test_finish_fails_and_names_what_changed_when_another_ref_or_the_config_moved(repo):
    git(repo, "config", "remote.origin.url", "https://user:s3cret@example.com/r.git")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    before = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "branch", "other")
    git(repo, "config", "remote.origin.url", "https://user:n3w@example.com/r.git")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "refs/heads/other" in done.stderr and "remote.origin.url" in done.stderr
    output = done.stdout + done.stderr
    assert "s3cret" not in output and "n3w" not in output
    assert git(repo, "rev-parse", "HEAD").strip() == before  # nothing folded
    # The state is kept for another look.
    assert build_state(repo, "finish").returncode == 1


def test_start_refuses_while_a_build_is_open_unless_forced(repo):
    build_state(repo, "start")
    first_base = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "a.py", "Work")

    refused = build_state(repo, "start")
    forced = build_state(repo, "start", "--force")

    assert refused.returncode == 1 and first_base in refused.stderr
    assert (
        forced.returncode == 0
        and git(repo, "rev-parse", "HEAD").strip() in forced.stdout
    )


def test_finish_without_start_says_so(repo):
    done = build_state(repo, "finish")

    assert done.returncode == 1 and "build-state start" in done.stderr


def test_finish_lists_the_test_files_changed_since_the_refactor_base(repo):
    build_state(repo, "start")
    commit(repo, "tests/test_parser.py", "Test parsing")
    commit(repo, "test_cli.py", "Test the CLI")
    assert build_state(repo, "refactor").returncode == 0
    commit(repo, "parser.py", "Tidy the parser")
    commit(repo, "tests/test_parser.py", "Tidy the parser's test")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    listed = {
        line.split("|")[0].strip() for line in done.stdout.splitlines() if "|" in line
    }
    assert listed == {"tests/test_parser.py"}


def test_finish_refuses_uncommitted_changes(repo):
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    (repo / "a.py").write_text("half done\n")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "uncommitted" in done.stderr
    assert (repo / "a.py").read_text() == "half done\n"


def test_a_fixup_that_conflicts_leaves_the_branch_as_it_was(repo):
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    commit(repo, "a.py", "Second")
    commit(repo, "a.py", "fix first", f"--fixup={first}")
    before = git(repo, "rev-parse", "HEAD").strip()

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "CONFLICT" in done.stderr
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert git(repo, "status", "--porcelain") == ""


def test_show_prints_the_recorded_bases(repo):
    build_state(repo, "start")
    base = git(repo, "rev-parse", "HEAD").strip()
    refactor_base = commit(repo, "a.py", "Work")
    build_state(repo, "refactor")

    shown = build_state(repo, "show")

    assert shown.returncode == 0
    assert (
        f"base {base}" in shown.stdout
        and f"refactor base {refactor_base}" in shown.stdout
    )


def test_a_squash_commit_folds_without_opening_an_editor(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    commit(repo, "a.py", "more", f"--squash={first}")

    done = subprocess.run(
        [str(SCRIPT), "finish"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"GIT_EDITOR": "false", "EDITOR": "false"},
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["First"]


def test_a_fixup_for_a_commit_before_the_base_fails_finish(repo):
    old = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    commit(repo, "README", "fix start", f"--fixup={old}")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "fixup! start" in done.stderr


def test_the_work_branchs_upstream_may_move(repo):
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")
    git(repo, "config", "remote.origin.url", "/nowhere")
    git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # as a push would

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
