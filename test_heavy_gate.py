"""bin/heavy-gate: which Bash calls that wait on heavy it refuses, and what it lets through."""

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "heavy-gate"


# What every gate's refusal ends with: a refused call runs none of its parts.
NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."


def run_hook(command, background=None, tool="Bash", timeout=None, agent=None):
    """The hook's exit status and stderr for one call, its input shaped as Claude Code sends it.
    ``timeout`` is the Bash call's own, in ms; ``agent`` a subagent's id, when it made the call."""
    tool_input = {"command": command, "description": "Run it"}
    if background is not None:
        tool_input["run_in_background"] = background
    if timeout is not None:
        tool_input["timeout"] = timeout
    event = {
        "session_id": "s",
        "transcript_path": "/t.jsonl",
        "cwd": "/r",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if agent:
        event |= {"agent_id": agent, "agent_type": "general-purpose"}
    result = subprocess.run(
        [SCRIPT], input=json.dumps(event), capture_output=True, text=True, check=False
    )
    return result.returncode, result.stderr


WAITS_ON_HEAVY = [
    "heavy -- cargo nextest run --cargo-quiet",
    "timeout 600 heavy -- cargo nextest run --cargo-quiet",
    "timeout -s KILL 10m heavy -- make",
    "nice heavy -- make",
    "nice -n 5 heavy --long -- cargo mutants",
    "heavy --l -- cargo test",
    'heavy "$@"',
    "bgrun --wait -o /tmp/s/log -- heavy -- cargo test",
    "bgrun --wait -o log heavy -- cargo test",
    "bgrun --wait -o log -- nice heavy -- cargo test",
    "cd crates/server && heavy -- cargo test 2>&1 | tail -20",
    "~/.local/bin/heavy -- cargo build",
    "FOO=1 env BAR=2 heavy -- make",
    "sudo -u bob heavy -- make",
    "echo x | xargs heavy -- make",
    "nohup heavy -- cargo build > build.log 2>&1 &",
    "bash -lc 'heavy -- cargo test'",
    "bash -euo pipefail -c 'heavy -- cargo test'",
    "eval 'heavy -- cargo test'",
    'out="$(heavy -- cargo test 2>&1)"; echo "$out"',
    "echo $(heavy -- cargo test)",
    "echo `heavy -- cargo test`",
    "diff <(heavy -- cargo test) expected",
    "if heavy -- cargo test; then echo ok; fi",
    "for t in a b; do heavy -- cargo test $t; done",
    "(cd x && heavy -- make)",
    "echo start # a note\nheavy -- cargo test",
    "curl -s https://x.dev/p#a; heavy -- cargo test",
    "echo $'it\\'s' && heavy -- cargo test",
    "heavy -- cargo \\\n  test",
    "bash <<'EOF'\nheavy -- cargo test\nEOF",
    "cat <<'EOF' | sh\nheavy -- cargo test\nEOF",
    "cat <<<EOF\nheavy -- cargo test\nEOF",
    'git commit -m "fix <<EOF parsing"\nheavy -- cargo test',
    "x=$((a<<b))\nheavy -- cargo test",
    "bash <<EOF\nheavy -- cargo test",
    "2>/dev/null heavy -- cargo test",
    "bgrun --wait --o log -- heavy -- cargo test",
    "bgrun --wait --out log -- heavy -- cargo test",
    "taskset -c 0-3 heavy -- make",
    "chrt -f 10 heavy -- make",
    "flock /tmp/lock heavy -- make",
    'echo "unclosed; heavy -- cargo test',
]


@pytest.mark.parametrize("command", WAITS_ON_HEAVY)
def test_a_foreground_call_that_waits_on_heavy_is_refused(command):
    code, err = run_hook(command)

    assert code == 2
    assert "bgrun --wait -o" in err and "run_in_background: true" in err
    assert "heavy --quick -- CMD" in err
    assert len(err.splitlines()) <= 2
    assert err.rstrip().endswith(NOTHING_RAN)


@pytest.mark.parametrize(
    "command",
    [
        "heavy -- cargo test",
        "heavy --quick -- pytest -q test_x.py",
        "heavy -- bgrun -o log -- make",
    ],
)
def test_a_subagent_is_told_to_wait_in_the_foreground(command):
    """A subagent's prompt cache lasts five minutes, so it blocks on the job rather than
    ending its turn to wait for a background call's notification."""
    code, err = run_hook(command, agent="a1")

    assert code == 2
    assert "bgrun -o <scratchpad>/LOG -- heavy -- CMD" in err
    assert "waitone --pid PID -t 170" in err
    assert "bgrun --wait" not in err and "run_in_background" not in err
    assert err.rstrip().endswith(NOTHING_RAN)


def test_a_subagents_quick_run_with_the_longest_timeout_passes():
    assert (
        run_hook("heavy --quick -- pytest -q test_x.py", timeout=600000, agent="a1")[0]
        == 0
    )


WAITS_ON_QUICK = [
    "heavy --quick -- cargo nextest run --cargo-quiet -p server -E 'test(/^read::/)'",
    "cd crates/server && heavy --quick -- cargo nextest run -E 'test(x)'",
    "heavy --qu -- pytest -q test_x.py",
    "timeout 600 heavy --quick -- make t",
    "bgrun --wait -o log -- heavy --quick -- pytest -q test_x.py",
    "bash -c 'heavy --quick -- pytest -q test_x.py'",
]


@pytest.mark.parametrize("command", WAITS_ON_QUICK)
@pytest.mark.parametrize("timeout", [600000, "600000"])
def test_a_quick_run_passes_in_the_foreground_with_the_longest_timeout(
    command, timeout
):
    assert run_hook(command, timeout=timeout) == (0, "")


def assert_refused_for_its_timeout(code, err):
    assert code == 2
    assert "timeout parameter to 600000" in err and "run_in_background: true" in err
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize("command", WAITS_ON_QUICK)
def test_a_quick_run_in_the_foreground_without_a_timeout_is_refused(command):
    assert_refused_for_its_timeout(*run_hook(command))


@pytest.mark.parametrize(
    "timeout", [120000, 599999, "ten minutes", "²", [600000], True, 10**400]
)
def test_a_quick_run_with_a_shorter_or_unreadable_timeout_is_refused(timeout):
    assert_refused_for_its_timeout(
        *run_hook("heavy --quick -- make t", timeout=timeout)
    )


@pytest.mark.parametrize("timeout", [None, 600000])
def test_a_plain_heavy_beside_a_quick_one_gets_the_plain_refusal(timeout):
    code, err = run_hook(
        "heavy --quick -- make t && heavy -- make all", timeout=timeout
    )

    assert code == 2
    assert "bgrun --wait -o" in err


@pytest.mark.parametrize("command", WAITS_ON_HEAVY + WAITS_ON_QUICK)
@pytest.mark.parametrize("background", [True, "true"])
def test_the_same_call_in_the_background_passes(command, background):
    assert run_hook(command, background=background) == (0, "")


@pytest.mark.parametrize("background", [False, None])
def test_a_call_not_marked_background_is_refused(background):
    assert run_hook("heavy -- make", background=background)[0] == 2


WRONG_ORDER = [
    "heavy -- bgrun -o log -- cargo test",
    "heavy --long -- bgrun --wait -o log -- cargo test",
    "heavy -- timeout 5 /usr/local/bin/bgrun -o log -- make",
    "heavy --quick -- bgrun -o log -- make",
]


@pytest.mark.parametrize("command", WRONG_ORDER)
@pytest.mark.parametrize("background", [True, None])
def test_heavy_around_bgrun_is_refused_anywhere(command, background):
    code, err = run_hook(command, background=background)

    assert code == 2
    assert "inside bgrun" in err and "bgrun --wait -o" in err


RUN_BARE = [
    "heavy -- wt-ship -C ../x --local --remove",
    "heavy -- git -C repo commit -m x",
    "heavy -- git --config-env x=Y commit -m x",
    "heavy --quick -- git commit -m x",
    "heavy --quick -- wt-ship -C ../x --local --remove",
]


@pytest.mark.parametrize("command", RUN_BARE)
@pytest.mark.parametrize("background", [True, None])
def test_heavy_around_a_self_queueing_command_is_refused_anywhere(command, background):
    code, err = run_hook(command, background=background)

    assert code == 2
    assert "bare" in err


@pytest.mark.parametrize(
    "command",
    [
        "heavy status",
        "heavy -h",
        "heavy --help",
        "heavy --long --he",
        "heavy",
        "echo a; heavy status | head",
        "grep -rn heavy bin/",
        'echo "heavy -- cargo test"',
        "git commit -m 'run heavy -- cargo test first'",
        "echo '$(heavy -- cargo test)'",
        "cat > notes.md <<'EOF'\nheavy -- cargo test\nEOF",
        "apply-edits <<EOF\n--- a\n<<<<<<< SEARCH\nheavy -- x\n=======\nheavy -- y\n>>>>>>> REPLACE\nEOF",
        "# heavy -- cargo test\necho hi",
        "echo hi # then heavy -- cargo test",
        "ls /tmp/heavy-queue-race && pytest -q test_heavy.py -k heavy",
        "ls ~/.local/bin/heavy",
        "command -v heavy",
        "bgrun -o log -- heavy -- cargo test",
        "bgrun -o log -- heavy --quick -- pytest -q test_x.py",
        "pid=$(bgrun -o log -C ~/x -- heavy -- cargo test) && echo $pid",
        "HEAVY_DIR=$(mktemp -d) HEAVY_BUSY=b bin/heavy -- true",
        "env HEAVY_SLOT=x heavy -- true",
        "gh pr create --body $'Fixed (heavy -- bgrun ...) order'",
        "gh pr create --body $'it\\'s (heavy -- bgrun -o l -- x)'",
        "git commit -m \"$(cat <<'EOF'\nDon't run heavy -- cargo test here\nEOF\n)\"",
        "git commit -m x",
        "wt-ship -C ../x --local --remove",
        "x=$((1<<3))\necho done",
        "x=\"$(cat <<'EOF'\nit's\nEOF\n)\"; echo 'x;heavy -- make'",
    ],
)
def test_a_call_that_does_not_wait_on_heavy_passes(command):
    assert run_hook(command) == (0, "")


def test_other_tools_pass():
    assert run_hook("heavy -- make", tool="Write") == (0, "")


def test_input_that_is_not_json_fails_in_one_line():
    result = subprocess.run(
        [SCRIPT], input="{not json", capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert len(result.stderr.splitlines()) == 1


def test_help_prints_the_docstring():
    result = subprocess.run([SCRIPT, "-h"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "run_in_background" in result.stdout
