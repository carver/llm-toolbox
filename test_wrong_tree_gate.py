"""bin/wrong-tree-gate as Claude Code's PreToolUse hook: destructive git after a cd
whose failure wouldn't stop it, or in a main tree, run as a subprocess fed the hook's
JSON, against repos in tmp_path."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import saved_command, without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "wrong-tree-gate"
NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    )


def hook(cwd, command, **event):
    """The gate's exit status and stderr for one Bash call run from `cwd`, or with
    no cwd in the event when `cwd` is None."""
    payload = {
        "session_id": "s",
        "transcript_path": "/t.jsonl",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    } | event
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return run_gate(json.dumps(payload))


def run_gate(stdin):
    run = subprocess.run(
        [str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=without_repo_vars(os.environ),
    )
    return run.returncode, run.stderr


def make_repo(path, agent_check=True):
    path.mkdir(parents=True)
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


@pytest.fixture
def scratch(tmp_path):
    """A repo without an agent check, as a review's scratch repo is."""
    return make_repo(tmp_path / "scratch", agent_check=False)


def test_the_incident_is_refused_naming_the_cd(main, tmp_path):
    # 2026-11-23: the folder in $d didn't exist, so `cd $d` failed and the reset
    # dropped the main tree's newest commit.
    command = (
        f"cd {main}; git log --oneline -1; d={tmp_path}/missing/r; cd $d;"
        " printf x > h; git reset -q --hard HEAD~1; git merge -q --ff-only main"
    )
    code, err = hook(main, command)
    assert code == 2
    assert "`git reset --hard` runs after `cd '$d'`" in err
    assert "&&" in err and "git -C DIR" in err and "set -e" in err
    assert NOTHING_RAN in err


def test_a_reset_after_a_cd_sent_to_the_background_is_refused(main, worktree):
    # The shape of 2026-11-26's crash: the `&` takes the cd with the sleep.
    command = f"cd {worktree} && sleep 900 & by=$!; git reset --hard; kill $by"
    code, err = hook(main, command)
    assert code == 2
    assert f"`git reset --hard` would run in {main}, r's main tree" in err
    assert "never a cd sent to the background" in err


def test_a_reset_in_a_worktree_reached_with_and_passes(main, worktree):
    assert hook(main, f"cd {worktree} && git reset --hard HEAD~1") == (0, "")


def test_a_reset_in_a_scratch_repo_passes(scratch):
    assert hook(scratch, "git reset --hard HEAD") == (0, "")
    assert hook(scratch, "git clean -fdx && git checkout -f main") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git reset -q --hard HEAD~1",
        "git -c core.x=y reset --hard",
        "git checkout -f main",
        "git checkout -qf main",
        "git checkout --force",
        "git switch -f main",
        "git switch --discard-changes main",
        "git clean -f",
        "git clean -fdx",
        "git clean --force",
        "git clean -Xf",
        "git clean -dXf",
        "git restore a",
        "git restore --worktree a",
        "git restore -SW a",
        "git restore -s HEAD~1 a",
        "git stash drop",
        "git stash clear",
        "git stash -q drop",
        "git stash -q drop stash@{1}",
        "git checkout -- .",
        "git checkout .",
        "git checkout HEAD~1 -- a",
        "x=$(git reset --hard 2>&1)",
        "echo `git stash drop`",
        "git rebase main",
        "GIT_SEQUENCE_EDITOR=: git rebase -q -i --autosquash HEAD~1",
        "bgrun --wait -o log -- git rebase main",
        "git status; git reset --hard",
    ],
)
def test_destructive_git_in_a_main_tree_is_refused(main, command):
    code, err = hook(main, command)
    assert code == 2
    assert f"would run in {main}, r's main tree, which only receives landings" in err
    assert "git worktree add --detach ../r-wt-<topic>" in err
    assert "ask the user first" in err
    assert NOTHING_RAN in err


@pytest.mark.parametrize(
    "command",
    [
        "git reset --soft HEAD~1",
        "git reset HEAD a",
        "git reset -h",
        "git checkout -b f",
        "git checkout main",
        "git checkout main --",
        "git checkout -bf",
        "git switch -c f",
        "git clean -n",
        "git clean -fn",
        "git clean --dry-run -f",
        "git restore --staged a",
        "git restore -S a",
        "git stash",
        "git stash pop",
        "git stash list",
        "git stash show -p drop",
        "git rebase --continue",
        "git rebase --abort",
        "git rebase --skip",
        "git rebase -h",
        "git log --grep 'reset --hard'",
        "echo git reset --hard",
    ],
)
def test_what_destroys_nothing_passes_in_a_main_tree(main, command):
    assert hook(main, command) == (0, "")


def test_the_folder_it_judges_is_the_gits_own(main, worktree):
    assert hook(worktree, "git reset --hard")[0] == 0
    assert hook(worktree, f"git -C {main} reset --hard")[0] == 2
    assert hook(worktree, f"cd {main} && git reset --hard")[0] == 2
    assert hook(main, f"git -C {worktree} reset --hard")[0] == 0
    assert hook(main, f"bgrun -C {worktree} -o l -- git reset --hard")[0] == 0
    assert hook(main, f"(cd {worktree} && git reset --hard)")[0] == 0
    assert hook(main, f"(cd {worktree} && true); git reset --hard")[0] == 2


def test_a_main_tree_whose_folder_isnt_certain_passes(main):
    assert hook(main, 'cd "$WT" && git reset --hard') == (0, "")
    assert hook(main, ". ./env.sh && git reset --hard") == (0, "")


def test_a_repo_with_its_own_worktree_script_hears_of_that(main):
    script = main / "bin" / "new-worktree"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    code, err = hook(main, "git reset --hard")
    assert code == 2
    assert "`bin/new-worktree <topic>`" in err


def test_the_one_shot_prefix_lets_a_command_through(main):
    assert hook(main, "AGENT_MAIN_TREE_OK=1 git reset --hard") == (0, "")
    assert hook(main, "AGENT_MAIN_TREE_OK=0 git reset --hard")[0] == 2
    assert hook(main, "AGENT_MAIN_TREE_OK=1 git status; git reset --hard")[0] == 2


@pytest.mark.parametrize(
    "marker", ["rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"]
)
def test_a_main_tree_mid_operation_lets_only_its_paths_and_finish_through(main, marker):
    # The honest case: an agent resolving its own rebase in a main tree, here a repo
    # with no worktree at all. The owner's call (2026-11-26): a path-level fix and
    # the operation's own finish pass; whatever drops work stays refused.
    git_dir = main / ".git"
    if marker.endswith("_HEAD"):
        (git_dir / marker).write_text(git(main, "rev-parse", "HEAD").stdout)
    else:
        (git_dir / marker).mkdir()
    for command in (
        "git restore --theirs a",
        "git restore a",
        "git checkout -f -- a",
        "git checkout --theirs -f a",
        "git checkout -- a",
        "git rebase --continue",
        "git rebase --abort",
        "git merge --abort",
        "git cherry-pick --skip",
    ):
        assert hook(main, command) == (0, ""), command
    for command in (
        "git reset --hard",
        "git clean -fd",
        "git stash drop",
        "git stash clear",
        "git checkout -f main",
        "git switch -f main",
        "git rebase main",
    ):
        code, err = hook(main, command)
        assert code == 2, command
        assert "only finishing it" in err
    assert hook(main, "AGENT_MAIN_TREE_OK=1 git reset --hard") == (0, "")


def test_an_unborn_head_lets_everything_through(tmp_path):
    repo = tmp_path / "u"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.hooksPath", ".githooks")
    (repo / ".githooks").mkdir()
    check = repo / ".githooks" / "agent-pre-commit"
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    assert hook(repo, "git clean -fdx && git reset --hard") == (0, "")


def test_a_real_rebase_conflict_in_a_main_tree_can_be_resolved(main):
    git(main, "checkout", "-q", "-b", "side")
    (main / "a").write_text("side\n")
    git(main, "commit", "-qam", "side")
    git(main, "checkout", "-q", "main")
    (main / "a").write_text("main\n")
    git(main, "commit", "-qam", "main")
    git(main, "checkout", "-q", "side")
    assert hook(main, "git rebase main")[0] == 2
    rebase = subprocess.run(
        ["git", "-C", str(main), "rebase", "main"],
        capture_output=True,
        env=without_repo_vars(os.environ),
        check=False,
    )
    assert rebase.returncode != 0
    for command in ("git checkout --theirs a", "git rebase --continue"):
        assert hook(main, command) == (0, "")
    assert hook(main, "git restore a && git rebase --abort") == (0, "")


def test_a_repo_without_an_agent_check_passes_in_its_main_tree(scratch):
    assert hook(scratch, "git reset --hard && git clean -fdx") == (0, "")


LOOSE_CDS = [
    "cd $X; git reset --hard",
    "cd $X\ngit reset --hard",
    "cd /no/such/folder; git reset --hard",
    "cd sub; git clean -fdx",
    "cd $X; git -C sub reset --hard",
    "set -u; cd $X; git rebase main",
    "set -e; set +e; cd $X; git rebase main",
    "cd $X; git status; git stash drop",
    # An && list that ends with `;` or a newline runs on after the cd fails.
    "cd /no/such/x && git fetch; git reset --hard origin/main",
    "cd $X && git fetch\ngit reset --hard",
    "cd $X && { git fetch; }; git reset --hard",
    "cd $X && (git fetch); git reset --hard",
    "cd /abs && git fetch; git reset --hard",
    # Where bash ignores set -e.
    "set -e; { cd $X; git reset --hard HEAD~1; } || echo failed",
    "set -e; (cd $X; git reset --hard) || true",
    "set -e; if true; then cd $X; git reset --hard; fi || true",
    "set -e; while true; do cd $X; git reset --hard; break; done && echo",
    "set -e; f() { cd $X; git reset --hard; }; f || true",
    "set -e; if cd $X; git reset --hard; then :; fi",
    "set -e; cd $X && true; git reset --hard",
    # A cd that is an if's or loop's condition, where the shell goes on after it fails.
    "if cd $X; then :; fi; git reset --hard",
    "if cd $X; then :; else git reset --hard; fi",
    "if ! cd $X; then echo no; fi; git reset --hard",
    "until cd $X; do git reset --hard; done",
    "until cd $X; do break; done; git reset --hard",
    "while cd $X; do break; done; git reset --hard",
    # A shell's script and a substitution run after the cd too.
    "cd $X; bash -c 'git reset --hard'",
    "cd $X; sh -c 'git status; git stash clear'",
    "cd $X; x=$(git reset --hard 2>&1)",
]


@pytest.mark.parametrize("command", LOOSE_CDS)
def test_a_cd_whose_failure_would_leave_it_in_a_main_tree_is_refused(main, command):
    code, err = hook(main, command)
    assert code == 2
    assert "so if the cd fails the shell stays where it was" in err


@pytest.mark.parametrize("command", LOOSE_CDS)
def test_a_cd_whose_failure_would_leave_it_somewhere_unknown_is_refused(command):
    assert hook(None, command)[0] == 2


@pytest.mark.parametrize("command", LOOSE_CDS)
def test_a_cd_whose_failure_would_leave_it_outside_a_main_tree_passes(
    tmp_path, scratch, worktree, command
):
    for cwd in (tmp_path, scratch, worktree):
        assert hook(cwd, command) == (0, ""), cwd


def test_a_failed_cd_judges_every_folder_the_shell_may_be_left_in(main, tmp_path):
    # If both cds fail, the shell is still in the main tree.
    assert hook(main, f"cd {tmp_path}/gone; cd $X; git reset --hard")[0] == 2
    assert hook(main, f"cd {tmp_path}; cd $X; git reset --hard") == (0, "")
    assert hook(tmp_path, "cd ..; git reset --hard") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "cd $X && git reset --hard",
        "cd $X && git fetch && git reset --hard; echo done",
        "cd $X && { git fetch; git reset --hard; }",
        "cd $X || exit 1; git reset --hard",
        "set -e; cd $X; git reset --hard",
        "set -euo pipefail\ncd $X\ngit reset --hard",
        "set -o errexit; cd $X; git reset --hard",
        "set -e; { cd $X; git reset --hard; }",
        "set -e; true || { cd $X; git reset --hard; }",
        "set -e; if true; then cd $X; git reset --hard; fi",
        "cd $X; cd /abs && git reset --hard",
        "cd $X; git -C /abs reset --hard",
        "cd $X; git status",
        "(cd $X; true); git reset --hard",
        "git reset --hard; cd $X",
        # The body runs only when the cd succeeded, or the shell exits when it fails.
        "if cd $X; then git reset --hard; fi",
        "if ! cd $X; then exit 1; fi; git reset --hard",
        "if ! cd $X\nthen\n  echo no >&2\n  exit 1\nfi\ngit reset --hard",
        "if ! cd $X; then return 1; else echo; fi; git reset --hard",
        "until cd $X; do sleep 1; done; git reset --hard",
        "while ! cd $X; do sleep 1; done; git reset --hard",
        # A function's body is where set -e may not hold, not the rest.
        "set -e\nf() { echo; }\ncd $X\ngit rebase main",
        "set -e; function f { echo; }; f || true; cd $X; git rebase main",
        # An array assignment defines no function.
        "arr=(a b); set -e; cd $X; git reset --hard",
        "set -e; arr+=(c); cd $X; git reset --hard",
        "cd $X; bash -c 'cd /abs && git reset --hard'",
    ],
)
def test_a_cd_whose_failure_doesnt_matter_passes(command):
    assert hook(None, command) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "set -e; f() { cd $X; git reset --hard; }; f",
        "set -e; function f { cd $X; git reset --hard; }; f",
        "f() { set +e; }; set -e; f; cd $X; git reset --hard",
        "set -e; { cd $X; git reset --hard; } || echo failed",
        "set -e; cd $X && true; git reset --hard",
    ],
)
def test_where_set_e_wouldnt_help_the_refusal_says_why(command):
    code, err = hook(None, command)
    assert code == 2
    assert "`set -e` wouldn't stop it: " in err
    assert "put `set -e` first" not in err


@pytest.mark.parametrize(
    "command",
    [
        'cd "$S" && git rebase main; cd "$T" && git rebase main',
        "cd $S; cd $T && git rebase main",
    ],
)
def test_a_step_chained_to_an_unsure_cd_hears_of_that_cd(main, worktree, command):
    # The rebase waits for `cd "$T"`; what matters is whether that names a relative
    # folder, which is then taken from wherever a failed `cd "$S"` left the shell.
    code, err = hook(main, command)
    assert code == 2
    assert "`git rebase` runs after `cd '$T' &&`, whose folder isn't certain" in err
    assert "the earlier `cd '$S'`" in err
    assert "Give this cd an absolute folder" in err
    assert hook(worktree, command) == (0, "")


def test_a_cd_to_a_folder_that_is_there_passes(main, worktree):
    assert hook(main, f"cd {worktree}; git rebase main") == (0, "")
    assert hook(main, f"cd {worktree}/gone; git rebase main")[0] == 2
    assert hook(None, f"cd {worktree} && git fetch; git reset --hard") == (0, "")


def test_a_relative_cd_to_a_folder_that_is_there_passes(main, worktree, tmp_path):
    # The workspace's convention: `cd ../<repo>-wt-<topic>` from the main tree.
    assert hook(main, "cd ../r-wt-x; git rebase main") == (0, "")
    assert hook(main, "cd ../r-wt-x\ngit fetch\ngit reset --hard") == (0, "")
    assert hook(main, "cd ../r-wt-gone; git rebase main")[0] == 2
    assert hook(None, "cd ../r-wt-x; git rebase main")[0] == 2
    # It is taken from the folder an earlier cd went to, which may have failed.
    assert hook(main, f"cd {tmp_path}/gone; cd ../r-wt-x; git rebase main")[0] == 2
    assert hook(main, f"cd {worktree}; cd ../r-wt-x; git rebase main") == (0, "")


def test_a_refused_command_is_saved_for_the_next_call(main, refused_session):
    fields, folder = refused_session
    command = "git reset --hard <<'X'\nbody\nX"
    code, err = hook(main, command, **fields)
    assert code == 2
    assert saved_command(err, folder) == command


def test_another_tools_call_passes(main):
    assert hook(main, "git reset --hard", tool_name="Edit") == (0, "")


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        "[]",
        '{"tool_name": "Bash"}',
        '{"tool_name": "Bash", "tool_input": {"command": 3}}',
        '{"tool_name": "Bash", "tool_input": null}',
    ],
)
def test_input_it_cant_read_lets_the_call_through(stdin):
    assert run_gate(stdin) == (0, "")


def test_help_prints_the_docstring():
    run = subprocess.run(
        [str(SCRIPT), "-h"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0
    assert "Exit status" in run.stdout
