"""What two or more tools need to know about a repo's GitHub remote and the workflows
it leaves out, and the cut that makes a name GitHub hands back safe to print.

`github_repo` raises `NotOnGitHub`, which each caller maps to its own failure: ci-status
says nothing, gh-ci-wait exits 2."""

import re
import subprocess

# A GitHub remote's URL, by HTTPS or SSH, and the OWNER/REPO in it.
GITHUB_URL = re.compile(r"(?:^|[@/])github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
UNSAFE = re.compile(r"[^\w .:/-]")
NAME_LENGTH = 40
IGNORE_KEY = "ci-status.ignore"


class NotOnGitHub(Exception):
    """The remote is missing, or its URL isn't on github.com."""


def github_repo(folder: str, remote: str) -> str:
    """OWNER/REPO of `remote` in the repo at `folder`."""
    got = subprocess.run(
        ["git", "-C", folder, "remote", "get-url", remote],
        capture_output=True,
        text=True,
        check=False,
    )
    match = GITHUB_URL.search(got.stdout.strip()) if got.returncode == 0 else None
    if not match:
        raise NotOnGitHub(f"{remote} isn't on GitHub here")
    return match.group(1)


def ignored_workflows(folder: str) -> set[str]:
    """The workflow file names `git config ci-status.ignore` lists, one per value."""
    got = subprocess.run(
        ["git", "-C", folder, "config", "--get-all", IGNORE_KEY],
        capture_output=True,
        text=True,
        check=False,
    )
    return set(got.stdout.splitlines()) if got.returncode == 0 else set()


def plain(name: str) -> str:
    """A name anyone who writes a workflow file writes (a job's, a step's, the file's),
    cut to plain characters, 40 at most, before it reaches an agent's context."""
    return UNSAFE.sub("", name)[:NAME_LENGTH]
