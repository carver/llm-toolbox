"""bin/pipe-status-gate: which Bash commands piping git commit, wt-ship or bgrun --wait it
refuses, and what it lets through."""

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "pipe-status-gate"


def run_hook(command, tool="Bash"):
    """The hook's exit status and stderr for one call, its input shaped as Claude Code sends it."""
    event = {
        "session_id": "s",
        "transcript_path": "/t.jsonl",
        "cwd": "/r",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command, "description": "Run it"},
    }
    result = subprocess.run(
        [SCRIPT], input=json.dumps(event), capture_output=True, text=True, check=False
    )
    return result.returncode, result.stderr


PIPED = [
    ("git commit -q -F msg 2>&1 | tail -2", "git commit"),
    ("git commit -m x 2>&1 | grep -v warning", "git commit"),
    ("git commit -m x 2>&1|tail", "git commit"),
    ("git commit -m x |& tee commit.log", "git commit"),
    ("git commit -m x |\n  tail -5", "git commit"),
    ("git -C repo commit -m x | tail", "git commit"),
    ("cd repo && git commit -m x | tail -3 && git log -1", "git commit"),
    ("AI_AGENT=1 timeout 600 git commit -m x | tail", "git commit"),
    ("echo msg | git commit -F - | tail", "git commit"),
    ("git commit -F - <<'EOF' | tail\nsubject\nEOF", "git commit"),
    ("wt-ship -C ../x --local --remove | tail -20", "wt-ship"),
    ("nice wt-ship --local 2>&1 | grep -v '^ok'", "wt-ship"),
    ("bgrun --wait -o log -- git commit -q -F msg 2>&1 | tail -2", "bgrun --wait"),
    ("bgrun --wait -o log -- pytest -q | tail", "bgrun --wait"),
    ("bgrun --w -o log pytest | tail", "bgrun --wait"),
    ("(cd x && git commit -m y) 2>&1 | tail", "git commit"),
    ("{ git commit -m y; } 2>&1 | tail", "git commit"),
    ("for m in a b; do git commit -m $m; done | tail", "git commit"),
    ("if true; then wt-ship --local; fi | tail", "wt-ship"),
    ("bash -c 'git commit -m x' | tail", "git commit"),
    ("bash -c 'git commit -m x | tail'", "git commit"),
    ("eval 'git commit -m x | tail'", "git commit"),
    ('out=$(git commit -m x 2>&1 | tail -3); echo "$out"', "git commit"),
    ("bash <<'EOF'\ngit commit -m x | tail\nEOF", "git commit"),
    ("bash <<'EOF' | tail\ngit commit -m x\nEOF", "git commit"),
    ("bgrun -o log -- bash -c 'git commit -m x | tail'", "git commit"),
    ("set -o pipefail; set +o pipefail; git commit -m x | tail", "git commit"),
    ("set -o pipefail; bash -c 'git commit -m x | tail'", "git commit"),
    ('git commit -m "unclosed | tail', "git commit"),
    ("git commit -m '' --allow-empty-message | tail", "git commit"),
    ("(git commit -m x)|tail", "git commit"),
    ("(git commit -m x; echo done)>log | tail", "git commit"),
    ("git commit -F <(printf msg) -q | tail", "git commit"),
    ("git --config-env x=Y commit -m x | tail", "git commit"),
    ("git commit -m -h | tail", "git commit"),
    # Claude Code's own commit form, with an apostrophe in the message.
    ("git commit -m \"$(cat <<'EOF'\nDon't break\nEOF\n)\" | tail -2", "git commit"),
    # pipefail inside what's piped leaves the outer pipeline's status tail's.
    ("( set -o pipefail; git commit -m x ) | tail", "git commit"),
    ("bash -o pipefail -c 'git commit -m x' | tail", "git commit"),
    ("bash -euo pipefail -c 'git commit -m x' | tail", "git commit"),
    ("bash -c 'set -euo pipefail; git commit -m x' 2>&1 | tail", "git commit"),
    ("eval 'set -o pipefail; git commit -m x' | tail", "git commit"),
    ("bash <<'EOF' 2>&1 | tail\nset -o pipefail\ngit commit -m x\nEOF", "git commit"),
    # A set inside a compound command leaves the pipeline after it without pipefail.
    ("if true; then set -o pipefail; fi; git commit -m x | tail", "git commit"),
    # Inside a piped shell, a substitution's status is hidden too.
    ("bash -c 'out=$(git commit -m x 2>&1)' | tail", "git commit"),
    ("git commit -m x |>log tail", "git commit"),
    ("git commit -m x &>log | tail", "git commit"),
    ("(git commit -m x;)|tail", "git commit"),
    # A case pattern's ) closes no group around it.
    ("if true; then case $x in a) :;; esac; git commit -m x; fi | tail", "git commit"),
    ("( case $x in a) :;; esac; git commit -m x ) | tail", "git commit"),
    ("wt-ship --dry-run | tail", "wt-ship"),
]


@pytest.mark.parametrize(("command", "name"), PIPED)
def test_a_piped_commit_landing_or_wait_is_refused_naming_it(command, name):
    code, err = run_hook(command)

    assert code == 2
    assert name in err.splitlines()[0]


def test_the_refusal_says_why_and_what_to_do_in_two_lines():
    code, err = run_hook("bgrun --wait -o log -- git commit -q -F msg 2>&1 | tail -2")

    assert code == 2
    assert len(err.splitlines()) <= 2
    assert "last command's status" in err
    assert "Drop the pipe" in err and "log's last lines" in err
    assert "set -o pipefail" in err and "redirect" in err


@pytest.mark.parametrize(
    "command",
    [
        "git commit -q -F msg",
        "bgrun --wait -o log -- git commit -q -F msg",
        "wt-ship -C ../x --local --remove",
        "git commit -m 'a | b'",
        'git commit -m "x | tail"',
        "git commit -m $'a | b'",
        "git commit -m 'x' -m '|' -m done",
        'git commit -m "|"',
        "git commit -m x || echo failed",
        "git commit -m x && git log -1 | cat",
        "(git commit -m x) && echo ok | tail",
        "echo msg | git commit -F -",
        "git commit -F - <<'EOF'\nfix | tail\nEOF",
        "git commit -m x # | tail",
        "git commit -m $'|' -q",
        "bash -c '' 'git commit -m x | tail'",
        "git commit -m x;>log echo | tail",
        "{ git commit -m x; ( case $y in a) :;; esac ) | tail; }",
        "set -o pipefail; eval 'git commit -m x | tail'",
        "git commit -q -m x >| log",
        "git commit -m \"$(cat <<'EOF'\nDon't break\nEOF\n)\"",
        "git commit --dry-run | head",
        "set -o pipefail; { git commit -m x | tail; }",
        "bash -euo pipefail -c 'git commit -m x | tail'",
        "set -o pipefail; git commit -m x 2>&1 | tail -2",
        "set -euo pipefail\nwt-ship --local | tail",
        "set -e -o pipefail && bgrun --wait -o log -- pytest | tail",
        "bash -o pipefail -c 'git commit -m x | tail'",
        "bash -c 'set -o pipefail; git commit -m x | tail'",
        "set -o pipefail; out=$(git commit -m x | tail)",
        "wt-ship -h | head",
        "git commit -h | head",
        "bgrun --wait -h | head",
        "bgrun -o log -- pytest | tail",
        "git log --oneline | head",
        "git status --short | head -5",
        "git help commit | head",
        "safeps -f wt-ship | head",
        "grep -n 'git commit' notes.md | head",
        "cat log | grep 'bgrun --wait'",
    ],
)
def test_a_command_whose_status_stays_honest_passes(command):
    assert run_hook(command) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        ")",
        ";;)",
        ")|&",
        "|\n|",
        "(((",
        "} done fi",
        "| tail",
        "&& ||",
        "'",
        "$(",
        "<<",
        "<(",
        "`",
        "git commit <<",
        "done | git commit | (",
        "\\",
    ],
)
def test_odd_shell_never_crashes_the_hook(command):
    assert run_hook(command)[0] in (0, 2)


def test_other_tools_pass():
    assert run_hook("git commit -m x | tail", tool="Write") == (0, "")


def test_input_that_is_not_json_fails_in_one_line():
    result = subprocess.run(
        [SCRIPT], input="{not json", capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert len(result.stderr.splitlines()) == 1


def test_help_prints_the_docstring():
    result = subprocess.run([SCRIPT, "-h"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "pipefail" in result.stdout
