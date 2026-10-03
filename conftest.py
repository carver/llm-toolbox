"""Keep every test away from the repository pytest was started in.

`git rebase --exec`, `commit -a` and hooks export GIT_DIR, GIT_INDEX_FILE and
the like. A test's throwaway repo, or the production code under test, would
inherit them and write to the real repository instead."""

import os
import re
import subprocess

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


@pytest.fixture(autouse=True)
def no_inherited_repo(monkeypatch):
    for name in list(os.environ):
        if redirects_git(name):
            monkeypatch.delenv(name)
