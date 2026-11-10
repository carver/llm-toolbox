"""bin/agent-commit-gate: the --no-verify refusal, the installer, and real commits through the hook line."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import path_without, wait_or_warn, wait_until, without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "agent-commit-gate"
loader = importlib.machinery.SourceFileLoader("agent_commit_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader("agent_commit_gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)
import fifo  # from lib/, which loading the gate put on the path
import passrecord


def run_hook(monkeypatch, capsys, command, tool="Bash"):
    event = {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/r"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = gate.main([])
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "git commit -n -m x",
        "git commit -nm x",
        "git commit -anm x",
        "git -C sub commit -q --no-verify",
        "git add a && git commit -m x --no-verify",
        "cd sub; /usr/bin/git commit -n",
        "git commit --no-verif -m x",
        "git --git-dir .git commit -n",
    ],
)
def test_skipping_the_hook_is_refused(monkeypatch, capsys, command):
    code, err = run_hook(monkeypatch, capsys, command)
    assert code == 2 and "--no-verify" in err
    assert err.rstrip().endswith(
        "Nothing in this command ran, the parts before the refused one included."
    )


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git commit -m -n",
        "git commit -m 'skip with --no-verify'",
        "git commit -F - <<'EOF'\ngit commit --no-verify\nEOF",
        "git commit -Snkey -m x",
        "git add -n . && git clean -n",
        "git push --no-verify",
        "echo git commit -n",
        "git commit --help",
    ],
)
def test_other_commands_pass(monkeypatch, capsys, command):
    assert run_hook(monkeypatch, capsys, command) == (0, "")


def test_other_tools_pass(monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "git commit -n", tool="Edit") == (0, "")


def git(repo, *args, env=None, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env or without_repo_vars(os.environ),
    )


def make_repo(path):
    git(path, "init", "-q")
    git(path, "config", "core.hooksPath", ".githooks")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@t")
    (path / ".githooks").mkdir()
    return path


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A repo whose hooks live in .githooks, made once: each test copies it."""
    return make_repo(tmp_path_factory.mktemp("template"))


@pytest.fixture
def repo(template, tmp_path):
    shutil.copytree(template, tmp_path, symlinks=True, dirs_exist_ok=True)
    return tmp_path


def hook(repo, name, body):
    path = repo / ".githooks" / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def install(*repos):
    return subprocess.run(
        [str(SCRIPT), "--install", *map(str, repos)],
        capture_output=True,
        text=True,
        check=False,
        env=without_repo_vars(os.environ),
    )


def commit(repo, *args, agent=True, gate_on_path=True, env=None):
    env = without_repo_vars(os.environ) | (env or {})
    # The hook line calls agent-commit-gate from PATH: this checkout's, not the installed one.
    env["PATH"] = path_without(SCRIPT.name, env["PATH"])
    if gate_on_path:
        env["PATH"] = os.pathsep.join([str(SCRIPT.parent), env["PATH"]])
    if agent:
        env["AI_AGENT"] = "test-agent"
    return git(repo, "commit", "-q", "-m", "x", *args, env=env, check=False)


def test_install_puts_the_line_after_the_shebang_once(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = hook(repo, "pre-commit", "set -e\necho fast")
    assert install(repo).returncode == 0
    assert install(repo).returncode == 0
    lines = pre.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[-2:] == ["set -e", "echo fast"]
    assert pre.read_text().count("AI_AGENT") == 1


def test_install_keeps_a_disabled_pre_commit_disabled(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = hook(repo, "pre-commit", "exit 1")
    pre.chmod(0o644)
    assert install(repo).returncode == 0
    assert not os.access(pre, os.X_OK)


def test_install_handles_a_shebang_without_a_newline(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = repo / ".githooks" / "pre-commit"
    pre.write_text("#!/bin/sh")
    pre.chmod(0o755)
    assert install(repo).returncode == 0
    assert pre.read_text().splitlines()[0] == "#!/bin/sh"


def test_install_refuses_a_pre_commit_that_is_not_a_shell_script(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = repo / ".githooks" / "pre-commit"
    pre.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    run = install(repo)
    assert run.returncode == 1 and "shell" in run.stderr
    assert "AI_AGENT" not in pre.read_text()


@pytest.mark.parametrize(
    "path, line",
    [
        (".githooks/pre-commit", gate.LINE),
        ("templates/egui-app/.githooks/pre-commit", gate.LINE),
        (".githooks/post-commit", gate.POST_LINE),
        ("templates/egui-app/.githooks/post-commit", gate.POST_LINE),
    ],
    ids=["pre-commit", "egui pre-commit", "post-commit", "egui post-commit"],
)
def test_checked_in_hooks_carry_the_current_line(path, line):
    assert line in (SCRIPT.parent.parent / path).read_text()


# The comment --install wrote above its line before this one, and those lines, oldest
# first.
OLD_COMMENT = "# Agents' commits also run the slower agent-pre-commit (llm-toolbox agent-commit-gate)."
OLD_LINES = {
    "before --run": (
        'if [ -n "${AI_AGENT:-}" ] && [ -z "${AGENT_PRE_COMMIT:-}" ]; then '
        'AGENT_PRE_COMMIT=1 "$(dirname "$0")/agent-pre-commit" || exit; fi'
    ),
    "before the refusal": (
        'if [ -n "${AI_AGENT:-}" ] && [ -z "${AGENT_PRE_COMMIT:-}" ]; then '
        "if command -v agent-commit-gate >/dev/null; then "
        'AGENT_PRE_COMMIT=1 agent-commit-gate --run "$(dirname "$0")/agent-pre-commit"; '
        'else AGENT_PRE_COMMIT=1 "$(dirname "$0")/agent-pre-commit"; fi || exit; fi'
    ),
    "before the rerun": (
        'if [ -n "${AI_AGENT:-}" ] && [ -z "${AGENT_PRE_COMMIT:-}" ]; then '
        "if command -v agent-commit-gate >/dev/null; then "
        'AGENT_PRE_COMMIT=1 agent-commit-gate --run "$(dirname "$0")/agent-pre-commit" || exit; '
        'else AGENT_PRE_COMMIT=1 "$(dirname "$0")/agent-pre-commit" || '
        '{ echo "COMMIT REFUSED: agent-pre-commit failed" >&2; exit 1; }; fi; fi'
    ),
    "before the commit mark": (
        'if [ -n "${AI_AGENT:-}" ] && [ -z "${AGENT_PRE_COMMIT:-}" ]; then '
        'export AGENT_PRE_COMMIT=1; "$0" || exit; '
        "if command -v agent-commit-gate >/dev/null; then "
        'agent-commit-gate --run "$(dirname "$0")/agent-pre-commit"; '
        'else "$(dirname "$0")/agent-pre-commit" || '
        '{ echo "COMMIT REFUSED: agent-pre-commit failed" >&2; exit 1; }; fi; exit; fi'
    ),
}


@pytest.mark.parametrize("where", ["top", "bottom"])
@pytest.mark.parametrize("old", OLD_LINES.values(), ids=OLD_LINES.keys())
def test_install_puts_an_old_lines_successor_at_the_top(repo, old, where):
    """The line runs the whole hook again, so whatever sits above it would run twice."""
    hook(repo, "agent-pre-commit", "exit 0")
    lines, body = [OLD_COMMENT, old], ["set -e", "echo fast"]
    pre = hook(
        repo, "pre-commit", "\n".join(lines + body if where == "top" else body + lines)
    )

    run = install(repo)

    assert run.returncode == 0, run.stderr
    assert pre.read_text().splitlines() == ["#!/bin/sh", gate.COMMENT, gate.LINE, *body]


def test_install_moves_a_line_below_a_command_to_the_top(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = hook(repo, "pre-commit", f"echo fast\n{gate.COMMENT}\n{gate.LINE}")

    run = install(repo)

    assert run.returncode == 0, run.stderr
    assert "moved" in run.stdout
    assert pre.read_text().splitlines() == [
        "#!/bin/sh",
        gate.COMMENT,
        gate.LINE,
        "echo fast",
    ]


def test_install_leaves_a_line_with_only_comments_above_it(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    text = f"#!/bin/sh\n# My checks.\n\n{gate.COMMENT}\n{gate.LINE}\necho fast\n"
    pre = repo / ".githooks" / "pre-commit"
    pre.write_text(text)
    pre.chmod(0o755)

    run = install(repo)

    assert run.returncode == 0, run.stderr
    assert "already installed" in run.stdout
    assert pre.read_text() == text


def test_install_creates_a_missing_pre_commit(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    assert install(repo).returncode == 0
    pre = repo / ".githooks" / "pre-commit"
    assert os.access(pre, os.X_OK) and "agent-pre-commit" in pre.read_text()


def test_install_uses_git_hooks_dir_without_hooks_path(tmp_path):
    git(tmp_path, "init", "-q")
    agent = tmp_path / ".git" / "hooks" / "agent-pre-commit"
    agent.write_text("#!/bin/sh\nexit 0\n")
    agent.chmod(0o755)
    assert install(tmp_path).returncode == 0
    assert "agent-pre-commit" in (tmp_path / ".git/hooks/pre-commit").read_text()


def test_install_refuses_a_repo_without_an_agent_check(repo):
    run = install(repo)
    assert run.returncode == 1 and "agent-pre-commit" in run.stderr
    assert not (repo / ".githooks" / "pre-commit").exists()


def test_install_puts_the_post_commit_line_first_once(repo):
    """First, so a post-commit that exits early, as llm-toolbox's does in a linked
    worktree, still runs it."""
    hook(repo, "agent-pre-commit", "exit 0")
    post = hook(repo, "post-commit", "exit 0")

    assert install(repo).returncode == 0
    run = install(repo)

    assert run.returncode == 0, run.stderr
    assert post.read_text().splitlines() == [
        "#!/bin/sh",
        gate.POST_COMMENT,
        gate.POST_LINE,
        "exit 0",
    ]


def test_install_creates_a_missing_post_commit(repo):
    hook(repo, "agent-pre-commit", "exit 0")

    assert install(repo).returncode == 0

    post = repo / ".githooks" / "post-commit"
    assert os.access(post, os.X_OK) and gate.POST_LINE in post.read_text()


def test_install_leaves_a_pre_commit_with_the_line_first_as_it_is(repo):
    """Whatever its shebang: `env -S` reads as no shell the installer knows."""
    hook(repo, "agent-pre-commit", "exit 0")
    pre = repo / ".githooks" / "pre-commit"
    text = f"#!/usr/bin/env -S bash -e\n{gate.COMMENT}\n{gate.LINE}\necho fast\n"
    pre.write_text(text)
    pre.chmod(0o755)

    run = install(repo)

    assert run.returncode == 0, run.stderr
    assert "already installed" in run.stdout and pre.read_text() == text


def test_a_pre_commit_that_is_not_a_shell_script_still_gets_the_post_commit_line(
    repo,
):
    hook(repo, "agent-pre-commit", "exit 0")
    (repo / ".githooks" / "pre-commit").write_text("#!/usr/bin/env python3\n")

    run = install(repo)

    assert run.returncode == 1 and "pre-commit" in run.stderr
    assert gate.POST_LINE in (repo / ".githooks" / "post-commit").read_text()


def test_install_refuses_a_post_commit_that_is_not_a_shell_script(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    post = repo / ".githooks" / "post-commit"
    post.write_text("#!/usr/bin/env python3\nprint('hi')\n")

    run = install(repo)

    assert run.returncode == 1 and "post-commit" in run.stderr
    assert "AI_AGENT" not in post.read_text()
    assert gate.LINE in (repo / ".githooks" / "pre-commit").read_text()


@pytest.fixture(scope="module")
def installed_template(tmp_path_factory):
    """A repo whose agent check fails when the committed `a` says `bad`, made once: each test copies it."""
    repo = make_repo(tmp_path_factory.mktemp("installed"))
    hook(
        repo,
        "agent-pre-commit",
        'echo agent >> "$(git rev-parse --git-dir)/ran"\n'
        "git show :a | grep -q bad && { echo 'agent check: a is bad'; exit 1; }\n"
        "exit 0",
    )
    hook(repo, "pre-commit", 'echo fast >> "$(git rev-parse --git-dir)/ran"')
    install(repo)
    (repo / "a").write_text("good\n")
    git(repo, "add", "a")
    return repo


@pytest.fixture
def installed(installed_template, tmp_path):
    shutil.copytree(installed_template, tmp_path, symlinks=True, dirs_exist_ok=True)
    return tmp_path


def ran(repo):
    path = repo / ".git" / "ran"
    return path.read_text().split() if path.exists() else []


def test_an_agent_commit_runs_the_fast_check_then_the_agent_check(installed):
    assert commit(installed).returncode == 0
    assert ran(installed) == ["fast", "agent"]


def install_fast_check(repo, body):
    """Make the repo's pre-commit note `fast`, then run `body`, with the gate's line
    installed above them."""
    hook(repo, "pre-commit", f'echo fast >> "$(git rev-parse --git-dir)/ran"\n{body}')
    assert install(repo).returncode == 0


def test_a_failing_fast_check_refuses_before_the_agent_check(installed):
    install_fast_check(installed, "exit 1")

    assert commit(installed).returncode != 0

    assert ran(installed) == ["fast"]


# A formatter that squeezes the runs of spaces in the staged `a` and stages the result.
SQUEEZE = (
    "if git show :a | grep -q '  '; then git show :a | tr -s ' ' > a; git add a; fi"
)


def install_formatter(repo):
    """Make the repo's pre-commit squeeze the spaces in `a` and stage the result, as a
    formatter that restages does."""
    install_fast_check(repo, SQUEEZE)


def test_the_agent_check_sees_what_the_formatter_staged(installed):
    install_formatter(installed)
    hook(
        installed,
        "agent-pre-commit",
        "if git show :a | grep -q '  '; then seen=unformatted; else seen=formatted; fi\n"
        'echo "$seen" >> "$(git rev-parse --git-dir)/ran"',
    )
    edit(installed, "a  b\n")

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "formatted"]


def test_a_formatted_commit_is_the_next_ones_check_base(installed):
    install_formatter(installed)
    noting_bases(installed)
    edit(installed, "a  b\n")
    assert commit(installed).returncode == 0
    formatted = head(installed)
    edit(installed, "c  d\n")

    run = commit(installed)

    assert run.returncode == 0, run.stderr
    assert bases(installed) == ["none", formatted]


def test_a_check_that_changes_whats_staged_refuses_the_commit(installed):
    """It didn't see what would be committed: what it staged itself, or an agent's git
    add while it ran."""
    hook(
        installed,
        "agent-pre-commit",
        f'echo "${{CHECK_BASE:-none}}" >> "$(git rev-parse --git-dir)/bases"\n{SQUEEZE}',
    )
    edit(installed, "a  b\n")

    refused = commit(installed)

    assert refused.returncode != 0
    assert refused.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")
    assert git(installed, "rev-parse", "HEAD", check=False).returncode != 0
    # The next commit has nothing left to change, so the check passes.
    retried = commit(installed)
    assert retried.returncode == 0, retried.stderr
    passed = head(installed)
    # A person commits the tree the refused check started on, which never passed.
    edit(installed, "a  b\n")
    assert commit(installed, agent=False).returncode == 0
    edit(installed, "c\n")
    assert commit(installed).returncode == 0
    assert bases(installed) == ["none", "none", passed]


def test_a_quick_check_that_changes_whats_staged_refuses_the_fix_commit(installed):
    assert commit(installed).returncode == 0
    quick_check(installed, SQUEEZE)
    edit(installed, "a  b\n")

    run = commit(installed, "--fixup=HEAD")

    assert run.returncode != 0
    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")


def test_a_check_that_dirties_the_work_tree_is_not_remembered(installed):
    hook(
        installed,
        "agent-pre-commit",
        'echo agent >> "$(git rev-parse --git-dir)/ran"\necho scribbled > a',
    )
    assert commit(installed).returncode == 0

    assert commit(installed, "--amend").returncode == 0

    assert ran(installed) == ["fast", "agent", "fast", "agent"]


def test_a_failing_agent_check_blocks_the_commit_and_shows_why(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")
    run = commit(installed)
    assert run.returncode != 0 and "a is bad" in run.stderr
    assert git(installed, "rev-parse", "HEAD", check=False).returncode != 0


def test_commit_all_checks_what_it_commits(installed):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("bad\n")
    run = commit(installed, "-a")
    assert run.returncode != 0 and "a is bad" in run.stderr


def test_a_human_commit_skips_the_agent_check(installed):
    assert commit(installed, agent=False).returncode == 0
    assert ran(installed) == ["fast"]


def test_a_nounset_pre_commit_still_works_for_humans(installed):
    pre = installed / ".githooks" / "pre-commit"
    pre.write_text(pre.read_text().replace("#!/bin/sh", "#!/bin/sh -u", 1))
    assert commit(installed, agent=False).returncode == 0


def test_an_unchanged_tree_skips_the_agent_check_it_already_passed(installed):
    assert commit(installed).returncode == 0
    reworded = commit(installed, "--amend")
    assert reworded.returncode == 0
    assert ran(installed) == ["fast", "agent", "fast"]
    assert "already passed" in reworded.stderr


def test_a_teammates_agent_without_the_gate_runs_the_check_every_time(installed):
    """The hook is shared; agent-commit-gate is one person's tool."""
    assert commit(installed, gate_on_path=False).returncode == 0
    assert commit(installed, "--amend", gate_on_path=False).returncode == 0
    assert ran(installed) == ["fast", "agent", "fast", "agent"]


def test_a_changed_tree_runs_the_agent_check_again(installed):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("better\n")
    assert commit(installed, "--amend", "-a").returncode == 0
    assert ran(installed) == ["fast", "agent", "fast", "agent"]


def test_a_failed_agent_check_runs_again_on_the_same_tree(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")
    assert commit(installed).returncode != 0
    assert commit(installed).returncode != 0
    assert ran(installed) == ["fast", "agent", "fast", "agent"]


def test_a_pass_with_unstaged_changes_is_not_remembered(installed):
    """Some agent checks read the working tree, so their pass says nothing of the index."""
    assert commit(installed).returncode == 0
    (installed / "a").write_text("staged\n")
    git(installed, "add", "a")
    (installed / "a").write_text("unstaged\n")
    assert commit(installed).returncode == 0
    assert commit(installed, "--amend").returncode == 0
    assert ran(installed) == ["fast", "agent", "fast", "agent", "fast", "agent"]


def test_a_changed_agent_check_runs_again_on_the_same_tree(installed):
    assert commit(installed).returncode == 0
    check = installed / ".githooks" / "agent-pre-commit"
    check.write_text(check.read_text() + "\n# stricter now\n")
    assert commit(installed, "--amend").returncode == 0
    assert ran(installed) == ["fast", "agent", "fast", "agent"]


def test_an_agent_check_that_runs_pre_commit_does_not_loop(repo):
    hook(
        repo,
        "agent-pre-commit",
        'echo agent >> "$(git rev-parse --git-dir)/ran"\n"$(dirname "$0")/pre-commit"',
    )
    hook(repo, "pre-commit", 'echo fast >> "$(git rev-parse --git-dir)/ran"')
    install(repo)
    assert commit(repo, "--allow-empty").returncode == 0
    assert ran(repo) == ["fast", "agent", "fast"]


def test_a_passing_agent_check_prints_one_line(installed):
    hook(installed, "agent-pre-commit", "echo lots of noisy output\necho .......")

    run = commit(installed)

    assert run.returncode == 0
    assert "noisy" not in run.stderr + run.stdout
    assert "agent-pre-commit: passed" in run.stderr


def test_a_failing_agent_check_ends_with_commit_refused(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")

    run = commit(installed)

    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")
    assert run.stderr.index("a is bad") < run.stderr.index("COMMIT REFUSED")


@pytest.mark.parametrize("flag", ["--fixup=HEAD", "--squash=HEAD"])
def test_a_small_fix_commit_without_a_quick_check_skips_the_agent_check(
    installed, flag
):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")

    run = commit(installed, flag)

    assert run.returncode == 0, run.stderr
    assert (
        "skipped for a small fix commit" in run.stderr
        and "wt-ship runs it on each commit it lands" in run.stderr
    )
    assert ran(installed) == ["fast", "agent", "fast"]


def quick_check(repo, body='echo quick >> "$(git rev-parse --git-dir)/ran"'):
    return hook(repo, "agent-quick-check", body)


def test_a_small_fix_commit_runs_the_quick_check_not_the_agent_check(installed):
    quick_check(installed)
    assert commit(installed).returncode == 0
    (installed / "a").write_text("still good\n")
    git(installed, "add", "a")

    run = commit(installed, "--fixup=HEAD")

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "agent", "fast", "quick"]
    assert "agent-quick-check: passed" in run.stderr


def test_a_failing_quick_check_refuses_the_fix_commit(installed):
    quick_check(installed, "echo 'quick check: lint'; exit 1")
    assert commit(installed).returncode == 0
    (installed / "a").write_text("still good\n")
    git(installed, "add", "a")

    run = commit(installed, "--fixup=HEAD")

    assert run.returncode != 0
    assert "quick check: lint" in run.stderr
    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")


def lines(count, word="line"):
    return "".join(f"{word} {n}\n" for n in range(count))


def checks_for_fix(installed, prepare, change, *flags):
    """`prepare` sets files up and an ordinary commit records them; then `change`
    alters them and a fix commit records that. Returns the checks the fix commit ran."""
    quick_check(installed)
    prepare(installed)
    git(installed, "add", "-A")
    assert commit(installed).returncode == 0
    before = len(ran(installed))
    change(installed)
    git(installed, "add", "-A")
    commit(installed, "--fixup=HEAD", *flags)
    return ran(installed)[before:]


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def retarget(link, target):
    link.unlink()
    link.symlink_to(target)


QUICK = ["fast", "quick"]
FULL = ["fast", "agent"]


@pytest.mark.parametrize(
    "prepare, change, expected",
    [
        # 10 lines replaced: 10 added, 10 removed.
        (
            lambda repo: write(repo / "a", lines(10)),
            lambda repo: write(repo / "a", lines(10, "new")),
            QUICK,
        ),
        (
            lambda repo: write(repo / "a", lines(10)),
            lambda repo: write(repo / "a", lines(11, "new")),
            FULL,
        ),
        (
            lambda repo: write(repo / "tests" / "x.py", "x\n"),
            lambda repo: write(repo / "tests" / "x.py", "y\n"),
            FULL,
        ),
        (
            lambda repo: write(repo / "test_a.py", "x\n"),
            lambda repo: write(repo / "test_a.py", "y\n"),
            FULL,
        ),
        (
            lambda repo: write(repo / "tests" / "t\u00e9st.py", "x\n"),
            lambda repo: write(repo / "tests" / "t\u00e9st.py", "y\n"),
            FULL,
        ),
        (
            lambda repo: (repo / "b.bin").write_bytes(b"\0\1\2"),
            lambda repo: (repo / "b.bin").write_bytes(b"\0\1\3"),
            FULL,
        ),
        (
            lambda repo: write(repo / "c.txt", "c\n"),
            lambda repo: (repo / "c.txt").unlink(),
            FULL,
        ),
        (
            lambda repo: write(repo / "c.txt", lines(5)),
            lambda repo: (repo / "c.txt").rename(repo / "d.txt"),
            FULL,
        ),
        (lambda repo: None, lambda repo: write(repo / "new.txt", "n\n"), FULL),
        (
            lambda repo: write(repo / "run.sh", "echo\n"),
            lambda repo: (repo / "run.sh").chmod(0o755),
            FULL,
        ),
        (
            lambda repo: (repo / "link").symlink_to("a"),
            lambda repo: retarget(repo / "link", "b"),
            FULL,
        ),
    ],
    ids=[
        "20 lines",
        "21 lines",
        "a file under tests",
        "a file named test_",
        "a test file whose path git quotes",
        "a binary file",
        "a deleted file",
        "a renamed file",
        "a new file",
        "a mode change",
        "a symlink retargeted",
    ],
)
def test_only_a_small_fix_commit_runs_the_quick_check(
    installed, prepare, change, expected
):
    assert checks_for_fix(installed, prepare, change) == expected


def test_an_empty_fix_commit_is_small(installed):
    def nothing(_):
        pass

    assert checks_for_fix(installed, nothing, nothing, "--allow-empty") == QUICK


def staged_for_a_fix(repo):
    """After one passing commit, stage a change the agent check refuses, so only a
    commit that skips that check passes. Returns an agent's environment, with this
    checkout's gate on PATH."""
    assert commit(repo).returncode == 0
    (repo / "a").write_text("bad\n")
    git(repo, "add", "a")
    env = without_repo_vars(os.environ)
    env["PATH"] = os.pathsep.join([str(SCRIPT.parent), env["PATH"]])
    env["AI_AGENT"] = "test-agent"
    return env


def test_a_fixup_subject_given_with_m_skips_the_agent_check(installed):
    env = staged_for_a_fix(installed)

    run = git(installed, "commit", "-q", "-m", "fixup! x", env=env, check=False)

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "agent", "fast"]


def test_a_fixup_subject_in_a_message_file_given_from_a_subfolder_skips_the_agent_check(
    installed,
):
    """git reads a relative -F path from where it started, which hooks get as
    GIT_PREFIX, while the hook itself runs at the top level."""
    env = staged_for_a_fix(installed)
    (installed / "sub").mkdir()
    (installed / "sub" / "msg.txt").write_text("fixup! x\n\nWhy.\n")
    (installed / "msg.txt").write_text("x\n")  # what a path from the top would find

    run = git(installed / "sub", "commit", "-q", "-F", "msg.txt", env=env, check=False)

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "agent", "fast"]


def run_hook_in(monkeypatch, capsys, command, cwd):
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = gate.main([])
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "cd scratch && git commit -n -m x",
        "git -C scratch commit -n -m x",
    ],
)
def test_no_verify_passes_in_a_repo_without_an_agent_check(
    monkeypatch, capsys, tmp_path, command
):
    scratch = tmp_path / "scratch"
    git(tmp_path, "init", "-q", str(scratch))
    cwd = scratch if command.startswith("git commit") else tmp_path

    code, err = run_hook_in(monkeypatch, capsys, command, cwd)

    assert code == 0, err


def test_no_verify_is_still_refused_where_there_is_an_agent_check(
    monkeypatch, capsys, installed
):
    code, err = run_hook_in(monkeypatch, capsys, "git commit -n -m x", installed)

    assert code == 2 and "--no-verify" in err
    assert "amend" in err  # a message-only amend already skips the check


@pytest.mark.parametrize(
    "args",
    [
        ["-am", "fixup! x"],
        ["--message=fixup! x"],
        ["-m", "x", "--fixup=HEAD"],
        ["-a", "-m", "squash! x"],
    ],
)
def test_fix_commit_forms(args):
    assert gate.is_fix_commit(args)


@pytest.mark.parametrize(
    "args", [["-m", "x", "-m", "fixup! later paragraph"], ["-am", "x"], []]
)
def test_ordinary_commits_are_not_fix_commits(args):
    assert not gate.is_fix_commit(args)


@pytest.mark.parametrize(
    "form",
    [["-F", "{}"], ["-F{}"], ["-qF", "{}"], ["--file", "{}"], ["--file={}"]],
)
def test_a_fixup_subject_in_a_message_file_makes_a_fix_commit(tmp_path, form):
    path = tmp_path / "msg.txt"
    path.write_text("\nfixup! x\n\nWhy.\n")  # git's cleanup drops the blank line

    assert gate.is_fix_commit([word.format(path) for word in form])


def test_a_message_file_with_fixup_only_in_a_later_paragraph_is_not_a_fix(tmp_path):
    path = tmp_path / "msg.txt"
    path.write_text("x\n\nfixup! later paragraph\n")

    assert not gate.is_fix_commit(["-F", str(path)])


def test_a_message_file_the_gate_cant_read_is_not_a_fix(tmp_path):
    assert not gate.is_fix_commit(["-F", str(tmp_path / "missing.txt")])


def test_a_message_in_a_pipe_is_left_for_git(tmp_path):
    """`-F <(printf ...)` or a named pipe: the gate must neither read it, which would
    leave git no message, nor wait on it."""
    pipe = tmp_path / "msg"
    os.mkfifo(pipe)
    held = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)  # a writer, so no open blocks
    found = []
    try:
        os.write(held, b"fixup! x\n")
        call = threading.Thread(
            target=lambda: found.append(gate.is_fix_commit(["-F", str(pipe)])),
            daemon=True,
        )
        call.start()
        call.join(5)

        assert not call.is_alive(), "the gate waited on the pipe"
        assert found == [False]
        assert os.read(held, 100) == b"fixup! x\n"
    finally:
        os.close(held)  # the last writer: a read still waiting gets its end


def test_a_message_on_stdin_is_not_a_fix(tmp_path, monkeypatch):
    """git reads stdin only after the hook has run; `-` is never a file's name here."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-").write_text("fixup! x\n")

    assert not gate.is_fix_commit(["-F", "-"])


def person_commit(repo, subject, files):
    """Write `files` ({name: text}) and commit all that's changed as a person would,
    with no agent check or warning; returns the commit's ID."""
    for name, text in files.items():
        write(repo / name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", subject)
    return head(repo)


def agent_git(repo, *args, stdin=None, agent=True, env=None):
    """`git ARGS` in `repo` as an agent runs it, with this checkout's gate on PATH, an
    editor that keeps the message git proposes, and `env` added; the run, after
    checking it passed."""
    env = without_repo_vars(os.environ) | {"GIT_EDITOR": "true"} | (env or {})
    env["PATH"] = os.pathsep.join(
        [str(SCRIPT.parent), path_without(SCRIPT.name, env["PATH"])]
    )
    if agent:
        env["AI_AGENT"] = "test-agent"
    run = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return run


def warnings(run):
    return [
        line
        for line in run.stderr.splitlines()
        if line.startswith("agent-commit-gate:")
    ]


def stray(path, target, kind="fixup!"):
    return (
        f"agent-commit-gate: this {kind} changes {path}, which its target"
        f' "{target}" didn\'t touch'
    )


def split_off(repo, *paths):
    """The advice for `paths`, each named from the top of the work tree (`:/`)."""
    return (
        "agent-commit-gate: to split off what belongs to another commit, while HEAD is"
        f" {head(repo)[:12]}: git reset -q HEAD^ --"
        f" {' '.join(f':/{path}' for path in paths)} && git commit -q --amend"
        " --no-edit, then commit it with that commit"
    )


# What the advice adds when more is staged than the commit took in.
UNSTAGE_FIRST = "; unstage the rest of what's staged first, or it goes in too"


def a_then_b(repo):
    """Commit a.txt as "Add a", then b.txt as "Add b"; returns Add a's ID."""
    a = person_commit(repo, "Add a", {"a.txt": "a\n"})
    person_commit(repo, "Add b", {"b.txt": "b\n"})
    return a


def test_a_fixup_that_sweeps_in_another_commits_file_names_only_that_file(installed):
    """The case that asked for the warning: `commit -a --fixup=A` swept in edits that
    were meant for B."""
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    assert warnings(run) == [stray("b.txt", "Add a"), split_off(installed, "b.txt")]


def test_a_fixup_of_its_targets_files_says_nothing(installed):
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")

    run = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    assert warnings(run) == []


@pytest.mark.parametrize(
    "form, kind",
    [
        (lambda a: ["-m", "fixup! Add a"], "fixup!"),
        (lambda a: ["-m", f"fixup! {a[:10]}"], "fixup!"),
        # A title that only starts subjects, as autosquash takes it: the oldest.
        (lambda a: ["-m", "fixup! Add"], "fixup!"),
        (lambda a: ["-m", "fixup!   squash! Add a"], "fixup!"),
        (lambda a: [f"--squash={a}"], "squash!"),
        (lambda a: [f"--fixup=amend:{a}"], "amend!"),
        (lambda a: ["-F", "-"], "fixup!"),
    ],
    ids=["subject", "ID", "prefix", "prefixes", "squash", "amend", "stdin"],
)
def test_each_way_to_name_the_target_finds_it(installed, form, kind):
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", *form(a), stdin="fixup! Add a\n")

    assert git(installed, "log", "-1", "--format=%s").stdout.startswith(kind)
    assert warnings(run) == [
        stray("b.txt", "Add a", kind),
        split_off(installed, "b.txt"),
    ]


def test_amending_a_fixup_compares_the_whole_commit(installed):
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    git(installed, "commit", "-q", "-a", f"--fixup={a}")
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", "--amend", "--no-edit")

    assert warnings(run) == [stray("b.txt", "Add a"), split_off(installed, "b.txt")]


def test_a_subject_that_is_the_title_wins_over_one_that_starts_with_it(installed):
    person_commit(installed, "Add a and c", {"c.txt": "c\n"})
    person_commit(installed, "Add a", {"a.txt": "a\n"})
    write(installed / "c.txt", "c fixed\n")
    write(installed / "a.txt", "a fixed\n")

    run = agent_git(installed, "commit", "-q", "-a", "-m", "fixup! Add a")

    assert warnings(run) == [stray("c.txt", "Add a"), split_off(installed, "c.txt")]


def test_of_two_commits_with_the_title_the_oldest_is_the_target(installed):
    person_commit(installed, "Add a", {"a1.txt": "a\n"})
    person_commit(installed, "Add a", {"a2.txt": "a\n"})
    write(installed / "a1.txt", "a fixed\n")
    write(installed / "a2.txt", "a fixed\n")

    run = agent_git(installed, "commit", "-q", "-a", "-m", "fixup! Add a")

    assert warnings(run) == [stray("a2.txt", "Add a"), split_off(installed, "a2.txt")]


def test_a_fixup_of_a_fixup_compares_with_the_first_ones_target(installed):
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    git(installed, "commit", "-q", "-a", f"--fixup={a}")
    write(installed / "a.txt", "a fixed again\n")
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", "--fixup=HEAD")

    assert warnings(run) == [stray("b.txt", "Add a"), split_off(installed, "b.txt")]


def test_a_file_an_earlier_fixup_added_counts_as_the_targets(installed):
    a = a_then_b(installed)
    write(installed / "a_test.txt", "test\n")
    git(installed, "add", "-A")
    first = agent_git(installed, "commit", "-q", f"--fixup={a}")
    write(installed / "a_test.txt", "test fixed\n")

    second = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    assert warnings(first)[:1] == [stray("a_test.txt", "Add a")]
    assert warnings(second) == []


def test_a_fixup_whose_target_isnt_found_says_nothing(installed):
    a_then_b(installed)
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", "-m", "fixup! Nothing like it")

    assert warnings(run) == []


def test_a_target_another_branch_holds_is_not_found(installed):
    """Autosquash replays only the commits a rebase onto that branch would: a commit
    the branch holds has landed, and no fold reaches it."""
    person_commit(installed, "Add a", {"a.txt": "a\n"})
    git(installed, "checkout", "-q", "--detach")
    person_commit(installed, "Add b", {"b.txt": "b\n"})
    write(installed / "a.txt", "a fixed\n")
    unlanded = agent_git(installed, "commit", "-q", "-a", "-m", "fixup! Add b")
    write(installed / "b.txt", "b for B\n")

    landed = agent_git(installed, "commit", "-q", "-a", "-m", "fixup! Add a")

    assert warnings(unlanded)[:1] == [stray("a.txt", "Add b")]
    assert warnings(landed) == []


def test_a_rebase_that_replays_a_fixup_says_nothing(installed):
    """git's rebase runs the post-commit hook for each commit it makes. In a detached
    worktree, as agents work, the commits before the rebased ones are unlanded too."""
    person_commit(installed, "Start", {"start.txt": "s\n"})
    git(installed, "checkout", "-q", "--detach")
    a = a_then_b(installed)
    write(installed / "b.txt", "b for B\n")
    made = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    replayed = agent_git(installed, "rebase", "-q", "--force-rebase", "HEAD~2")

    assert warnings(made)
    assert warnings(replayed) == []


def test_a_fixup_with_none_of_its_targets_files_says_to_undo_it(installed):
    a = a_then_b(installed)
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    undo = (
        "agent-commit-gate: if it fixes another commit, undo it while HEAD is"
        f" {head(installed)[:12]} (git reset -q --soft HEAD^) and commit it for that"
        " one"
    )
    assert warnings(run) == [stray("b.txt", "Add a"), undo]


def test_renaming_the_targets_file_says_nothing(installed):
    a = a_then_b(installed)
    git(installed, "mv", "a.txt", "a2.txt")

    run = agent_git(installed, "commit", "-q", f"--fixup={a}")

    assert warnings(run) == []


def test_a_stray_rename_names_both_paths(installed):
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    git(installed, "mv", "b.txt", "b2.txt")

    run = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")

    assert warnings(run) == [
        stray("b.txt -> b2.txt", "Add a"),
        split_off(installed, "b.txt", "b2.txt"),
    ]


def test_the_advice_splits_the_file_off(installed):
    """Run as printed, from a subfolder, for a path with a glob character, which git
    would otherwise read as a pattern that also matches b1.txt, a file of the
    target's."""
    a = person_commit(installed, "Add a", {"a.txt": "a\n", "b1.txt": "b\n"})
    person_commit(installed, "Add b", {"b[1].txt": "b\n"})
    write(installed / "a.txt", "a fixed\n")
    write(installed / "b1.txt", "b1 fixed\n")
    write(installed / "b[1].txt", "b for B\n")
    fix = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")
    advice = warnings(fix)
    assert len(advice) == 2, fix.stderr
    command = advice[-1].split(": ", 2)[2].split(", then ")[0]
    (installed / "sub").mkdir()

    subprocess.run(["sh", "-c", command], cwd=installed / "sub", check=True)

    files = git(installed, "show", "--format=", "--name-only", "HEAD").stdout
    assert files.split() == ["a.txt", "b1.txt"]
    assert git(installed, "status", "--short").stdout.splitlines() == [" M b[1].txt"]


def test_the_advice_says_to_unstage_what_else_is_staged(installed):
    """`git commit PATHS` leaves the rest of what's staged, which the advised amend, or
    the commit after an undo, would take in."""
    a = a_then_b(installed)
    write(installed / "a.txt", "a fixed\n")
    write(installed / "b.txt", "b for B\n")
    write(installed / "c.txt", "c\n")
    git(installed, "add", "c.txt")

    run = agent_git(installed, "commit", "-q", f"--fixup={a}", "--", "a.txt", "b.txt")

    assert warnings(run) == [
        stray("b.txt", "Add a"),
        split_off(installed, "b.txt") + UNSTAGE_FIRST,
    ]


def test_a_rebase_that_rewords_a_fixup_says_nothing(installed):
    """A rebase makes a reworded commit, as it does one after `rebase --continue`, with
    a `git commit` of its own."""
    person_commit(installed, "Start", {"start.txt": "s\n"})
    git(installed, "checkout", "-q", "--detach")
    a = a_then_b(installed)
    write(installed / "b.txt", "b for B\n")
    made = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")
    reword = {
        "GIT_SEQUENCE_EDITOR": "sed -i '2s/^pick/reword/'",  # the fixup's line
        "GIT_EDITOR": "sed -i '$a Why.'",
    }

    reworded = agent_git(
        installed, "rebase", "-q", "-i", "--no-autosquash", "HEAD~2", env=reword
    )

    assert git(installed, "log", "-1", "--format=%b").stdout.strip() == "Why."
    assert warnings(made)
    assert warnings(reworded) == []


def test_a_cherry_pick_of_a_fixup_says_nothing(installed):
    """A single cherry-pick, like a revert, runs post-commit with no folder of its own
    in the git dir."""
    person_commit(installed, "Start", {"start.txt": "s\n"})
    git(installed, "checkout", "-q", "--detach")
    a = a_then_b(installed)
    write(installed / "b.txt", "b for B\n")
    made = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}")
    fix = head(installed)
    git(installed, "reset", "-q", "--hard", "HEAD^")

    picked = agent_git(installed, "cherry-pick", fix)

    assert warnings(made)
    assert warnings(picked) == []


def test_a_persons_fixup_says_nothing(installed):
    a = a_then_b(installed)
    write(installed / "b.txt", "b for B\n")

    run = agent_git(installed, "commit", "-q", "-a", f"--fixup={a}", agent=False)

    assert warnings(run) == []


def test_a_cd_carries_across_segments(monkeypatch, capsys, tmp_path):
    scratch = tmp_path / "a" / "scratch"
    scratch.parent.mkdir()
    git(tmp_path, "init", "-q", str(scratch))

    code, err = run_hook_in(
        monkeypatch, capsys, "cd a; ls && cd scratch && git commit -n -m x", tmp_path
    )

    assert code == 0, err


def test_the_missing_agent_check_refusal_ends_with_commit_refused(tmp_path, capsys):
    code = gate.run_check(str(tmp_path / "nowhere"))

    assert code == 1
    assert (
        capsys.readouterr().err.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")
    )


def test_without_the_gate_a_refusal_still_ends_with_commit_refused(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")

    run = commit(installed, gate_on_path=False)

    assert run.returncode != 0
    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")


def head(repo, rev="HEAD"):
    return git(repo, "rev-parse", rev).stdout.strip()


def noting_bases(repo, name="agent-pre-commit"):
    """Make the repo's check `name` pass and note the CHECK_BASE it was given, `none`
    without one, in bases(repo)."""
    hook(repo, name, 'echo "${CHECK_BASE:-none}" >> "$(git rev-parse --git-dir)/bases"')


def bases(repo):
    path = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip(), "bases")
    return path.read_text().split() if path.exists() else []


def edit(repo, text):
    (repo / "a").write_text(text)
    git(repo, "add", "a")


def test_the_check_gets_the_commit_it_builds_on_as_check_base(installed):
    noting_bases(installed)
    assert commit(installed).returncode == 0
    first = head(installed)
    edit(installed, "better\n")

    run = commit(installed)

    assert run.returncode == 0, run.stderr
    assert bases(installed) == ["none", first]
    assert f"given CHECK_BASE {first[:12]}" in run.stderr


def test_check_base_is_the_nearest_commit_that_passed(installed):
    noting_bases(installed)
    assert commit(installed).returncode == 0
    passed = head(installed)
    edit(installed, "a person's\n")
    assert commit(installed, agent=False).returncode == 0  # runs no agent check
    edit(installed, "an agent's\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == ["none", passed]


def test_a_changed_check_gets_no_base_its_old_self_passed(installed):
    noting_bases(installed)
    assert commit(installed).returncode == 0
    check = installed / ".githooks" / "agent-pre-commit"
    check.write_text(check.read_text() + "# stricter now\n")
    edit(installed, "better\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == ["none", "none"]


def commits_without_checks(repo, count):
    """`count` commits on HEAD's branch, each a new `b`, made by fast-import, which
    runs no hook: none of them passed the check."""
    ref = git(repo, "symbolic-ref", "HEAD").stdout.strip()
    stream = []
    for n in range(count):
        stream += [f"commit {ref}", "committer t <t@t> 0 +0000", "data 0"]
        if n == 0:
            stream.append(f"from {head(repo)}")
        stream += ["M 100644 inline b", f"data {len(str(n))}", str(n)]
    subprocess.run(
        ["git", "-C", str(repo), "fast-import", "--quiet"],
        input="\n".join(stream) + "\n",
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    )
    git(repo, "reset", "-q", "--hard")


@pytest.mark.parametrize("between, found", [(99, True), (100, False)])
def test_the_search_for_a_base_looks_back_100_commits(installed, between, found):
    noting_bases(installed)
    assert commit(installed).returncode == 0
    passed = head(installed)
    commits_without_checks(installed, between)
    edit(installed, "better\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == ["none", passed if found else "none"]


def test_a_signed_commit_shown_with_its_signature_still_gives_a_base(installed):
    """log.showSignature adds lines on the signature to every git log's output."""
    key = installed / ".git" / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    for name, value in (
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", os.devnull),
        ("log.showSignature", "true"),
    ):
        git(installed, "config", name, value)
    noting_bases(installed)
    assert commit(installed, "-S").returncode == 0
    signed = head(installed)
    edit(installed, "better\n")

    run = commit(installed)

    assert run.returncode == 0, run.stderr
    assert bases(installed) == ["none", signed]


def test_an_inherited_check_base_does_not_reach_the_check(installed):
    noting_bases(installed)

    assert commit(installed, env={"CHECK_BASE": "deadbeef"}).returncode == 0

    assert bases(installed) == ["none"]


def test_a_small_fix_commit_gets_the_base_that_passed_the_full_check(installed):
    noting_bases(installed, "agent-quick-check")
    assert commit(installed).returncode == 0
    passed = head(installed)
    edit(installed, "still good\n")

    assert commit(installed, "--fixup=HEAD").returncode == 0

    assert bases(installed) == [passed]


def test_a_tree_that_passed_in_another_worktree_skips_the_check_there(installed):
    git(installed, "add", ".githooks")
    assert commit(installed).returncode == 0
    other = installed.parent / f"{installed.name}-wt"
    git(installed, "worktree", "add", "-q", "--detach", str(other))

    run = commit(other, "--allow-empty")

    assert run.returncode == 0, run.stderr
    assert "already passed" in run.stderr
    other_git_dir = git(other, "rev-parse", "--absolute-git-dir").stdout.strip()
    assert Path(other_git_dir, "ran").read_text().split() == ["fast"]


def test_any_recorded_tree_of_heads_skips_the_check_not_only_the_last(installed):
    assert commit(installed).returncode == 0
    edit(installed, "better\n")
    assert commit(installed).returncode == 0
    git(installed, "reset", "-q", "--hard", "HEAD~1")

    run = commit(installed, "--amend")

    assert run.returncode == 0, run.stderr
    assert "already passed" in run.stderr


def test_a_tree_that_passed_but_is_not_heads_runs_the_check(installed):
    """A revert brings back a tree that passed, but a check may read more than the
    tree, so only HEAD's own tree skips it; the revert runs it against HEAD."""
    noting_bases(installed)
    assert commit(installed).returncode == 0
    edit(installed, "better\n")
    assert commit(installed).returncode == 0
    edit(installed, "good\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == [
        "none",
        head(installed, "HEAD~2"),
        head(installed, "HEAD~1"),
    ]


def test_the_record_keeps_the_last_1000_passes(installed):
    record = installed / ".git" / passrecord.PASS_RECORD
    record.write_text("".join(f"{n:040x} old\n" for n in range(1500)))

    assert commit(installed).returncode == 0

    kept = record.read_text().splitlines()
    assert len(kept) == 1000
    assert kept[0] == f"{501:040x} old"
    assert kept[-1].split()[0] == head(installed, "HEAD^{tree}")


@pytest.fixture
def held_lane():
    """heavy's normal lane, held by another job: a live process named in its slot.
    Killing the process (`held_lane.kill()`) frees it."""
    holder = subprocess.Popen(["sleep", "60"])
    start = fifo.started(holder.pid)
    lane = Path(os.environ["HEAVY_DIR"]) / "normal"
    lane.mkdir(parents=True)
    (lane / "slot").write_text(
        f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()}"
        f" pid={holder.pid} start={start}\ncwd=/elsewhere\ncmd=cargo test\n"
    )
    holder.lane = lane
    yield holder
    holder.kill()
    holder.wait()


@pytest.fixture
def committing(tmp_path_factory):
    """Start an agent's `git commit` in a repo in the background, in a process group of
    its own, its stderr going to a file that `proc.err()` reads; every one is killed
    and reaped at the end."""
    procs = []

    def start(repo, *args):
        env = without_repo_vars(os.environ)
        env["PATH"] = os.pathsep.join([str(SCRIPT.parent), env["PATH"]])
        env["AI_AGENT"] = "test-agent"
        err = tmp_path_factory.mktemp("commit") / "stderr"
        with err.open("w") as sink:
            proc = subprocess.Popen(
                ["git", "commit", "-q", "-m", "x", *args],
                cwd=repo,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=sink,
                start_new_session=True,
            )
        proc.err = err.read_text
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def in_queue(lane):
    return any(p.name[:1].isdigit() for p in lane.iterdir())


def queues_or_warns(commit_run, lane):
    """Wait, as wait_or_warn does, for the commit's check to join the queue and for
    heavy to say it waits for the lane. Only then is heavy inside its wait loop: it
    writes its entry before it first looks at the lane, and a lane freed in that gap
    is never waited for."""
    return wait_or_warn(
        lambda: in_queue(lane), "the check never joined the queue"
    ) and wait_or_warn(
        lambda: "waiting for the normal lane" in commit_run.err(),
        "heavy never said the check waits for the lane",
    )


def group_ended(pgid):
    """Whether every process in group `pgid` has exited, a zombie counting as exited."""
    for name in os.listdir("/proc"):
        # State, parent, group; None or short when it exited while being read.
        fields = fifo.stat_fields(name) if name.isdigit() else None
        if fields and len(fields) > 2 and fields[0] != "Z" and int(fields[2]) == pgid:
            return False
    return True


def finishes(commit_run):
    """Wait, as finishes_or_warns does, for a commit whose check runs outside heavy's
    queue and its hook to end, failing past the deadline: nothing it does waits on the
    machine."""
    wait_until(
        lambda: commit_run.poll() is not None and group_ended(commit_run.pid),
        "the commit never finished",
    )


def finishes_or_warns(commit_run):
    """Wait for the commit and its hook, which share its process group. A Ctrl-C ends
    git at once, while the hook may still be printing its refusal to the shared stderr
    file, so git's exit alone would let a test read it too soon."""
    return wait_or_warn(
        lambda: commit_run.poll() is not None and group_ended(commit_run.pid),
        "the commit never finished",
        30,
    )


def test_an_agent_check_waits_its_turn_in_heavys_queue(
    installed, held_lane, committing
):
    commit_run = committing(installed)
    waiting = queues_or_warns(commit_run, held_lane.lane)
    # Whether heavy said so or the wait ran out, the agent check didn't run while the
    # lane was held; the fast one ran before it.
    assert ran(installed) == ["fast"], commit_run.err()
    if not waiting:
        return
    held_lane.kill()  # not reaped: a zombie, which has ended all the same
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode == 0, err
    assert ran(installed) == ["fast", "agent"]
    assert "waiting for the normal lane" in err and "cargo test" in err


def test_a_git_add_while_the_check_waits_its_turn_refuses_the_commit(
    installed, held_lane, committing
):
    """git reads the index again after the hook, so the add would go in unchecked."""
    commit_run = committing(installed)
    if not queues_or_warns(commit_run, held_lane.lane):
        return
    edit(installed, "added while it waited\n")
    held_lane.kill()
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode != 0, err
    assert err.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")
    assert git(installed, "rev-parse", "HEAD", check=False).returncode != 0


def test_stopping_a_check_that_waits_leaves_the_queue_in_one_line(
    installed, held_lane, committing
):
    commit_run = committing(installed)
    if not queues_or_warns(commit_run, held_lane.lane):
        return
    os.killpg(commit_run.pid, signal.SIGINT)  # Ctrl-C reaches the whole group
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode != 0
    assert "Traceback" not in err, err
    assert "COMMIT REFUSED" in err
    assert not in_queue(held_lane.lane)


def test_a_heavy_inside_an_agent_check_runs_at_once(installed, committing):
    # The check holds the lane's slot; a heavy it runs must not wait on it.
    heavy = SCRIPT.parent / "heavy"
    hook(
        installed,
        "agent-pre-commit",
        f'"{heavy}" -- true && echo agent >> "$(git rev-parse --git-dir)/ran"',
    )
    git(installed, "add", ".githooks/agent-pre-commit")
    commit_run = committing(installed)
    commit_run.wait(timeout=30)

    assert commit_run.returncode == 0, commit_run.err()
    assert ran(installed) == ["fast", "agent"]


def test_only_a_check_that_passes_goes_into_heavys_history(installed):
    history = Path(os.environ["HEAVY_DIR"]) / "history.json"
    passed = commit(installed)
    assert passed.returncode == 0, passed.stderr
    assert history.exists()
    after_pass = history.read_text()

    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")
    refused = commit(installed)

    assert refused.returncode != 0
    assert history.read_text() == after_pass


def asking_check(repo, name="agent-pre-commit", word="agent"):
    """Make the repo's check `name` one that needs heavy's queue: run with
    CHECK_QUEUE_EXIT set, it notes `ask`, says why, and exits with it; run without, it
    notes `word` and passes."""
    hook(
        repo,
        name,
        'if [ -n "${CHECK_QUEUE_EXIT:-}" ]; then\n'
        '  echo ask >> "$(git rev-parse --git-dir)/ran"\n'
        "  echo 'needs a build: src/main.rs'\n"
        '  exit "$CHECK_QUEUE_EXIT"\n'
        "fi\n"
        f'echo {word} >> "$(git rev-parse --git-dir)/ran"',
    )


def light_check(repo, body=""):
    """Make the repo's agent check one that needs no queue: it names CHECK_QUEUE_EXIT,
    notes `light-` and the value it was given, then runs `body`."""
    hook(
        repo,
        "agent-pre-commit",
        'echo "light-${CHECK_QUEUE_EXIT:-unset}" >> "$(git rev-parse --git-dir)/ran"\n'
        + body,
    )


def test_a_check_that_needs_no_queue_passes_while_the_lane_is_held(
    installed, held_lane, committing
):
    light_check(installed)
    commit_run = committing(installed)
    finishes(commit_run)
    err = commit_run.err()

    assert commit_run.returncode == 0, err
    assert ran(installed) == ["fast", "light-75"]
    assert "agent-pre-commit: passed" in err and "outside heavy's queue" in err
    assert "waiting" not in err


def test_a_check_that_asks_for_the_queue_waits_its_turn_then_runs_in_it(
    installed, held_lane, committing
):
    asking_check(installed)
    commit_run = committing(installed)
    waiting = queues_or_warns(commit_run, held_lane.lane)
    # Whether heavy said so or the wait ran out, it didn't run again while it was held.
    assert "agent" not in ran(installed), commit_run.err()
    if not waiting:
        return
    assert ran(installed) == ["fast", "ask"]
    held_lane.kill()
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode == 0, err
    assert ran(installed) == ["fast", "ask", "agent"]
    # Its reason shows before heavy's waiting lines.
    assert err.index("needs a build: src/main.rs") < err.index("waiting for the")
    assert "outside heavy's queue" not in err


def test_the_run_outside_the_queue_gets_one_core_and_one_cargo_job(installed):
    light_check(
        installed,
        'sed -n "s/^Cpus_allowed_list:[[:space:]]*/cores-/p" /proc/$$/status'
        ' >> "$(git rev-parse --git-dir)/ran"\n'
        'echo "jobs-${CARGO_BUILD_JOBS:-unset}" >> "$(git rev-parse --git-dir)/ran"',
    )

    run = commit(installed)

    assert run.returncode == 0, run.stderr
    _, _, cores, jobs = ran(installed)
    assert cores == f"cores-{max(os.sched_getaffinity(0))}"  # the last it may use
    assert jobs == "jobs-1"


def test_a_pass_outside_the_queue_is_recorded(installed):
    light_check(installed)
    assert commit(installed).returncode == 0

    run = commit(installed, "--amend")

    assert run.returncode == 0, run.stderr
    assert "already passed" in run.stderr
    assert ran(installed) == ["fast", "light-75", "fast"]


def test_stopping_a_check_outside_the_queue_refuses_in_one_line(
    installed, held_lane, committing
):
    # With the lane held, a check wrongly queued never starts.
    light_check(installed, 'touch "$(git rev-parse --git-dir)/started"\nsleep 30')
    commit_run = committing(installed)
    started = installed / ".git" / "started"
    wait_until(started.exists, "the check never started")
    os.killpg(commit_run.pid, signal.SIGINT)  # Ctrl-C reaches the whole group
    finishes(commit_run)
    err = commit_run.err()

    assert commit_run.returncode != 0
    assert "Traceback" not in err, err
    assert "COMMIT REFUSED" in err


def test_a_check_that_fails_outside_the_queue_is_refused_without_queueing(installed):
    light_check(installed, "echo 'lint: x is unused'\nexit 1")

    run = commit(installed)

    assert run.returncode != 0
    assert ran(installed) == ["fast", "light-75"]
    assert run.stderr.index("lint: x is unused") < run.stderr.index("COMMIT REFUSED")


def test_a_check_that_asks_again_in_the_queue_is_refused(installed):
    hook(
        installed,
        "agent-pre-commit",
        "# exits 75 whether CHECK_QUEUE_EXIT is set or not\n"
        'echo again >> "$(git rev-parse --git-dir)/ran"\n'
        "exit 75",
    )

    run = commit(installed)

    assert run.returncode != 0
    assert ran(installed) == ["fast", "again", "again"]
    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")


def test_an_inherited_check_queue_exit_reaches_no_check_that_does_not_name_it(
    installed,
):
    # [X] keeps the check from naming the variable, which would opt it in.
    hook(
        installed,
        "agent-pre-commit",
        "env | sed -n 's/^CHECK_QUEUE_E[X]IT=/inherited-/p'"
        ' >> "$(git rev-parse --git-dir)/ran"\n'
        'echo agent >> "$(git rev-parse --git-dir)/ran"',
    )

    run = commit(installed, env={"CHECK_QUEUE_EXIT": "75"})

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "agent"]


def test_a_quick_check_that_asks_for_the_queue_waits_its_turn_too(
    installed, held_lane, committing
):
    light_check(installed)
    assert commit(installed).returncode == 0  # outside the queue, so past the lane
    asking_check(installed, "agent-quick-check", word="quick")
    edit(installed, "still good\n")
    commit_run = committing(installed, "--fixup=HEAD")
    waiting = queues_or_warns(commit_run, held_lane.lane)
    # Whether heavy said so or the wait ran out, it didn't run again while it was held.
    assert "quick" not in ran(installed), commit_run.err()
    if not waiting:
        return
    assert ran(installed) == ["fast", "light-75", "fast", "ask"]
    held_lane.kill()
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode == 0, err
    assert ran(installed) == ["fast", "light-75", "fast", "ask", "quick"]
    assert "needs a build: src/main.rs" in err


def test_a_check_that_fails_as_the_oom_killer_acts_says_to_run_it_again(
    installed, monkeypatch
):
    vmstat = installed / ".git" / "vmstat"  # outside the work tree
    vmstat.write_text("oom_kill 0\n")
    monkeypatch.setenv("HEAVY_VMSTAT", str(vmstat))
    # As a test runner exits when the OOM killer takes one of its workers.
    hook(installed, "agent-pre-commit", f'echo "oom_kill 1" > "{vmstat}"; exit 1')
    git(installed, "add", ".githooks/agent-pre-commit")

    run = commit(installed)

    assert run.returncode != 0
    assert "out-of-memory killer" in run.stderr, run.stderr


def test_a_check_never_shares_a_lane(installed, held_lane, committing, monkeypatch):
    # Room to share, and a holder that idles: a job that asked to would start.
    meminfo = Path(os.environ["HEAVY_DIR"]).parent / "roomy"
    meminfo.write_text("MemAvailable: 16777216 kB\n")
    monkeypatch.setenv("HEAVY_MEMINFO", str(meminfo))
    monkeypatch.setenv("HEAVY_SHARE_SECONDS", "0.05")
    commit_run = committing(installed)
    waiting = queues_or_warns(commit_run, held_lane.lane)
    # fixed sleep: many share windows, in which a check that shares would start
    time.sleep(1)

    assert ran(installed) == ["fast"], commit_run.err()  # it still waits its turn
    if not waiting:
        return
    held_lane.kill()
    if finishes_or_warns(commit_run):
        assert commit_run.returncode == 0, commit_run.err()


# A repo's agent-commit-check, the commit check, runs at each commit in place of
# agent-pre-commit, on a record of its own, so wt-ship runs the full check when it lands.


def install_commit_check(repo, body=""):
    """Give the repo an agent-commit-check that notes `commit` in the run log, then
    runs `body`."""
    hook(
        repo,
        "agent-commit-check",
        f'echo commit >> "$(git rev-parse --git-dir)/ran"\n{body}',
    )


def recheck(repo):
    """Run the agent check through the gate with no commit above it, as wt-ship does."""
    return subprocess.run(
        [str(SCRIPT), "--run", str(repo / ".githooks" / "agent-pre-commit")],
        cwd=repo,
        env=without_repo_vars(os.environ) | {"AGENT_PRE_COMMIT": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_commit_runs_the_commit_check_in_place_of_the_agent_check(installed):
    install_commit_check(installed)

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "commit"]


def test_a_failing_commit_check_refuses_the_commit(installed):
    install_commit_check(installed, "exit 4")

    run = commit(installed)

    assert run.returncode != 0
    assert "COMMIT REFUSED: agent-commit-check failed" in run.stderr, run.stderr


def test_a_recheck_runs_the_full_check_on_a_tree_only_the_commit_check_passed(
    installed,
):
    install_commit_check(installed)
    assert commit(installed).returncode == 0

    done = recheck(installed)

    assert done.returncode == 0, done.stderr
    assert ran(installed) == ["fast", "commit", "agent"]


def test_the_commit_checks_base_is_the_last_commit_it_passed(installed):
    noting_bases(installed, "agent-commit-check")
    assert commit(installed).returncode == 0
    first = head(installed)
    edit(installed, "more\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == ["none", first]


def test_no_check_inherits_the_variable_that_marks_a_commit(installed):
    install_commit_check(
        installed,
        'echo "mark-${AGENT_CHECK_AT_COMMIT:-unset}" >> "$(git rev-parse --git-dir)/ran"',
    )

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "commit", "mark-unset"]


def test_an_amend_of_only_the_message_skips_the_commit_check_it_passed(installed):
    install_commit_check(installed)
    assert commit(installed).returncode == 0

    run = commit(installed, "--amend")

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "commit", "fast"]


def test_the_commit_check_waits_its_turn_in_heavys_queue(installed):
    install_commit_check(
        installed, 'echo "slot-${HEAVY_SLOT:+set}" >> "$(git rev-parse --git-dir)/ran"'
    )

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "commit", "slot-set"]


def test_a_commit_check_naming_the_queue_exit_runs_outside_the_queue_first(installed):
    install_commit_check(
        installed,
        'echo "exit-${CHECK_QUEUE_EXIT:-unset}" >> "$(git rev-parse --git-dir)/ran"',
    )

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "commit", "exit-75"]


def test_a_repo_still_on_the_old_line_runs_the_full_check_at_each_commit(installed):
    old = OLD_LINES["before the commit mark"]
    hook(
        installed, "pre-commit", f'{old}\necho fast >> "$(git rev-parse --git-dir)/ran"'
    )
    install_commit_check(installed)

    assert commit(installed).returncode == 0

    assert ran(installed) == ["fast", "agent"]


def test_the_commit_checks_base_may_be_a_commit_the_full_check_passed(installed):
    assert commit(installed).returncode == 0  # the full check, as on a landed tip
    first = head(installed)
    noting_bases(installed, "agent-commit-check")
    edit(installed, "more\n")

    assert commit(installed).returncode == 0

    assert bases(installed) == [first]


def test_an_amend_skips_the_commit_check_on_a_tree_the_full_check_passed(installed):
    assert commit(installed).returncode == 0
    install_commit_check(installed)

    run = commit(installed, "--amend")

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "agent", "fast"]


def test_a_small_fix_commit_runs_the_quick_check_though_a_commit_check_exists(
    installed,
):
    install_commit_check(installed)
    quick_check(installed)
    assert commit(installed).returncode == 0
    edit(installed, "better\n")

    run = commit(installed, "--fixup=HEAD")

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["fast", "commit", "fast", "quick"]


def test_the_quick_checks_base_counts_only_full_passes(installed):
    assert commit(installed).returncode == 0
    first = head(installed)
    install_commit_check(installed)
    edit(installed, "more\n")
    assert commit(installed).returncode == 0  # the commit check's pass
    noting_bases(installed, "agent-quick-check")
    edit(installed, "most\n")

    assert commit(installed, "--fixup=HEAD").returncode == 0

    assert bases(installed) == [first]


def test_a_rechecks_base_counts_only_full_passes(installed):
    install_commit_check(installed)
    assert commit(installed).returncode == 0
    noting_bases(installed)

    done = recheck(installed)

    assert done.returncode == 0, done.stderr
    # The commit check's pass of HEAD's tree is no base: the full check runs whole.
    assert bases(installed) == ["none"]


# --no-queue: wt-ship's first round of a landing's checks, outside heavy's queue.


def run_no_queue(repo, env=None):
    """Run the agent check through the gate with --no-queue, as wt-ship's first round
    does, with `env` added to the environment."""
    return subprocess.run(
        [
            str(SCRIPT),
            "--run",
            str(repo / ".githooks" / "agent-pre-commit"),
            "--no-queue",
        ],
        cwd=repo,
        env=without_repo_vars(os.environ) | {"AGENT_PRE_COMMIT": "1"} | (env or {}),
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_queue_exits_75_without_running_a_check_that_waits_in_the_queue(installed):
    done = run_no_queue(installed)  # its agent check doesn't name CHECK_QUEUE_EXIT

    assert done.returncode == 75, done.stderr
    assert ran(installed) == []


def test_no_queue_exits_75_when_the_check_asks_for_the_queue(installed):
    asking_check(installed)

    done = run_no_queue(installed)

    assert done.returncode == 75, done.stderr
    assert ran(installed) == ["ask"]


def test_no_queue_runs_a_check_that_needs_no_queue(installed):
    light_check(installed)

    done = run_no_queue(installed)

    assert done.returncode == 0, done.stderr
    assert ran(installed) == ["light-75"]


# Variables whose names mark a secret, and some that only look alike. Their values are
# fake, and the checks below write down names only: a test's failure prints names.
SECRET_NAMED = (
    *("FAKE_API_KEY", "GH_TOKEN", "SHOP_CLIENT_SECRET", "MAIL_PASSWORD"),
    *("SMTP_PASS", "github_token"),
)
LOOKALIKES = ("MONKEY", "SSH_AUTH_SOCK", "KEYRING_DIR", "BYPASS_CACHE", "TOKENS_SEEN")
PLANTED = dict.fromkeys(SECRET_NAMED + LOOKALIKES, "fake")
# The check writes down the names it received in `names`, and in `empty` the names of
# those whose value is empty.
NOTE_NAMES = (
    'awk -v dir="$(git rev-parse --git-dir)" \'BEGIN { for (name in ENVIRON) {'
    ' print name > (dir "/names");'
    ' if (ENVIRON[name] == "") print name > (dir "/empty") } }\''
)


def names_received(repo):
    return set((repo / ".git" / "names").read_text().split())


def empty_names_received(repo):
    path = repo / ".git" / "empty"
    return set(path.read_text().split()) if path.exists() else set()


def full_check(repo, env=PLANTED):
    hook(repo, "agent-pre-commit", NOTE_NAMES)
    return commit(repo, env=env)


def commit_check_at_a_commit(repo, env=PLANTED):
    install_commit_check(repo, NOTE_NAMES)
    return commit(repo, env=env)


def quick_check_of_a_small_fix(repo, env=PLANTED):
    quick_check(repo, NOTE_NAMES)
    assert commit(repo).returncode == 0
    edit(repo, "still good\n")
    return commit(repo, "--fixup=HEAD", env=env)


def check_outside_the_queue(repo, env=PLANTED):
    light_check(repo, NOTE_NAMES)
    return commit(repo, env=env)


def wt_ships_recheck(repo, env=PLANTED):
    light_check(repo, NOTE_NAMES)
    return run_no_queue(repo, env=env)


EVERY_CHECK = pytest.mark.parametrize(
    "run_check",
    [
        full_check,
        commit_check_at_a_commit,
        quick_check_of_a_small_fix,
        check_outside_the_queue,
        wt_ships_recheck,
    ],
)


@EVERY_CHECK
def test_no_check_gets_a_variable_whose_name_marks_a_secret(installed, run_check):
    done = run_check(installed)

    assert done.returncode == 0, done.stderr
    received = names_received(installed)
    assert not received & set(SECRET_NAMED)
    assert set(LOOKALIKES) <= received


@EVERY_CHECK
def test_every_check_gets_the_secrets_file_variable_empty(installed, run_check):
    # The gate's own environment has no AGENT_SECRETS_FILE here. A check's login bash
    # in the sandbox sources the secrets file unless the variable is set and empty.
    done = run_check(installed)

    assert done.returncode == 0, done.stderr
    assert "AGENT_SECRETS_FILE" in empty_names_received(installed)


def test_a_bash_the_check_starts_doesnt_source_the_secrets_back(installed, tmp_path):
    # As /etc/sandbox-persistent.sh, which BASH_ENV names, sources the secrets file.
    startup = tmp_path / "persistent.sh"
    startup.write_text("export FAKE_SOURCED_TOKEN=fake\n")
    hook(installed, "agent-pre-commit", f"bash -c {shlex.quote(NOTE_NAMES)}")

    done = commit(installed, env={"BASH_ENV": str(startup)})

    assert done.returncode == 0, done.stderr
    assert "FAKE_SOURCED_TOKEN" not in names_received(installed)


# A secrets file as bash reads it, its values fake, exporting names no part marks.
SECRETS_FILE_TEXT = """\
# export FAKE_COMMENTED=fake
export FAKE_CERT_ID="fake value"
export FAKE_APP_ID=fake; export FAKE_USER
export FAKE_A FAKE_B=fake
export -p FAKE_FLAGGED=fake
export FAKE_ADDED+=fake
export \\
  FAKE_CONTINUED=fake
export FAKE_QUOTED='FAKE_IN_A_VALUE=fake'
"""
EXPORTED = (
    *("FAKE_CERT_ID", "FAKE_APP_ID", "FAKE_USER", "FAKE_A", "FAKE_B"),
    *("FAKE_FLAGGED", "FAKE_ADDED", "FAKE_CONTINUED", "FAKE_QUOTED"),
)
NOT_EXPORTED = ("FAKE_COMMENTED", "FAKE_IN_A_VALUE")


def secrets_file_env(tmp_path):
    path = tmp_path / "secrets.sh"
    path.write_text(SECRETS_FILE_TEXT)
    return dict.fromkeys(EXPORTED + NOT_EXPORTED, "fake") | {
        "AGENT_SECRETS_FILE": str(path)
    }


@EVERY_CHECK
def test_no_check_gets_a_name_the_secrets_file_exports(installed, run_check, tmp_path):
    done = run_check(installed, env=secrets_file_env(tmp_path))

    assert done.returncode == 0, done.stderr
    received = names_received(installed)
    assert not received & set(EXPORTED)
    assert set(NOT_EXPORTED) <= received
    assert "AGENT_SECRETS_FILE" in empty_names_received(installed)  # read, then emptied


def no_file(path):
    pass


def a_directory(path):
    path.mkdir()


def unreadable(path):
    path.write_text(SECRETS_FILE_TEXT)
    path.chmod(0)


def a_pipe(path):
    os.mkfifo(path)  # a read would wait for a writer that never comes


@pytest.mark.timeout(30)
@pytest.mark.parametrize("make", [no_file, a_directory, unreadable, a_pipe])
def test_a_secrets_file_it_cant_read_strips_nothing_more(installed, tmp_path, make):
    make(tmp_path / "secrets.sh")
    hook(installed, "agent-pre-commit", NOTE_NAMES)

    done = commit(
        installed,
        env={
            "FAKE_CERT_ID": "fake",
            "AGENT_SECRETS_FILE": str(tmp_path / "secrets.sh"),
        },
    )

    assert done.returncode == 0, done.stderr
    assert "FAKE_CERT_ID" in names_received(installed)
    assert "Traceback" not in done.stderr


def test_an_empty_secrets_file_variable_strips_nothing_more(installed):
    hook(installed, "agent-pre-commit", NOTE_NAMES)

    done = commit(installed, env={"FAKE_CERT_ID": "fake", "AGENT_SECRETS_FILE": ""})

    assert done.returncode == 0, done.stderr
    assert "FAKE_CERT_ID" in names_received(installed)
    assert "AGENT_SECRETS_FILE" in empty_names_received(installed)
