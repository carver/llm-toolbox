""".githooks/agent-pre-commit runs only the tests a change since $CHECK_BASE can
affect, and before it starts a run that needs heavy's queue, it exits with
$CHECK_QUEUE_EXIT. agent-commit-gate sets both (`agent-commit-gate -h`); most tests
here set them as it does, CHECK_BASE to HEAD unless a test says otherwise, and run the
real hook in a scratch repo, with a fake pre-commit beside it and a fake uv first on
PATH, which note their calls, so no real check runs. Two commit through the gate."""

import os
import shutil
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import path_without, wait_until, without_repo_vars

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / ".githooks" / "agent-pre-commit"
GATE = ROOT / "bin" / "agent-commit-gate"
QUEUE_EXIT = 75
SUITE = "the whole suite"

# One file of each kind the hook tells apart.
LAYOUT = {
    "README.md": "# Tools\n",
    "GLOBAL_CLAUDE.md": "# Guidelines\n",
    "CODING_STANDARDS.md": "# Standards\n",
    "skills/coding/SKILL.md": "---\nname: coding\n---\n",
    "skill-patches/tdd.patch": "",
    "bin/tool": "#!/usr/bin/env python3\n",
    "lib/shared.py": "",
    "test_tool.py": "def test_tool():\n    pass\n",
    "conftest.py": "",
    "pytest.ini": "[pytest]\n",
    "notes/plan.txt": "a path the map doesn't name\n",
}
FAKE_UV = '#!/bin/sh\necho "uv $*" >> "$FAKE_LOG"\nexit "${FAKE_UV_EXIT:-0}"\n'
FAKE_PRE_COMMIT = '#!/bin/sh\necho pre-commit >> "$FAKE_LOG"\n'


def git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env or without_repo_vars(os.environ),
    ).stdout.strip()


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    repo = tmp_path_factory.mktemp("template") / "repo"
    for name, text in LAYOUT.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / "agent-pre-commit")
    (hooks / "pre-commit").write_text(FAKE_PRE_COMMIT)
    (hooks / "pre-commit").chmod(0o755)
    git(repo, "init", "-q")
    git(repo, "config", "core.hooksPath", ".githooks")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@t")
    # The line that sends an agent's commit through the gate, for the tests that commit.
    installed = subprocess.run(
        [str(GATE), "--install", str(repo)],
        capture_output=True,
        text=True,
        check=False,
        env=without_repo_vars(os.environ),
    )
    assert installed.returncode == 0, installed.stderr
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "init")
    return repo


@pytest.fixture
def repo(template, tmp_path):
    copy = tmp_path / "repo"
    shutil.copytree(template, copy, symlinks=True)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "uv").write_text(FAKE_UV)
    (fakes / "uv").chmod(0o755)
    return copy


def change(repo, *names):
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.read_text() + "changed\n" if path.exists() else "new\n")
        git(repo, "add", name)


@dataclass
class Run:
    code: int
    err: str
    linted: bool
    # None: pytest never ran; SUITE: it ran with no test named; else the tests named.
    tests: set | str | None


def env_for(repo, extra):
    env = without_repo_vars(os.environ)
    env["PATH"] = os.pathsep.join([str(repo.parent / "fakes"), env["PATH"]])
    env["FAKE_LOG"] = str(repo.parent / "calls")
    return env | extra


def calls(repo):
    log = repo.parent / "calls"
    return log.read_text().splitlines() if log.exists() else []


def ran_tests(repo):
    """None when pytest never ran, SUITE when it ran with no test named, else the
    tests it was given."""
    lines = calls(repo)
    runs = [
        line.split(" pytest ", 1)[1].split() for line in lines if " pytest " in line
    ]
    assert len(runs) <= 1, lines
    if not runs:
        return None
    return {arg for arg in runs[0] if arg.endswith(".py")} or SUITE


def check(repo, base="HEAD", queue_exit=None, env=None):
    """Run the hook in `repo` as the gate does: CHECK_BASE set to `base` as `git
    rev-parse` gives it (None: unset), and CHECK_QUEUE_EXIT to `queue_exit` (None:
    unset)."""
    extra = dict(env or {})
    if base is not None:
        extra["CHECK_BASE"] = git(repo, "rev-parse", base)
    if queue_exit is not None:
        extra["CHECK_QUEUE_EXIT"] = str(queue_exit)
    (repo.parent / "calls").unlink(missing_ok=True)
    done = subprocess.run(
        [str(repo / ".githooks" / "agent-pre-commit")],
        cwd=repo,
        env=env_for(repo, extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    linted = "pre-commit" in calls(repo)
    return Run(done.returncode, done.stdout + done.stderr, linted, ran_tests(repo))


def runs_the_suite_in_the_queue(repo, base="HEAD", env=None):
    """Check that the hook, run as the gate runs it first, asks for heavy's queue
    before any test, and that run again there it runs the whole suite. Returns what
    the first run printed, its reason."""
    asked = check(repo, base=base, queue_exit=QUEUE_EXIT, env=env)
    assert asked.code == QUEUE_EXIT, asked.err
    assert asked.tests is None and "heavy's queue" in asked.err
    queued = check(repo, base=base, env=env)
    assert queued.code == 0, queued.err
    assert queued.tests == SUITE and queued.linted
    return asked.err


def test_a_skills_only_change_runs_only_the_skill_tests_outside_the_queue(repo):
    change(repo, "skills/coding/SKILL.md")

    run = check(repo, queue_exit=QUEUE_EXIT)

    assert run.code == 0, run.err
    assert run.tests == {"test_skill_refs.py"}
    assert run.linted


def test_a_skill_path_with_non_ascii_letters_runs_only_the_skill_tests(repo):
    change(repo, "skills/café/SKILL.md")

    run = check(repo, queue_exit=QUEUE_EXIT)

    assert run.code == 0, run.err
    assert run.tests == {"test_skill_refs.py"}


def test_a_readme_change_runs_the_tests_that_read_it_outside_the_queue(repo):
    change(repo, "README.md")

    run = check(repo, queue_exit=QUEUE_EXIT)

    assert run.code == 0, run.err
    assert run.tests == {"test_readme_env.py", "test_skill_refs.py"}


def test_docs_changes_run_the_tests_each_path_needs(repo):
    change(repo, "skills/coding/SKILL.md", "README.md", "skill-patches/tdd.patch")

    run = check(repo, queue_exit=QUEUE_EXIT)

    assert run.code == 0, run.err
    assert run.tests == {"test_install.py", "test_readme_env.py", "test_skill_refs.py"}


@pytest.mark.parametrize("path", ["CODING_STANDARDS.md", "docs/design.md"])
def test_a_change_no_test_reads_runs_none_but_still_lints(repo, path):
    change(repo, path)

    run = check(repo, queue_exit=QUEUE_EXIT)

    assert run.code == 0, run.err
    assert run.tests is None
    assert run.linted


@pytest.mark.parametrize(
    "path", ["bin/tool", "lib/shared.py", "test_tool.py", "conftest.py", "pytest.ini"]
)
def test_a_code_change_asks_for_the_queue_then_runs_the_suite(repo, path):
    change(repo, path)

    assert path in runs_the_suite_in_the_queue(repo)


def test_an_unmapped_path_runs_the_suite(repo):
    change(repo, "notes/plan.txt")

    runs_the_suite_in_the_queue(repo)


@pytest.mark.parametrize(
    "path",
    [
        "skills/coding/test_steps.py",
        "skill-patches/conftest.py",
        "skills/coding/test_doc.txt",
    ],
)
def test_a_file_pytest_collects_runs_the_suite_even_under_a_mapped_path(repo, path):
    """The whole suite, run without file arguments, collects test files and doctest
    files (test*.txt) anywhere in the tree."""
    change(repo, path)

    runs_the_suite_in_the_queue(repo)


def test_a_code_file_moved_under_a_mapped_path_runs_the_suite(repo):
    git(repo, "mv", "bin/tool", "skills/coding/tool")

    runs_the_suite_in_the_queue(repo)


def test_a_docs_change_beside_a_code_change_runs_the_suite(repo):
    change(repo, "skills/coding/SKILL.md", "bin/tool")

    runs_the_suite_in_the_queue(repo)


def test_a_change_to_the_check_itself_runs_the_suite(repo):
    hook = repo / ".githooks" / "agent-pre-commit"
    hook.write_text(hook.read_text() + "# changed\n")
    git(repo, "add", ".githooks/agent-pre-commit")

    runs_the_suite_in_the_queue(repo)


def test_without_check_base_it_runs_the_suite(repo):
    change(repo, "skills/coding/SKILL.md")

    assert "CHECK_BASE" in runs_the_suite_in_the_queue(repo, base=None)


def test_a_base_git_cant_diff_against_runs_the_suite(repo):
    change(repo, "skills/coding/SKILL.md")
    bogus = {"CHECK_BASE": "0" * 40}

    assert "CHECK_BASE" in runs_the_suite_in_the_queue(repo, base=None, env=bogus)


def test_nothing_changed_since_the_base_runs_the_suite(repo):
    runs_the_suite_in_the_queue(repo)


def test_the_base_is_compared_with_whats_staged_not_only_with_head(repo):
    """On wt-ship's recheck, nothing is staged, and the change is what the index holds
    against an older base."""
    change(repo, "bin/tool")
    git(repo, "commit", "-q", "--no-verify", "-m", "code")
    change(repo, "README.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "docs")

    assert check(repo, base="HEAD~1", queue_exit=QUEUE_EXIT).tests == {
        "test_readme_env.py",
        "test_skill_refs.py",
    }
    assert check(repo, base="HEAD~2", queue_exit=QUEUE_EXIT).code == QUEUE_EXIT


@pytest.mark.parametrize("value", ["256", "abc", "0"])
def test_a_queue_exit_that_isnt_an_exit_status_counts_as_unset(repo, value):
    """sh keeps an exit status's low 8 bits, so 256 would exit 0: a pass."""
    change(repo, "bin/tool")

    run = check(repo, queue_exit=value)

    assert run.code == 0, run.err
    assert run.tests == SUITE


def test_failing_tests_fail_the_check(repo):
    change(repo, "skills/coding/SKILL.md")

    run = check(repo, queue_exit=QUEUE_EXIT, env={"FAKE_UV_EXIT": "1"})

    assert run.code != 0 and run.code != QUEUE_EXIT
    assert run.tests == {"test_skill_refs.py"}


def writes_to_its_temp_root(repo, then=""):
    """Swap in a fake uv that writes a file under the pytest temp root it's given, as
    pytest does, notes that root, then runs the shell lines `then`. Returns the file
    that holds the root."""
    seen = repo.parent / "temp-root"
    (repo.parent / "fakes" / "uv").write_text(
        FAKE_UV.replace(
            "exit",
            'root=${PYTEST_DEBUG_TEMPROOT:?}\nmkdir -p "$root/pytest-of-x"\n'
            f'echo f > "$root/pytest-of-x/f"\necho "$root" > "{seen}"\n{then}exit',
        )
    )
    return seen


@pytest.mark.parametrize(
    ("path", "tests"),
    [("skills/coding/SKILL.md", {"test_skill_refs.py"}), ("bin/tool", SUITE)],
    ids=["the few tests", "the whole suite"],
)
def test_each_run_keeps_its_temp_files_in_a_root_the_hook_removes(
    repo, tmp_path, path, tests
):
    """At exit, pytest deletes the oldest runs' folders in its temp root, which every
    pytest run on the machine shares unless told otherwise: after a full run of this
    suite, tens of thousands of files, minutes on the slow disk it may be on. The
    variable, unlike --basetemp, also reaches a test's own nested pytest run."""
    shared = tmp_path / "shared-root"
    shared.mkdir()
    seen = writes_to_its_temp_root(repo)
    change(repo, path)

    run = check(repo, env={"PYTEST_DEBUG_TEMPROOT": str(shared)})

    assert run.code == 0, run.err
    assert run.tests == tests
    assert list(shared.iterdir()) == []
    assert not Path(seen.read_text().strip()).exists()


def test_a_stopped_run_still_removes_its_temp_root(repo, tmp_path):
    """heavy stops a check with SIGTERM to its process group, and sh runs no EXIT
    trap on a signal it doesn't trap: a stopped full run would leave hundreds of MB
    on the sandbox's own disk."""
    started = tmp_path / "started"
    seen = writes_to_its_temp_root(repo, then=f'touch "{started}"\nsleep 60\n')
    change(repo, "skills/coding/SKILL.md")
    env = env_for(
        repo,
        {
            "CHECK_BASE": git(repo, "rev-parse", "HEAD"),
            "PYTEST_DEBUG_TEMPROOT": str(tmp_path),
        },
    )
    hook = subprocess.Popen(
        [str(repo / ".githooks" / "agent-pre-commit")],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_until(started.exists, "the fake pytest started", seconds=30)
        os.killpg(hook.pid, signal.SIGTERM)
        hook.wait(timeout=30)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(hook.pid, signal.SIGKILL)

    assert not Path(seen.read_text().strip()).exists()


def test_the_tests_see_neither_variable(repo, tmp_path):
    """A test that commits through the gate would hand them on to its own check."""
    seen = tmp_path / "seen"
    (repo.parent / "fakes" / "uv").write_text(
        FAKE_UV.replace(
            "exit",
            f'echo "base=${{CHECK_BASE-}} queue=${{CHECK_QUEUE_EXIT-}}" > "{seen}"\nexit',
        )
    )
    change(repo, "skills/coding/SKILL.md")

    assert check(repo, queue_exit=QUEUE_EXIT).code == 0
    assert seen.read_text() == "base= queue=\n"


def gate_commit(repo):
    """An agent's `git commit` in `repo`, through agent-commit-gate's line in its
    pre-commit hook, with this checkout's gate on PATH."""
    (repo.parent / "calls").unlink(missing_ok=True)
    env = env_for(repo, {"AI_AGENT": "test-agent"})
    env["PATH"] = os.pathsep.join(
        [str(GATE.parent), path_without(GATE.name, env["PATH"])]
    )
    return subprocess.run(
        ["git", "commit", "-q", "-m", "x"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_skills_only_commit_passes_outside_heavys_queue(repo):
    # The first commit through the gate passes in full, which makes it the next one's
    # CHECK_BASE.
    change(repo, "bin/tool")
    assert gate_commit(repo).returncode == 0
    change(repo, "skills/coding/SKILL.md")

    done = gate_commit(repo)

    assert done.returncode == 0, done.stderr
    assert "agent-pre-commit: passed" in done.stderr
    assert "outside heavy's queue" in done.stderr
    assert ran_tests(repo) == {"test_skill_refs.py"}


def test_a_code_commit_runs_the_suite_in_heavys_queue(repo):
    change(repo, "bin/tool")

    done = gate_commit(repo)

    assert done.returncode == 0, done.stderr
    assert "needs heavy's queue" in done.stderr
    assert "agent-pre-commit: passed" in done.stderr
    assert "outside heavy's queue" not in done.stderr
    assert ran_tests(repo) == SUITE


def test_the_commit_check_runs_no_test_and_never_waits_in_the_queue():
    """What agent-commit-gate runs at an agent's commit in agent-pre-commit's place:
    ruff ran in the pre-commit hook, and wt-ship runs the suite when the commit lands.
    It names CHECK_QUEUE_EXIT, so the gate runs it outside heavy's queue."""
    check = Path(__file__).resolve().parent / ".githooks" / "agent-commit-check"

    done = subprocess.run([str(check)], capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stderr
    assert "CHECK_QUEUE_EXIT" in check.read_text()
