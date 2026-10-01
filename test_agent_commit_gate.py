"""bin/agent-commit-gate: which commands commit where, and a real repo whose agent check passes or fails."""

import importlib.machinery
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "agent-commit-gate"
loader = importlib.machinery.SourceFileLoader("agent_commit_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader("agent_commit_gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)


@pytest.mark.parametrize(
    "command, dirs",
    [
        ("git commit -m x", ["/r"]),
        ("git add a && git commit -q -F - <<'EOF'\nfix: it's done\nEOF", ["/r"]),
        ("cd sub && git commit -m x", ["/r/sub"]),
        ("git -C /elsewhere -c user.name=a commit --amend", ["/elsewhere"]),
        ("/usr/bin/git commit -m x; git commit -m y", ["/r"]),
        ("git status && git log --oneline -1", []),
        ("git commit --help", []),
        ("echo git commit", []),
    ],
)
def test_finds_each_commit_and_where_it_runs(command, dirs):
    assert gate.commit_dirs(command, "/r") == [Path(d) for d in dirs]


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    (tmp_path / ".githooks").mkdir()
    return tmp_path


def agent_check(repo, body):
    hook = repo / ".githooks" / gate.HOOK_NAME
    hook.write_text(f"#!/bin/sh\n{body}\n")
    hook.chmod(0o755)


def run_hook(monkeypatch, capsys, command, cwd, tool="Bash"):
    event = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = gate.main([])
    return code, capsys.readouterr().err


def test_a_failing_check_blocks_the_commit_and_shows_its_output(
    repo, monkeypatch, capsys
):
    agent_check(repo, "echo '1 failed, 3 passed'; exit 1")
    code, err = run_hook(monkeypatch, capsys, "git commit -m x", repo)
    assert code == 2 and "1 failed, 3 passed" in err and "Commit blocked" in err


def test_a_passing_check_lets_the_commit_through(repo, monkeypatch, capsys):
    agent_check(repo, "exit 0")
    assert run_hook(monkeypatch, capsys, "git commit -m x", repo) == (0, "")


def test_no_check_and_no_commit_pass_untouched(repo, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "git commit -m x", repo) == (0, "")
    agent_check(repo, "exit 1")
    assert run_hook(monkeypatch, capsys, "git status", repo) == (0, "")
    assert run_hook(monkeypatch, capsys, "git commit -m x", repo, tool="Edit") == (
        0,
        "",
    )


def test_a_commit_outside_any_repo_passes(tmp_path, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "git commit -m x", tmp_path) == (0, "")


def test_the_check_runs_from_the_top_of_the_repo(repo, monkeypatch, capsys):
    (repo / "sub").mkdir()
    agent_check(repo, "test -d .githooks || { echo 'not at the top'; exit 1; }")
    assert run_hook(monkeypatch, capsys, "cd sub && git commit -m x", repo) == (0, "")
