"""bin/issue-view-hint: when a `gh issue view` printed to-dos, it tells the agent about issue-todo."""

import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "issue-view-hint"
loader = importlib.machinery.SourceFileLoader("issue_view_hint", str(SCRIPT))
spec = importlib.util.spec_from_loader("issue_view_hint", loader)
hint = importlib.util.module_from_spec(spec)
loader.exec_module(hint)

TODOS = "title:\tPlan\n--\n## Plan\n\n- [ ] The forecast question\n- [x] A control\n"


def run_hook(monkeypatch, capsys, command, stdout=TODOS, tool="Bash"):
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": ""},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = hint.main([])
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        "gh issue view 42 --comments",
        "gh issue view 42",
        "cd repo && gh issue view --comments 42 | head -80",
        "gh issue view 42 -R owner/repo -c",
    ],
)
def test_a_gh_issue_view_that_printed_todos_gets_a_note_about_issue_todo(
    monkeypatch, capsys, command
):
    code, out = run_hook(monkeypatch, capsys, command)
    note = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and note["hookEventName"] == "PostToolUse"
    assert "issue-todo show" in note["additionalContext"]
    assert "--comments" in note["additionalContext"]


@pytest.mark.parametrize(
    "command, stdout, tool",
    [
        ("gh issue view 42", "title:\tPlan\n--\nNo checklist here.\n", "Bash"),
        ("gh pr view 42", TODOS, "Bash"),
        ("cat notes.md", TODOS, "Bash"),
        ("gh issue view 42", TODOS, "Read"),
    ],
)
def test_nothing_is_said_without_todos_from_a_gh_issue_view(
    monkeypatch, capsys, command, stdout, tool
):
    assert run_hook(monkeypatch, capsys, command, stdout, tool) == (0, "")


def test_a_malformed_event_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert (hint.main([]), capsys.readouterr().out) == (0, "")


def test_a_comments_view_gets_the_note_even_when_no_box_was_printed(
    monkeypatch, capsys
):
    code, out = run_hook(
        monkeypatch,
        capsys,
        "gh issue view 42 --comments",
        stdout="author:\tbob\n--\nLooks good.\n",
    )
    assert code == 0
    assert (
        "issue-todo show 42"
        in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh issue view 42 -R owner/repo -c",
        "gh issue view 42 --repo owner/repo",
        "gh issue view --repo=owner/repo 42",
        "gh issue -R owner/repo view 42 --comments",
    ],
)
def test_the_note_names_the_issue_and_the_repo_the_view_asked_for(
    monkeypatch, capsys, command
):
    _, out = run_hook(monkeypatch, capsys, command)
    assert (
        "issue-todo show 42 --repo owner/repo"
        in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    )


@pytest.mark.parametrize(
    "tool_input", [{"file_path": "/r/notes.md"}, {"command": None}]
)
def test_an_event_without_a_command_string_is_ignored(monkeypatch, capsys, tool_input):
    event = {
        "tool_name": "Bash",
        "tool_input": tool_input,
        "tool_response": {"stdout": TODOS},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert (hint.main([]), capsys.readouterr().out) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "git commit -q -F - <<'EOF'\n`gh issue view N --comments` leaves out the body\nEOF",
        "echo 'run gh issue view 42 --comments'",
    ],
)
def test_a_gh_issue_view_mentioned_in_text_gets_no_note(monkeypatch, capsys, command):
    code, out = run_hook(monkeypatch, capsys, command)
    assert code == 0 and out == ""


def test_the_note_names_issue_todo_read(monkeypatch, capsys):
    _, out = run_hook(monkeypatch, capsys, "gh issue view 42 --comments")
    assert "issue-todo read 42" in out


@pytest.mark.parametrize(
    "command",
    [
        "GH_REPO=a/b gh issue view 42 --comments",
        "cat <<< hi\ngh issue view 42 --comments",
        "timeout 30 gh issue view 42 --comments",
    ],
)
def test_a_call_after_a_prefix_or_a_here_string_still_counts(
    monkeypatch, capsys, command
):
    _, out = run_hook(monkeypatch, capsys, command)
    assert "issue-todo read 42" in out


def test_a_quoted_repo_reaches_the_note(monkeypatch, capsys):
    _, out = run_hook(monkeypatch, capsys, 'gh issue view 42 --comments -R "a/b"')
    assert "--repo ''" not in out and "a/b" in out
