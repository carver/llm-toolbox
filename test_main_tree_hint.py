"""bin/main-tree-hint: the note beside an edit or a Bash write in a main tree, run as a
subprocess fed Claude Code's PostToolUse JSON, against repos in tmp_path."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "main-tree-hint"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    )


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
    (path / ".gitignore").write_text("*.log\n")
    (path / "a.py").write_text("a\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "a")
    return path


@pytest.fixture
def main(tmp_path):
    return make_repo(tmp_path / "r")


def hint(cwd, tool, tool_input, response=None):
    """The note the hook adds, or None when it prints nothing; it always exits 0."""
    event = {
        "session_id": "s",
        "cwd": str(cwd),
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": response or {},
    }
    run = subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=False,
        env=without_repo_vars(os.environ),
    )
    assert run.returncode == 0 and run.stderr == "", run.stderr
    if not run.stdout:
        return None
    output = json.loads(run.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    return output["additionalContext"]


def bash(cwd, command):
    return hint(cwd, "Bash", {"command": command})


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_a_file_tools_edit_in_a_main_tree_gets_a_note(main, tool):
    note = hint(main, tool, {"file_path": str(main / "a.py")})
    assert note is not None
    assert f"a.py is in {main}, r's main tree" in note
    assert "leave it as it is" in note and "tell the user" in note
    assert "git worktree add --detach ../r-wt-<topic>" in note


def test_a_notebook_edit_is_found_by_its_notebook_path(main):
    assert hint(main, "NotebookEdit", {"notebook_path": str(main / "n.ipynb")})


def test_an_edit_in_a_worktree_gets_none(main):
    worktree = main.parent / "r-wt-x"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    assert hint(worktree, "Edit", {"file_path": str(worktree / "a.py")}) is None


def test_an_edit_in_a_repo_without_an_agent_check_gets_none(tmp_path):
    scratch = make_repo(tmp_path / "s", agent_check=False)
    assert hint(scratch, "Write", {"file_path": str(scratch / "a.py")}) is None


@pytest.mark.parametrize("path", ["x.log", ".git/info/exclude", "../elsewhere.txt"])
def test_an_ignored_file_or_one_outside_the_tree_gets_none(main, path):
    assert hint(main, "Write", {"file_path": str(main / path)}) is None


def test_an_edit_while_a_merge_is_in_progress_gets_none(main):
    (main / ".git" / "MERGE_HEAD").write_text(git(main, "rev-parse", "HEAD").stdout)
    assert hint(main, "Edit", {"file_path": str(main / "a.py")}) is None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/a/b/' a.py",
        "sed -i.bak -e 's/a/b/' a.py",
        "perl -pi -e 's/a/b/' a.py",
        "echo b > a.py",
        "echo b >> a.py",
        "echo b | tee -a a.py",
        "cd lib && echo b > ../a.py",
        "echo b > /dev/null && echo c > a.py",
        "apply-edits <<'EOF'\n--- a.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\nEOF",
    ],
)
def test_a_bash_write_in_a_main_tree_gets_a_note(main, command):
    (main / "lib").mkdir()
    note = bash(main, command)
    assert note is not None and f"a.py is in {main}" in note, command


@pytest.mark.parametrize(
    "command",
    [
        "cat a.py",
        "sed 's/a/b/' a.py",
        "echo b > /dev/null",
        "echo b > x.log",
        'echo b > "$OUT"',
        "cd ../r-wt-x && echo b > a.py",
        "git diff > /tmp/patch",
        "apply-edits --cached <<'EOF'\n--- a.py\nEOF",
        "2>&1 echo hi",
        "{ cd ../r-wt-x && sed -i s/a/b/ a.py; } | tail",
        "(cd ../r-wt-x && echo b > a.py) 2>&1 | tail",
    ],
)
def test_a_bash_call_that_writes_nothing_in_the_main_tree_gets_none(main, command):
    worktree = main.parent / "r-wt-x"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    assert bash(main, command) is None


def test_an_absolute_path_is_found_wherever_the_shell_stands(main):
    note = bash(main.parent, f'cd "$SOMEWHERE"; echo b > {main}/a.py')
    assert note is not None and f"a.py is in {main}" in note


def test_a_repo_with_its_own_worktree_script_hears_of_that(main):
    script = main / "bin" / "new-worktree"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    note = hint(main, "Edit", {"file_path": str(main / "a.py")})
    assert "`bin/new-worktree <topic>`" in note and "git worktree add" not in note


def test_a_note_names_a_few_files_then_counts_the_rest(main):
    command = "; ".join(f"echo x > f{n}.txt" for n in range(5))
    note = bash(main, command)
    assert "f0.txt, f1.txt, f2.txt and 2 more are in" in note


@pytest.mark.parametrize(
    "stdin", ["", "not json", "[]", '{"tool_name": "Bash"}', '{"tool_input": 3}']
)
def test_input_it_cant_read_says_nothing(stdin):
    run = subprocess.run(
        [str(SCRIPT)], input=stdin, capture_output=True, text=True, check=False
    )
    assert (run.returncode, run.stdout, run.stderr) == (0, "", "")
