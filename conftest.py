"""Fixtures shared by the tests.

Every test is kept away from the repository pytest was started in: `git rebase
--exec`, `commit -a` and hooks export GIT_DIR, GIT_INDEX_FILE and the like, and a
test's throwaway repo, or the production code under test, would inherit them and
write to the real repository instead. `shell_and_child` starts a wrapper shell and
its job, for the process tools' tests."""

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


# Set when an agent commits; a test's pre-commit hook would rerun the suite.
AGENT_VARS = ("AI_AGENT", "AGENT_PRE_COMMIT")


@pytest.fixture(autouse=True)
def no_inherited_repo(monkeypatch):
    for name in list(os.environ):
        if redirects_git(name):
            monkeypatch.delenv(name)
    for name in AGENT_VARS:
        monkeypatch.delenv(name, raising=False)


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
        # Not just the first child: a shell sourcing $BASH_ENV can start others first.
        children = Path(f"/proc/{shell.pid}/task/{shell.pid}/children")
        for _ in range(500):
            for pid in children.read_text().split():
                cmdline = Path(f"/proc/{pid}/cmdline")
                if cmdline.exists() and marker.encode() in cmdline.read_bytes():
                    return shell.pid, int(pid)
            time.sleep(0.01)
        raise AssertionError("the shell's child never started")

    yield start
    for shell in groups:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait()
