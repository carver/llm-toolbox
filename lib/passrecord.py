"""The record of trees that passed a repo's agent check, agent-pre-commit.

It lives in the repo's common git dir, which every worktree shares: a line for each
tree that passed, `<tree> <the check's fingerprint>`, newest last, the last RECORD_SIZE.
The fingerprint is the SHA-1 of the check's bytes, so an edited check reuses no pass of
its old self. agent-commit-gate writes it and reads it to skip a check or find
CHECK_BASE; wt-ship reads it, through lib/commitcheck.py, to find the commits it lands
that it must check.
"""

import fcntl
import hashlib
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

PASS_RECORD = "agent-pre-commit-passed"
RECORD_SIZE = 1000
# How long a pass waits for another's write to the record before it goes unrecorded.
RECORD_LOCK_SECONDS = 5


def record_path():
    """The pass record's path, in the common git dir; None outside a repository."""
    run = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(run.stdout.strip(), PASS_RECORD) if run.returncode == 0 else None


def recorded(record):
    """The record's keys, oldest first; none when it's missing or can't be read, which
    only costs fuller runs."""
    try:
        return record.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def fingerprint(content):
    """What a pass on the record names the check by: the SHA-1 of its bytes."""
    return hashlib.sha1(content).hexdigest()


def pass_key(tree, fingerprint):
    """A pass's line on the record: the tree, and the check's fingerprint."""
    return f"{tree} {fingerprint}"


def passed(tree, check):
    """Whether `tree` passed the agent check at `check`, as its bytes are now, by the
    record of the repository in the current directory. No when there's no record or
    the check can't be read."""
    record = record_path()
    try:
        key = pass_key(tree, fingerprint(Path(check).read_bytes()))
    except OSError:
        return False
    return record is not None and key in recorded(record)


@contextmanager
def record_lock(record):
    """Hold the lock beside the record; OSError when another process holds it past
    RECORD_LOCK_SECONDS. It never blocks outright: the record lives on a shared mount,
    where a holder stuck in I/O would stall every commit's pass behind it."""
    deadline = time.monotonic() + RECORD_LOCK_SECONDS
    with open(record.with_name(f"{record.name}.lock"), "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("another commit holds its lock") from None
                time.sleep(0.05)
        yield


def remember(record, key):
    """Add key to the record, newest last, keeping the last RECORD_SIZE: rewritten
    whole and renamed into place, under the record's lock, so passes in two worktrees
    keep both. A lock held too long, or a failed write, leaves the pass unrecorded,
    which costs a fuller run later, and says so."""
    part = record.with_name(f"{record.name}.{os.getpid()}")
    try:
        with record_lock(record):
            kept = [line for line in recorded(record) if line != key]
            kept = [*kept[-(RECORD_SIZE - 1) :], key]
            part.write_text("".join(f"{line}\n" for line in kept))
            part.replace(record)
    except OSError as error:
        part.unlink(missing_ok=True)
        print(f"agent-pre-commit: passed; couldn't record it: {error}", file=sys.stderr)
