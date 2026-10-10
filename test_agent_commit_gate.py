"""bin/agent-commit-gate: which commands commit where, and a real repo whose agent check passes or fails."""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import without_repo_vars

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
    env = without_repo_vars(os.environ)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.hooksPath", ".githooks"],
        check=True,
        env=env,
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


@pytest.mark.parametrize(
    "command, staged",
    [
        ("git add a && git commit -m x", ["git add"]),
        ("git add a; git commit -m x", ["git add"]),
        ("git -C sub stage . && git commit -m x", ["git stage"]),
        ("git rm old && git mv a b && git commit -m x", ["git rm", "git mv"]),
        ("git restore --staged a && git commit -m x", ["git restore --staged"]),
        ("git reset -q a && git commit -m x", ["git reset"]),
        ("git apply --cached p.diff && git commit -m x", ["git apply --cached"]),
        ("git commit -am x", ["git commit -a"]),
        ("git commit -a -m x", ["git commit -a"]),
        ("git commit --all -m x", ["git commit -a"]),
        ("git commit -m x src/a.py", ["git commit PATHS"]),
        ("git commit -q -m x -- a.py", ["git commit PATHS"]),
        ("git commit -o -m x a.py", ["git commit PATHS"]),
        ("git commit -m x", []),
        ("git commit -qm 'add a thing'", []),
        ("git commit --author 'A <a@b.c>' --date now -m x", []),
        ("git commit --fixup=abc123", []),
        ("git commit --amend --no-edit", []),
        ("git commit -q -F - <<'EOF'\nfix: done\nEOF", []),
        ("git status && git diff --cached && git commit -m x", []),
        ("git restore a && git commit -m x", []),
        ("git apply p.diff && git commit -m x", []),
        ("git add a", ["git add"]),
        ('git commit -q -m "fix: first line\n\nbody words here"', []),
        ('git commit -qam "fix: first line\n\nbody"', ["git commit -a"]),
        ("git commit -q -F - <<'EOF'\nfix: it's done\nEOF", []),
        ('git commit -m "don\'t stop"', []),
    ],
)
def test_finds_what_a_command_stages(command, staged):
    assert gate.staging(command) == staged


def test_staging_and_committing_in_one_command_is_blocked_before_the_check(
    repo, monkeypatch, capsys
):
    agent_check(repo, "echo ran > ran.txt")
    code, err = run_hook(monkeypatch, capsys, "git add a && git commit -m x", repo)
    assert code == 2
    assert "git add" in err and "own command" in err
    assert not (repo / "ran.txt").exists()


def test_commit_all_is_blocked_too(repo, monkeypatch, capsys):
    agent_check(repo, "exit 0")
    code, err = run_hook(monkeypatch, capsys, "git commit -am x", repo)
    assert code == 2 and "git commit -a" in err


def test_staging_alone_or_without_an_agent_check_passes(
    repo, monkeypatch, capsys, tmp_path
):
    agent_check(repo, "exit 0")
    assert run_hook(monkeypatch, capsys, "git add a", repo) == (0, "")
    plain = tmp_path / "plain"
    subprocess.run(
        ["git", "init", "-q", str(plain)], check=True, env=without_repo_vars(os.environ)
    )
    assert run_hook(monkeypatch, capsys, "git add a && git commit -m x", plain) == (
        0,
        "",
    )
