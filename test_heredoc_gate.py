"""bin/heredoc-gate: it refuses a command whose heredoc closed before its writer meant
it to, and lets every other command through."""

import importlib.machinery
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

from conftest import saved_command

SCRIPT = Path(__file__).resolve().parent / "bin" / "heredoc-gate"
loader = importlib.machinery.SourceFileLoader("heredoc_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader("heredoc_gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)

NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."


def run_hook(monkeypatch, capsys, command, tool="Bash", session=None):
    event = {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/r"}
    event |= session or {}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = gate.main([])
    return code, capsys.readouterr().err


# The case that made it: edit blocks for a test file that holds a heredoc of its own.
CUT_SHORT = """\
apply-edits <<'EOF'
--- test_x.py
<<<<<<< SEARCH
old
=======
SCRIPT = \"\"\"
cat <<'EOF'
a > b
EOF
diff <(one) >(two)
\"\"\"
>>>>>>> REPLACE
EOF
"""


@pytest.mark.parametrize(
    "command",
    [
        CUT_SHORT,
        "cat > f <<EOF\nline\nEOF\nmore body\nEOF\n",
        "cat > f <<-'END'\n\tline\n\tEND\nleft over\n\tEND\n",
        "cat <<'X' | sh\necho hi\nX\n=======\n",
        "python3 - <<'PY'\nprint(1)\nPY\n>>>>>>> REPLACE\n",
    ],
    ids=["edit-blocks", "plain", "dash", "marker-after", "replace-after"],
)
def test_a_heredoc_cut_short_is_refused(monkeypatch, capsys, command):
    code, err = run_hook(monkeypatch, capsys, command)

    assert code == 2, err
    assert "heredoc" in err
    assert err.rstrip().endswith(NOTHING_RAN)


def test_the_refusal_names_the_delimiter_and_how_to_pick_another(monkeypatch, capsys):
    _, err = run_hook(monkeypatch, capsys, CUT_SHORT)
    _, marker = run_hook(monkeypatch, capsys, "cat <<'X'\necho hi\nX\n=======\n")

    assert "holds only 'EOF'" in err
    assert "<<'BLOCKS'" in err
    assert "'X'" in marker and "<<'BLOCKS'" in marker


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\none\nEOF\ncat <<EOF\ntwo\nEOF\n",
        "cat <<'EOF'\nthe EOF of the file\n  not EOF alone\nEOF\n",
        "cat <<-END\n\tbody\n\tEND\n",
        "echo EOF",
        "ls -la && git status",
        "apply-edits <<'BLOCKS'\n--- f\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\nBLOCKS\n",
        "echo $(( 1 << 2 ))",
        "git commit -m 'subject\n\nbody'",
        'grep -q x <<<"$text"',
        # A heredoc inside a quoted script is text of the string, not lines of shell.
        (
            "apply-edits <<'X'\n=======\nX\n"
            "bash -c \"\nmutate-check -- t <<'M'\n<<<<<<< SEARCH\na\n=======\nb\n"
            '>>>>>>> REPLACE\nM\n"\n'
        ),
        # Bash closes << only on the delimiter exactly, <<- after its leading tabs:
        # an indented one, as in a docstring's example, stays in the body.
        (
            "apply-edits <<'EDITS'\n--- f\n<<<<<<< SEARCH\n  EDITS\n=======\n  EOF\n"
            ">>>>>>> REPLACE\nEDITS\n"
        ),
        # A quote that closed holds no shell, though one left open at the end does.
        'cat <<EOF\nx\nEOF\ngit commit -m "one\nEOF\n"\necho "open',
        "cat <<-END\n\tbody\n  END\n\tEND\n",
        "cat <<EOF\nbody\nEOF \nEOF\n",
    ],
    ids=[
        "two-heredocs",
        "mentions",
        "dash",
        "echo",
        "no-heredoc",
        "edit-blocks",
        "shift",
        "multiline-quote",
        "here-string",
        "heredoc-in-quoted-script",
        "indented-delimiter-in-body",
        "dash-space-indented",
        "trailing-space",
        "delimiter-in-a-closed-quote",
    ],
)
def test_an_honest_command_passes(monkeypatch, capsys, command):
    code, err = run_hook(monkeypatch, capsys, command)

    assert code == 0, err
    assert err == ""


def test_another_tool_passes(monkeypatch, capsys):
    code, _ = run_hook(monkeypatch, capsys, CUT_SHORT, tool="Read")

    assert code == 0


def test_a_refused_command_is_saved_for_reuse(monkeypatch, capsys, refused_session):
    fields, folder = refused_session

    code, err = run_hook(monkeypatch, capsys, CUT_SHORT, session=fields)

    assert code == 2
    assert saved_command(err, folder) == CUT_SHORT


def test_help_says_what_it_refuses(capsys):
    assert gate.main(["-h"]) == 0
    assert "heredoc" in capsys.readouterr().out


def test_an_escaped_quote_keeps_a_later_quoted_script_quoted(monkeypatch, capsys):
    command = (
        'echo "a \\" b"\n'
        "apply-edits <<'X'\n=======\nX\n"
        "bash -c \"\nt <<'M'\n>>>>>>> REPLACE\nM\n\"\n"
    )

    code, err = run_hook(monkeypatch, capsys, command)

    assert code == 0, err


def as_hook(command: str, *args: str) -> subprocess.CompletedProcess:
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/r"}
    return subprocess.run(
        [str(SCRIPT), *args],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=False,
    )


def test_as_claude_code_runs_it_a_cut_short_heredoc_exits_2():
    refused = as_hook(CUT_SHORT)
    passed = as_hook("echo hi")

    assert refused.returncode == 2 and "heredoc" in refused.stderr
    assert passed.returncode == 0 and passed.stderr == ""


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_its_help_names_what_it_refuses(flag):
    done = subprocess.run(
        [str(SCRIPT), flag], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0
    assert "heredoc" in done.stdout
