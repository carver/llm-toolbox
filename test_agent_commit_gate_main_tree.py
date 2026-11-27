"""bin/agent-commit-gate as Claude Code's PreToolUse hook: the refusal of a commit in
a main tree, run as a subprocess fed the hook's JSON, against repos in tmp_path."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import saved_command, without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "agent-commit-gate"
NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        env=without_repo_vars(os.environ),
    )


def hook(cwd, command, env=None, **event):
    """The gate's exit status and stderr for one Bash call run from `cwd`."""
    payload = {
        "session_id": "s",
        "transcript_path": "/t.jsonl",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "run_in_background": True},
    } | event
    run = subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=without_repo_vars(os.environ) | (env or {}),
    )
    return run.returncode, run.stderr


def make_repo(path, agent_check=True, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "core.hooksPath", ".githooks")
    (path / ".githooks").mkdir()
    if agent_check:
        check = path / ".githooks" / "agent-pre-commit"
        check.write_text("#!/bin/sh\nexit 0\n")
        check.chmod(0o755)
    (path / "a").write_text("a\n")
    if commit:  # the hooks too, so a worktree has the same agent check
        git(path, "add", "a", ".githooks")
        git(path, "commit", "-q", "-m", "a")
    return path


@pytest.fixture
def main(tmp_path):
    """A main tree with an agent check and a commit, `r`, under tmp_path."""
    return make_repo(tmp_path / "r")


@pytest.fixture
def worktree(main):
    """A linked worktree of `main`, beside it."""
    path = main.parent / "r-wt-x"
    git(main, "worktree", "add", "-q", "--detach", str(path))
    return path


def refused(code, err):
    return code == 2 and "main tree" in err


@pytest.mark.parametrize(
    "command",
    [
        "git commit -q -m x",
        "git add a && git commit -m x",
        "cd lib && git commit -m x",
        "bgrun --wait -o log -- git commit -q -F msg",
        "heavy -- git commit -q -m x",
        "AGENT_MAIN_TREE_OK=0 git commit -m x",
        "(git commit -m x)",
    ],
)
def test_a_commit_in_a_main_tree_with_an_agent_check_is_refused(main, command):
    (main / "lib").mkdir()
    code, err = hook(main, command)
    assert refused(code, err), err


def test_the_refusal_names_the_worktree_and_asks_the_user_first(main):
    code, err = hook(main, "git commit -m x")
    assert code == 2
    assert f"{main} is r's main tree" in err
    assert "git worktree add --detach ../r-wt-<topic>" in err
    assert "wt-ship" in err
    assert "leave them as they are" in err and "tell the user" in err
    assert "ask the user first" in err
    # The opt-out is the user's to give: the refusal doesn't name it.
    assert "AGENT_MAIN_TREE_OK" not in err
    assert "origin/" not in err  # no remote, no fetch to suggest
    assert err.rstrip().endswith(NOTHING_RAN)


def test_a_refused_commit_is_saved_for_the_next_call(main, refused_session):
    fields, folder = refused_session
    command = "git commit -F - <<'MSG'\nSubject\nMSG"
    code, err = hook(main, command, **fields)
    assert code == 2
    assert saved_command(err, folder) == command


def test_in_a_repo_with_an_origin_it_names_the_branch_to_start_from(main):
    git(main, "remote", "add", "origin", "https://example.invalid/r.git")
    git(main, "switch", "-q", "-c", "trunk")
    code, err = hook(main, "git commit -m x")
    assert code == 2
    assert "`git fetch`, then add it at origin/trunk" in err


def new_worktree_script(main):
    script = main / "bin" / "new-worktree"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)


def test_a_repo_with_its_own_worktree_script_hears_of_that(main):
    new_worktree_script(main)
    git(main, "remote", "add", "origin", "https://example.invalid/r.git")
    code, err = hook(main, "git commit -m x")
    assert code == 2
    assert "`bin/new-worktree <topic>`" in err and "git worktree add" not in err
    assert "then add it with --at origin/main" in err


def test_a_detached_main_tree_is_refused_naming_no_branch(main):
    git(main, "remote", "add", "origin", "https://example.invalid/r.git")
    git(main, "switch", "-q", "--detach")
    code, err = hook(main, "git commit -m x")
    assert refused(code, err), err
    assert "origin/" not in err
    assert "the remote branch it lands on" in err


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git -C ../r commit -m x",
        "cd ../r && git commit -m x",
        "cd ../r && echo $(git commit -m x)",
    ],
)
def test_the_directory_it_judges_is_the_commits_own(main, worktree, command):
    # The first runs in the worktree; the others name the main tree from there.
    code, err = hook(worktree, command)
    assert (code == 0) == (command == "git commit -m x"), err


@pytest.mark.parametrize(
    "command",
    [
        "(cd ../r-wt-x && git commit -m x)",
        "cd ../r-wt-x; git commit -m x",
        "pushd ../r-wt-x && git commit -m x",
        "git -C ../r-wt-x commit -m x",
        "bgrun --wait -o log -C ../r-wt-x -- git commit -m x",
        "sh -c 'cd ../r-wt-x && git commit -m x'",
        "echo msg | (cd ../r-wt-x && git commit -F -)",
        "printf msg | { cd ../r-wt-x && git commit -F -; }",
        "set -o pipefail; (cd ../r-wt-x && git commit -m x) 2>&1 | tail -1",
    ],
)
def test_a_commit_moved_into_a_worktree_passes(main, worktree, command):
    assert hook(main, command) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        'cd "$WT" && git commit -m x',
        "cd ~/r-wt-x && git commit -m x",
        "cd - && git commit -m x",
        "popd && git commit -m x",
        "cd && git commit -m x",
        "git --git-dir=../r/.git --work-tree=. commit -m x",
        "GIT_DIR=.git git commit -m x",
        "export GIT_WORK_TREE=/x; git commit -m x",
        "env -C /x git commit -m x",
        "git worktree add -q --detach ../r-wt-new && cd ../r-wt-new && git commit -m x",
        "cd ../r-wt-missing && git commit -m x",
        'cd "$WT" && echo $(git commit -m x)',
    ],
)
def test_a_commit_whose_directory_is_uncertain_passes(main, command):
    assert hook(main, command) == (0, "")


def test_a_main_tree_after_a_failed_cd_is_not_guessed(main, tmp_path):
    # `cd` to a folder that doesn't exist leaves the shell where it was, the main
    # tree; the gate can't tell the folder won't exist by then, so it lets it pass.
    assert hook(main, "cd ../nowhere; git commit -m x") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "AGENT_MAIN_TREE_OK=1 git commit -m x",
        "AGENT_MAIN_TREE_OK=1 bgrun --wait -o log -- git commit -m x",
        "bgrun --wait -o log -- AGENT_MAIN_TREE_OK=1 git commit -m x",
    ],
)
def test_the_one_shot_prefix_lets_a_commit_through(main, command):
    assert hook(main, command) == (0, "")


@pytest.mark.parametrize(
    "command", ["git commit -h", "git commit --help", "git commit --dry-run"]
)
def test_a_call_that_commits_nothing_passes(main, command):
    assert hook(main, command) == (0, "")


def test_a_cd_in_a_backgrounded_list_leaves_the_commit_in_the_main_tree(main, worktree):
    # The list runs in a subshell; the commit after it runs where the shell was.
    code, err = hook(main, "cd ../r-wt-x && sleep 1 & git commit -m x")
    assert refused(code, err), err


def test_the_prefix_lasts_one_commit(main):
    code, err = hook(main, "AGENT_MAIN_TREE_OK=1 git commit -m x && git commit -m y")
    assert refused(code, err), err


def test_a_repo_without_an_agent_check_passes(tmp_path):
    scratch = make_repo(tmp_path / "scratch", agent_check=False)
    assert hook(scratch, "git commit -m x") == (0, "")


def test_an_agent_check_that_isnt_executable_doesnt_count(main):
    (main / ".githooks" / "agent-pre-commit").chmod(0o644)
    assert hook(main, "git commit -m x") == (0, "")


def test_a_new_repos_first_commit_passes(tmp_path):
    fresh = make_repo(tmp_path / "fresh", commit=False)
    assert hook(fresh, "git add a && git commit -m first") == (0, "")


@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge/",
        "rebase-apply/",
        "sequencer/",
    ],
)
def test_a_commit_that_finishes_an_operation_in_progress_passes(main, marker):
    path = main / ".git" / marker.rstrip("/")
    if marker.endswith("/"):
        path.mkdir()
    else:
        path.write_text(git(main, "rev-parse", "HEAD").stdout)
    assert hook(main, "git commit --no-edit") == (0, "")


def test_a_real_merge_conflict_lets_its_commit_through(main):
    git(main, "switch", "-q", "-c", "side")
    (main / "a").write_text("side\n")
    git(main, "commit", "-q", "-am", "side")
    git(main, "switch", "-q", "main")
    (main / "a").write_text("main\n")
    git(main, "commit", "-q", "-am", "main")
    assert git(main, "merge", "-q", "side", check=False).returncode != 0
    assert hook(main, "git add a && git commit --no-edit") == (0, "")


def test_a_submodule_passes(main, tmp_path):
    inner = make_repo(tmp_path / "inner")
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner),
            "sub",
        ],
        check=True,
        capture_output=True,
        env=without_repo_vars(os.environ),
    )
    sub = main / "sub"
    hooks = git(sub, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    check = Path(hooks.stdout.strip()) / "agent-pre-commit"
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    assert hook(sub, "git commit -m x") == (0, "")
    assert refused(*hook(main, "git commit -m x"))


def test_a_bare_repo_passes(tmp_path):
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    hooks = bare / "hooks" / "agent-pre-commit"
    hooks.write_text("#!/bin/sh\nexit 0\n")
    hooks.chmod(0o755)
    assert hook(bare, "git commit -m x") == (0, "")


def test_a_git_that_fails_lets_the_commit_through(main, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
    (fake / "git").chmod(0o755)
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    assert hook(main, "git commit -m x", env=env) == (0, "")


def test_a_hook_input_without_a_cwd_lets_the_commit_through(main):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
    run = subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=main,
        env=without_repo_vars(os.environ),
    )
    assert (run.returncode, run.stderr) == (0, "")


def test_a_cd_in_one_call_reaches_the_next_through_the_hooks_cwd(main, worktree):
    # The main session's shell keeps a cd for its next call, and Claude Code passes
    # the hook that call's directory as cwd: the gate reads no state of its own, so
    # a bare commit after `cd ../r-wt-x` in the call before is judged in the worktree.
    assert hook(main, f"cd {worktree}") == (0, "")
    assert hook(worktree, "git commit -m x") == (0, "")
    assert refused(*hook(main, "git commit -m x"))


def predicting(repo, body):
    path = repo / ".githooks" / "commit-needs-queue"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.mark.parametrize(
    "command",
    [
        "source ./env.sh && git commit -q -m x",
        "f() { :; }; git commit -q -m x",
        "env -C . git commit -q -m x",
    ],
)
def test_the_queue_check_still_reads_the_directory_the_refusal_doesnt_trust(
    tmp_path, command
):
    # What leaves the main-tree refusal unsure (it passes) leaves the queue check
    # where the plain cds put it, as before that refusal.
    scratch = make_repo(tmp_path / "s", agent_check=False)
    predicting(scratch, "exit 0")
    code, err = hook(
        scratch, command, tool_input={"command": command, "run_in_background": False}
    )
    assert code == 2 and "heavy's queue" in err, err


@pytest.mark.parametrize(
    "command", ["GIT_DIR=.git git commit -n -m x", "git --git-dir=.git commit -n -m x"]
)
def test_no_verify_still_passes_in_a_scratch_repo_that_names_its_git_dir(
    tmp_path, command
):
    scratch = make_repo(tmp_path / "s", agent_check=False)
    assert hook(scratch, command) == (0, "")


def test_no_verify_in_a_main_tree_hears_about_the_main_tree_first(main):
    code, err = hook(main, "git commit -n -m x")
    assert refused(code, err) and "--no-verify" not in err


def test_no_verify_in_a_worktree_is_still_refused(main, worktree):
    code, err = hook(worktree, "git commit --no-verify -m x")
    assert code == 2 and "--no-verify" in err and "main tree" not in err
