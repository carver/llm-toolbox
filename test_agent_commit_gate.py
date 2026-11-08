"""bin/agent-commit-gate: the --no-verify refusal, the installer, and real commits through the hook line."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import warnings
from pathlib import Path

import pytest

from conftest import without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "agent-commit-gate"
loader = importlib.machinery.SourceFileLoader("agent_commit_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader("agent_commit_gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)
import fifo  # from lib/, which loading the gate put on the path


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
        and "build-state finish" in run.stderr
    )
    assert ran(installed) == ["agent", "fast", "fast"]


def quick_check(repo, body='echo quick >> "$(git rev-parse --git-dir)/ran"'):
    return hook(repo, "agent-quick-check", body)


def test_a_small_fix_commit_runs_the_quick_check_not_the_agent_check(installed):
    quick_check(installed)
    assert commit(installed).returncode == 0
    (installed / "a").write_text("still good\n")
    git(installed, "add", "a")

    run = commit(installed, "--fixup=HEAD")

    assert run.returncode == 0, run.stderr
    assert ran(installed) == ["agent", "fast", "quick", "fast"]
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


QUICK = ["quick", "fast"]
FULL = ["agent", "fast"]


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
    assert ran(installed) == ["agent", "fast"]


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
    record = installed / ".git" / gate.PASS_RECORD
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

    def start(repo):
        env = without_repo_vars(os.environ)
        env["PATH"] = os.pathsep.join([str(SCRIPT.parent), env["PATH"]])
        env["AI_AGENT"] = "test-agent"
        err = tmp_path_factory.mktemp("commit") / "stderr"
        with err.open("w") as sink:
            proc = subprocess.Popen(
                ["git", "commit", "-q", "-m", "x"],
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


def wait_or_warn(condition, what, seconds=20):
    """Poll `condition` until it holds, and return True. When `seconds` pass first,
    fail saying `what` didn't happen; but on a busy machine, its load average above its
    core count, which can stall a subprocess past any deadline, warn instead and
    return False, for the test to end there."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            message = f"{what} within {seconds} s"
            load, cores = os.getloadavg()[0], os.cpu_count()
            if load <= cores:
                pytest.fail(message)
            warnings.warn(
                f"{message}, on a busy machine (load {load:.1f}, {cores} cores), so"
                " the test ends without judging the rest"
            )
            return False
        time.sleep(0.05)
    return True


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


def finishes_or_warns(commit_run):
    return wait_or_warn(
        lambda: commit_run.poll() is not None, "the commit never finished", 30
    )


def test_an_agent_check_waits_its_turn_in_heavys_queue(
    installed, held_lane, committing
):
    commit_run = committing(installed)
    waiting = queues_or_warns(commit_run, held_lane.lane)
    # Whether heavy said so or the wait ran out, neither check ran while the lane was held.
    assert ran(installed) == [], commit_run.err()
    if not waiting:
        return
    held_lane.kill()  # not reaped: a zombie, which has ended all the same
    if not finishes_or_warns(commit_run):
        return
    err = commit_run.err()

    assert commit_run.returncode == 0, err
    assert ran(installed) == ["agent", "fast"]
    assert "waiting for the normal lane" in err and "cargo test" in err


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
    assert ran(installed) == ["agent", "fast"]
