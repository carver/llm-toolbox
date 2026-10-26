"""bin/agent-commit-gate: the --no-verify refusal, the installer, and real commits through the hook line."""

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


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.hooksPath", ".githooks")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@t")
    (tmp_path / ".githooks").mkdir()
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


def commit(repo, *args, agent=True, gate_on_path=True):
    env = without_repo_vars(os.environ)
    # The hook line calls agent-commit-gate from PATH: this checkout's, not the installed one.
    dirs = [
        d for d in env["PATH"].split(os.pathsep) if not Path(d, SCRIPT.name).exists()
    ]
    if gate_on_path:
        dirs.insert(0, str(SCRIPT.parent))
    env["PATH"] = os.pathsep.join(dirs)
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
    "path",
    [".githooks/pre-commit", "templates/egui-app/.githooks/pre-commit"],
)
def test_checked_in_hooks_carry_the_current_line(path):
    assert gate.LINE in (SCRIPT.parent.parent / path).read_text()


def test_install_upgrades_the_old_line(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    old = (
        'if [ -n "${AI_AGENT:-}" ] && [ -z "${AGENT_PRE_COMMIT:-}" ]; then '
        'AGENT_PRE_COMMIT=1 "$(dirname "$0")/agent-pre-commit" || exit; fi'
    )
    pre = hook(repo, "pre-commit", f"{gate.COMMENT}\n{old}\necho fast")
    assert install(repo).returncode == 0
    text = pre.read_text()
    assert old not in text and gate.LINE in text
    assert text.splitlines() == ["#!/bin/sh", gate.COMMENT, gate.LINE, "echo fast"]


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


@pytest.fixture
def installed(repo):
    """A repo whose agent check fails when the committed `a` says `bad`."""
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


def ran(repo):
    path = repo / ".git" / "ran"
    return path.read_text().split() if path.exists() else []


def test_an_agent_commit_runs_the_agent_check_then_the_fast_one(installed):
    assert commit(installed).returncode == 0
    assert ran(installed) == ["agent", "fast"]


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
    assert ran(installed) == ["agent", "fast", "fast"]
    assert "already passed" in reworded.stderr


def test_a_teammates_agent_without_the_gate_runs_the_check_every_time(installed):
    """The hook is shared; agent-commit-gate is one person's tool."""
    assert commit(installed, gate_on_path=False).returncode == 0
    assert commit(installed, "--amend", gate_on_path=False).returncode == 0
    assert ran(installed) == ["agent", "fast", "agent", "fast"]


def test_a_changed_tree_runs_the_agent_check_again(installed):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("better\n")
    assert commit(installed, "--amend", "-a").returncode == 0
    assert ran(installed) == ["agent", "fast", "agent", "fast"]


def test_a_failed_agent_check_runs_again_on_the_same_tree(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")
    assert commit(installed).returncode != 0
    assert commit(installed).returncode != 0
    assert ran(installed) == ["agent", "agent"]


def test_a_pass_with_unstaged_changes_is_not_remembered(installed):
    """Some agent checks read the working tree, so their pass says nothing of the index."""
    assert commit(installed).returncode == 0
    (installed / "a").write_text("staged\n")
    git(installed, "add", "a")
    (installed / "a").write_text("unstaged\n")
    assert commit(installed).returncode == 0
    assert commit(installed, "--amend").returncode == 0
    assert ran(installed) == ["agent", "fast", "agent", "fast", "agent", "fast"]


def test_a_changed_agent_check_runs_again_on_the_same_tree(installed):
    assert commit(installed).returncode == 0
    check = installed / ".githooks" / "agent-pre-commit"
    check.write_text(check.read_text() + "\n# stricter now\n")
    assert commit(installed, "--amend").returncode == 0
    assert ran(installed) == ["agent", "fast", "agent", "fast"]


def test_an_agent_check_that_runs_pre_commit_does_not_loop(repo):
    hook(
        repo,
        "agent-pre-commit",
        'echo agent >> "$(git rev-parse --git-dir)/ran"\n"$(dirname "$0")/pre-commit"',
    )
    hook(repo, "pre-commit", 'echo fast >> "$(git rev-parse --git-dir)/ran"')
    install(repo)
    assert commit(repo, "--allow-empty").returncode == 0
    assert ran(repo) == ["agent", "fast", "fast"]


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
def test_a_fix_commit_skips_the_agent_check(installed, flag):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")

    run = commit(installed, flag)

    assert run.returncode == 0, run.stderr
    assert (
        "skipped for a fix commit" in run.stderr and "build-state finish" in run.stderr
    )
    assert ran(installed) == ["agent", "fast", "fast"]


def test_a_fixup_subject_given_with_m_skips_the_agent_check(installed):
    assert commit(installed).returncode == 0
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")
    env = without_repo_vars(os.environ)
    env["PATH"] = os.pathsep.join([str(SCRIPT.parent), env["PATH"]])
    env["AI_AGENT"] = "test-agent"

    run = git(installed, "commit", "-q", "-m", "fixup! x", env=env, check=False)

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["agent", "fast", "fast"]


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


def test_install_upgrades_the_line_without_the_refusal(repo):
    hook(repo, "agent-pre-commit", "exit 0")
    pre = hook(repo, "pre-commit", f"{gate.COMMENT}\n{gate.OLD_LINES[1]}\necho fast")

    assert install(repo).returncode == 0

    assert pre.read_text().splitlines() == [
        "#!/bin/sh",
        gate.COMMENT,
        gate.LINE,
        "echo fast",
    ]


def test_without_the_gate_a_refusal_still_ends_with_commit_refused(installed):
    (installed / "a").write_text("bad\n")
    git(installed, "add", "a")

    run = commit(installed, gate_on_path=False)

    assert run.returncode != 0
    assert run.stderr.rstrip().splitlines()[-1].startswith("COMMIT REFUSED")
