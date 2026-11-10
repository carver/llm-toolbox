"""bin/gh-body-gate: which gh issue and pr posts it refuses for a body slop-lint flags,
and what it lets through."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "gh-body-gate"
DASH = "\N{EN DASH}"
SLOP = f"Fixed it {DASH} mostly.\n"
CLEAN = "Fixed it, mostly.\n"
WAY_OUT = "<!-- slop-lint: ignore -->"
# A gh that answers `view` with the file `current` beside it, when there is one, logs
# every call to `calls`, and fails at everything else, so no test reaches GitHub.
FAKE_GH = """#!/bin/sh
here=$(dirname "$0")
printf '%s\\n' "$*" >> "$here/calls"
case "$2" in view) [ -f "$here/current" ] && exec cat "$here/current" ;; esac
exit 1
"""


# What every gate's refusal ends with: a refused call runs none of its parts.
NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."


class Gate:
    """Runs the hook from a folder of its own, with the event's cwd at ``work``."""

    def __init__(self, tmp_path: Path):
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()

    def write(self, name: str, text: str) -> Path:
        path = self.work / name
        path.write_text(text, encoding="utf-8")
        return path

    def current_body(self, text: str):
        (self.bin / "current").write_text(text, encoding="utf-8")

    def gh_calls(self) -> list[str]:
        calls = self.bin / "calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def fake_slop_lint(self, script: str) -> str:
        """A slop-lint that runs ``script`` in sh, for $GH_BODY_GATE_SLOP_LINT."""
        fake = self.bin / "slop-lint"
        fake.write_text("#!/bin/sh\n" + script)
        fake.chmod(0o755)
        return str(fake)

    def run(self, command, tool="Bash", cwd="work", env=None):
        """The hook's exit status and stderr for one call, its input shaped as Claude
        Code sends it. ``cwd``: "work" for the work folder, None for no cwd field;
        ``env``: variables to set for the hook."""
        event = {
            "session_id": "s",
            "transcript_path": "/t.jsonl",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"command": command, "description": "Post it"},
        }
        if cwd is not None:
            event["cwd"] = str(self.work) if cwd == "work" else cwd
        path = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        result = subprocess.run(
            [SCRIPT],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=self.elsewhere,
            env={**os.environ, "PATH": path, **(env or {})},
        )
        return result.returncode, result.stderr


@pytest.fixture
def gate(tmp_path):
    return Gate(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 5 --body-file body.md",
        "gh issue comment 5 -F body.md",
        "gh issue comment 5 -Fbody.md",
        "gh issue comment 5 -F=body.md",
        "gh issue comment 5 --body-file=body.md",
        "gh issue comment -R owner/repo 5 -F body.md",
        "gh issue -R owner/repo comment 5 -F body.md",
        "gh issue create -t 'A title' -F body.md",
        "gh issue new -t 'A title' -F body.md",
        "gh issue edit 5 -F body.md",
        "gh pr create -t 'A title' -B main -F body.md",
        "gh pr create -dF body.md -t x",
        "gh pr comment 7 -F body.md",
        "gh pr edit 7 -F body.md",
        "gh issue edit 5 --remove-milestone --remove-parent --remove-type -F body.md",
        "gh pr edit 7 --remove-milestone -F body.md",
        "/usr/bin/gh issue comment 5 -F body.md",
        "GH_REPO=o/r timeout 30 gh issue comment 5 -F body.md",
        "git push && gh pr create -t x -F body.md",
        'url=$(gh pr create -t x -F body.md) && echo "$url"',
        "bash -c 'gh issue comment 5 -F body.md'",
        "eval 'gh issue comment 5 -F body.md'",
        "bash <<'EOF'\ngh issue comment 5 -F body.md\nEOF",
        "bgrun --wait -o log -- gh issue comment 5 -F body.md",
        "gh issue comment 5 -F body.md && rm body.md",
        "slop-lint body.md; gh issue comment 5 -F body.md",
        "cat body.md && gh issue comment 5 -F body.md # body.md > x",
        "echo posting body.md && printf '%s\\n' body.md && gh issue comment 5 -F body.md",
    ],
)
def test_a_body_file_slop_lint_flags_is_refused(gate, command):
    path = gate.write("body.md", "Intro.\n\n" + SLOP)

    code, err = gate.run(command)

    assert code == 2
    lines = err.splitlines()
    assert "gh " in lines[0] and "slop-lint" in lines[0]
    assert f"{path}:3:10: dash: {DASH}" in lines[1]
    assert WAY_OUT in lines[-1] and "Fix the text" in lines[-1]
    assert lines[-1].endswith(NOTHING_RAN)
    assert len(lines) == 3


def test_an_absolute_or_home_path_is_read_wherever_the_command_runs(gate):
    path = gate.write("body.md", SLOP)

    assert gate.run(f"cd /tmp && gh issue comment 5 -F {path}")[0] == 2
    assert gate.run(f"gh issue comment 5 -F {path}", cwd=None)[0] == 2
    tilde = "cd /tmp && gh issue comment 5 -F ~/work/body.md"
    assert gate.run(tilde, env={"HOME": str(gate.work.parent)})[0] == 2


def test_every_finding_is_shown(gate):
    gate.write("body.md", f"One {DASH} two.\nIt serves as a test {DASH} really.\n")

    code, err = gate.run("gh issue comment 5 -F body.md")

    assert code == 2
    findings = err.splitlines()[1:-1]
    assert [line.split(": ")[1] for line in findings] == ["dash", "fancy-is", "dash"]


@pytest.mark.parametrize(
    "command",
    [
        # Clean, or only text slop-lint skips.
        "gh issue comment 5 -F clean.md",
        "gh issue comment 5 -F ignored.md",
        "gh issue comment 5 -F code.md",
        # Not a file the gate can read, or can find.
        "gh issue comment 5 -F missing.md",
        "gh issue comment 5 -F folder",
        "gh issue comment 5 -F latin1.md",
        'gh issue comment 5 -F "$BODY"',
        "gh issue comment 5 -F $(ls body.md)",
        "gh issue comment 5 -F <(cat body.md)",
        "cd sub && gh issue comment 5 -F body.md",
        "pushd sub; gh issue comment 5 -F body.md",
        "env -C sub gh issue comment 5 -F body.md",
        "sudo -D sub gh issue comment 5 -F body.md",
        "bgrun --wait -C sub -o log -- gh issue comment 5 -F body.md",
        # Not a post the gate judges.
        "gh issue view 5",
        "gh issue list --search body.md",
        "gh pr review 7 --comment -F body.md",
        "gh release create v1 -F body.md",
        "gh api -X PATCH repos/o/r/issues/comments/1 -F body=@body.md",
        "gh issue comment 5 -F body.md -h",
        "gh issue comment --help -F body.md",
        "gh issue comment 5 -- -F body.md",
        "echo gh issue comment 5 -F body.md",
        "grep -n 'gh issue comment 5 -F body.md' notes.md",
        "# gh issue comment 5 -F body.md\necho hi",
        "cat > notes.md <<'EOF'\ngh issue comment 5 -F body.md\nEOF",
        "gh issue comment 5 -t -F body.md",
        # A quote left open: the command can't be parsed, so it passes.
        "gh issue comment 5 -F body.md -b 'it",
    ],
)
def test_a_body_file_the_gate_cant_fault_passes(gate, command):
    gate.write("body.md", SLOP)
    gate.write("clean.md", CLEAN)
    gate.write("ignored.md", f"Fixed it {DASH} mostly. {WAY_OUT}\n")
    gate.write("code.md", f"Run `a{DASH}b`.\n\n```\nx {DASH} y\n```\n")
    (gate.work / "folder").mkdir()
    (gate.work / "latin1.md").write_bytes("Caf\xe9 \x96 x\n".encode("latin-1"))
    (gate.work / "sub").mkdir()

    assert gate.run(command) == (0, "")


def test_a_relative_path_passes_without_a_cwd_to_resolve_it(gate):
    gate.write("body.md", SLOP)

    assert gate.run("gh issue comment 5 -F body.md", cwd=None) == (0, "")


def test_a_fifo_body_file_passes_without_blocking(gate):
    os.mkfifo(gate.work / "pipe")

    assert gate.run("gh issue comment 5 -F pipe") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        f"gh issue comment 5 --body 'Fixed it {DASH} mostly.'",
        f'gh issue comment 5 -b "Fixed it {DASH} mostly."',
        f"gh issue comment 5 --body='Fixed it {DASH} mostly.'",
        f"gh issue comment 5 -b'Fixed it {DASH} mostly.'",
        f"gh pr create -t x -wb 'Fixed it {DASH} mostly.'",
        f"gh pr create -t '-b is gone' -b 'Fixed it {DASH} mostly.'",
        f'gh issue comment 5 -b "Fixed it {DASH} mostly. $(git log -1 --format=%h)"',
        f'gh issue comment 5 -b "Fixed it {DASH} mostly. $NOTE"',
        # Claude Code's own form for a PR body.
        f"gh pr create -t x --body \"$(cat <<'EOF'\nFixed it {DASH} mostly.\n\nDon't worry.\nEOF\n)\"",
        f'gh pr create -t x --body "$(cat <<EOF\nFixed it {DASH} mostly.\nEOF\n)"',
        'gh issue comment 5 --body "$(cat body.md)"',
        'gh issue comment 5 --body "$(< body.md)"',
        'sh -c "gh issue comment 5 --body \\"$(cat body.md)\\""',
        f"gh issue comment 5 -F - <<'EOF'\nFixed it {DASH} mostly.\nEOF",
        f"gh issue comment 5 -F /dev/stdin <<'EOF'\nFixed it {DASH} mostly.\nEOF",
        f"cat <<'EOF' | gh issue comment 5 -F -\nFixed it {DASH} mostly.\nEOF",
        f"out=$(gh issue comment 5 -F - <<'EOF'\nFixed it {DASH} mostly.\nEOF\n)",
    ],
)
def test_an_inline_or_stdin_body_slop_lint_flags_is_refused(gate, command):
    gate.write("body.md", SLOP)

    code, err = gate.run(command)

    assert code == 2
    lines = err.splitlines()
    assert lines[1].startswith("body:1:10: dash:")
    assert WAY_OUT in lines[-1]


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 5 --body 'Fixed it, mostly.'",
        "gh issue comment 5 -b '- [x] done, mostly.'",
        f"gh issue comment 5 -b 'Fixed it {DASH} mostly. {WAY_OUT}'",
        f"gh issue comment 5 -b 'Run `a{DASH}b` first.'",
        f"gh issue comment 5 -t 'Fixed {DASH} mostly' -b 'Fixed it.'",
        'gh issue comment 5 -b "$(./make-body)"',
        'gh issue comment 5 -b "$NOTE"',
        "gh issue comment 5 --body \"$(cat <<'EOF'\nFixed it, mostly.\nEOF\n)\"",
        # Stdin the gate can't see, or a filter between the heredoc and gh.
        "cat body.md | gh issue comment 5 -F -",
        f"cat <<'EOF' | sed 's/{DASH}/-/' | gh issue comment 5 -F -\n{SLOP}EOF",
        f"gh issue comment 5 -F - <<< 'Fixed it {DASH} mostly.'",
        # The text slop-lint skips isn't held against an inline body either.
        f"gh issue comment 5 -b \"$(cat <<'EOF'\n```\nx {DASH} y\n```\nEOF\n)\"",
        f"gh pr review 7 --comment -b 'Fixed it {DASH} mostly.'",
        f"gh api repos/o/r/issues/5/comments -f body='Fixed it {DASH} mostly.'",
        f"echo \"gh issue comment 5 -b 'Fixed it {DASH} mostly.'\"",
    ],
)
def test_an_inline_or_stdin_body_the_gate_cant_fault_passes(gate, command):
    gate.write("body.md", SLOP)

    assert gate.run(command) == (0, "")


def test_a_heredoc_that_writes_the_body_file_is_judged_instead_of_the_stale_file(gate):
    gate.write("body.md", CLEAN)
    write_and_post = (
        f"cat > body.md <<'EOF'\nIntro.\n{SLOP}EOF\ngh issue comment 5 -F body.md"
    )

    code, err = gate.run(write_and_post)

    assert code == 2
    assert err.splitlines()[1].startswith("body.md:2:10: dash:")
    gate.write("body.md", SLOP)
    fixed = f"cat > body.md <<'EOF'\n{CLEAN}EOF\ngh issue comment 5 -F body.md"
    assert gate.run(fixed) == (0, "")
    moved = f"cd sub && cat > body.md <<'EOF'\n{SLOP}EOF\ngh issue comment 5 -F body.md"
    assert gate.run(moved)[0] == 2


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/x/y/' body.md && gh issue comment 5 -F body.md",
        "cp draft.md body.md && gh issue comment 5 -F body.md",
        "printf 'Fixed.\\n' > body.md; gh issue comment 5 -F body.md",
        "echo more >> body.md; gh issue comment 5 -F body.md",
        "bash -c 'printf x > body.md' && gh issue comment 5 -F body.md",
        "bgrun --wait -o log -- cp draft.md body.md; gh issue comment 5 -F body.md",
        "apply-edits <<'EOF'\n--- body.md\nx\nEOF\ngh issue comment 5 -F body.md",
        (
            "cat > body.md <<'EOF'\nFixed.\nEOF\nsed -i 's/x/y/' body.md\n"
            "gh issue comment 5 -F body.md"
        ),
        'gh issue comment 5 --body "$(sed -n 1p body.md; cat body.md)"',
        # A heredoc written into another file, whose name holds this one's, isn't
        # the body.
        f"cat > notes-body.md <<'EOF'\n{SLOP}EOF\ngh issue comment 5 -F body.md",
    ],
)
def test_a_body_file_the_same_call_may_rewrite_passes(gate, command):
    gate.write("body.md", SLOP)
    gate.write("draft.md", CLEAN)

    assert gate.run(command) == (0, "")


def test_an_edit_is_judged_only_on_lines_the_current_body_lacks(gate):
    owner = f"Plan {DASH} the owner's words.\n- [ ] step one {DASH} detail\n"
    gate.current_body(owner)
    ticked = f"Plan {DASH} the owner's words.\n- [x] step one {DASH} detail\n"
    gate.write("body.md", ticked)

    assert gate.run("gh issue edit 16 -R o/r -F body.md") == (0, "")
    assert gate.gh_calls() == ["issue view 16 --repo o/r --json body --jq .body"]

    gate.write("body.md", owner + f"- [ ] step two {DASH} new\n")
    code, err = gate.run("gh issue edit 16 -F body.md")

    assert code == 2
    assert [line.split(":")[1] for line in err.splitlines()[1:-1]] == ["3"]


def test_an_edit_whose_current_body_cant_be_fetched_is_judged_whole(gate):
    gate.write("body.md", f"Plan {DASH} the owner's words.\n")

    assert gate.run("gh pr edit -F body.md")[0] == 2
    assert gate.gh_calls() == ["pr view --json body --jq .body"]


def test_a_comment_is_judged_whole_without_asking_github(gate):
    gate.current_body(SLOP)
    gate.write("body.md", SLOP)

    assert gate.run("gh issue comment 5 --edit-last -F body.md")[0] == 2
    assert gate.gh_calls() == []


def test_the_refusal_names_the_first_flagged_post_and_shows_each_body(gate):
    gate.write("one.md", SLOP)
    gate.write("two.md", f"It serves as a fix.\n{CLEAN}")
    command = (
        "gh issue comment 5 -F clean.md && gh pr comment 7 -F one.md "
        "&& gh issue comment 6 -F two.md"
    )
    gate.write("clean.md", CLEAN)

    code, err = gate.run(command)

    assert code == 2
    lines = err.splitlines()
    assert "gh pr comment" in lines[0]
    assert lines[1].startswith(f"{gate.work / 'one.md'}:1:10: dash:")
    assert lines[2].startswith(f"{gate.work / 'two.md'}:1:4: fancy-is:")
    assert len(lines) == 4


@pytest.mark.parametrize(
    "script",
    [
        # A crash: Python's traceback, exit 1, no finding lines.
        "echo 'Traceback (most recent call last):' >&2\nexit 1\n",
        # Output in a shape the hook doesn't know.
        "echo 'body.md line 1: dash'\nexit 1\n",
        "echo '-:1:10: dash: x'\nexit 2\n",
        "exit 0\n",
    ],
)
def test_slop_lint_failing_in_any_way_but_a_finding_passes(gate, script):
    gate.write("body.md", SLOP)
    fake = gate.fake_slop_lint(script)

    assert gate.run(
        "gh issue comment 5 -F body.md", env={"GH_BODY_GATE_SLOP_LINT": fake}
    ) == (0, "")


def test_the_slop_lint_the_variable_names_judges_the_body(gate):
    gate.write("body.md", CLEAN)
    fake = gate.fake_slop_lint("cat > /dev/null\necho '-:1:3: dash: x'\nexit 1\n")

    code, err = gate.run(
        "gh issue comment 5 -F body.md", env={"GH_BODY_GATE_SLOP_LINT": fake}
    )

    assert code == 2
    assert err.splitlines()[1] == f"{gate.work / 'body.md'}:1:3: dash: x"


def test_a_slop_lint_that_hangs_passes_at_the_deadline_with_its_children_stopped(
    gate,
):
    gate.write("body.md", SLOP)
    # sh waits on a child sleep, which would keep the output pipe open if left.
    fake = gate.fake_slop_lint("sleep 60\n")
    started = time.monotonic()

    result = gate.run(
        "gh issue comment 5 -F body.md",
        env={"GH_BODY_GATE_SLOP_LINT": fake, "GH_BODY_GATE_DEADLINE": "1"},
    )

    assert result == (0, "")
    assert time.monotonic() - started < 20


def test_other_tools_pass(gate):
    gate.write("body.md", SLOP)

    assert gate.run("gh issue comment 5 -F body.md", tool="Write") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        ")",
        "(((",
        "'",
        "$(",
        "<<",
        "<(",
        "`",
        "gh",
        "gh issue",
        "gh issue comment",
        "gh issue comment 5 -F",
        "gh issue comment 5 -b",
        "gh issue comment 5 --body",
        "gh issue comment 5 -F -",
        "gh pr create -b 7",
        "gh issue comment 5 -F - <<",
        "gh issue comment 5 -b \"$(cat <<'EOF'",
        'gh issue comment 5 -b "$("',
        "gh issue comment 5 -b $(< )",
        "cat > <<EOF\nx\nEOF\ngh issue comment 5 -F body.md",
        "\\",
    ],
)
def test_odd_shell_never_crashes_the_hook(gate, command):
    gate.write("body.md", SLOP)

    assert gate.run(command)[0] in (0, 2)


def test_input_that_is_not_json_fails_in_one_line():
    result = subprocess.run(
        [SCRIPT], input="{not json", capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert len(result.stderr.splitlines()) == 1


def test_help_prints_the_docstring():
    result = subprocess.run([SCRIPT, "-h"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "slop-lint" in result.stdout and "--body-file" in result.stdout
