"""Fixtures shared by the tests.

Every test is kept away from the repository pytest was started in: `git rebase
--exec`, `commit -a` and hooks export GIT_DIR, GIT_INDEX_FILE and the like, and a
test's throwaway repo, or the production code under test, would inherit them and
write to the real repository instead. `shell_and_child` starts a wrapper shell and
its job, for the process tools' tests, and `started_child` finds a process's child
by an argument it was started with.

The commit hook runs the suite in a copy of the index outside any git checkout, so a test
that needs a repository makes its own rather than reading this one's."""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Not all of these are repo-local, but each can send a git call elsewhere.
ALWAYS_UNSET = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_PREFIX",
    "GIT_REFLOG_ACTION",
    "GIT_EXEC_PATH",
}
CONFIG_PAIR = re.compile(r"GIT_CONFIG_(KEY|VALUE)_\d+")


def repo_local_vars():
    """The variables git itself lists as repo-local, plus ALWAYS_UNSET."""
    listed = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return ALWAYS_UNSET | set(listed)


REPO_VARS = repo_local_vars()


def redirects_git(name):
    return name in REPO_VARS or bool(CONFIG_PAIR.fullmatch(name))


def without_repo_vars(env):
    """`env` minus every variable that could point git at another repository."""
    return {k: v for k, v in env.items() if not redirects_git(k)}


def path_without(tool, path=None):
    """`path` (default: this process's PATH) without the folders that hold an
    executable named `tool`, so a tool that looks for it there can't find it."""
    dirs = (os.environ["PATH"] if path is None else path).split(os.pathsep)
    return os.pathsep.join(d for d in dirs if not Path(d, tool).exists())


# Set when an agent commits; a test's pre-commit hook would rerun the suite. CHECK_BASE
# names a commit of the repo being committed, which the gate hands the suite's own check.
AGENT_VARS = ("AI_AGENT", "AGENT_PRE_COMMIT", "CHECK_BASE")


@pytest.fixture(scope="session", autouse=True)
def no_inherited_repo():
    """Session-wide, so module-wide fixtures that make repos run without these too."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name in list(os.environ):
            if redirects_git(name):
                monkeypatch.delenv(name)
        for name in AGENT_VARS:
            monkeypatch.delenv(name, raising=False)
        yield


@pytest.fixture(autouse=True)
def private_heavy_queue(tmp_path_factory, monkeypatch):
    """Each test gets a heavy queue of its own whose cores are always quiet, so a test
    that commits through agent-commit-gate neither joins the VM's real queue (behind
    real builds) nor waits on another test's. HEAVY_SLOT goes too: the suite may be
    running inside a commit check that holds the real slot."""
    folder = tmp_path_factory.mktemp("heavy")
    (folder / "busy").write_text("0\n")
    monkeypatch.setenv("HEAVY_DIR", str(folder / "queue"))
    monkeypatch.setenv("HEAVY_BUSY", str(folder / "busy"))
    monkeypatch.setenv("HEAVY_QUIET_SECONDS", "0")
    monkeypatch.setenv("HEAVY_POLL", "0.01")
    monkeypatch.delenv("HEAVY_SLOT", raising=False)


def started_child(parent: int, arg: str) -> int:
    """The pid of `parent`'s child that has `arg` as one of its arguments, waiting up
    to 5 seconds for it to start. While a shell sources $BASH_ENV it forks short-lived
    copies of itself to run commands (pgrep, say), and it forks again before exec'ing
    each job; until they exec, those copies have the shell's argv and process name, so
    `arg` must be a whole argument only the real child has, not a piece of the shell's
    command line. A child that exits while being read is skipped."""
    children = Path(f"/proc/{parent}/task/{parent}/children")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for pid in children.read_text().split():
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if arg.encode() in cmdline.rstrip(b"\0").split(b"\0"):
                return int(pid)
        time.sleep(0.01)
    raise AssertionError(
        f"process {parent}'s child with argument {arg!r} never started"
    )


@pytest.fixture
def shell_and_child():
    """Starts `bash -c "python3 ... MARKER; true"` and returns (shell pid, child pid): a
    wrapper shell whose own command line matches whatever its child's does, like the
    shell a background job is started from. Both are killed afterwards."""
    groups = []

    def start(marker):
        shell = subprocess.Popen(
            [
                "bash",
                "-c",
                f"{sys.executable} -c 'import time; time.sleep(300)' {marker}; true",
            ],
            start_new_session=True,
        )
        groups.append(shell)
        return shell.pid, started_child(shell.pid, marker)

    yield start
    for shell in groups:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait()
