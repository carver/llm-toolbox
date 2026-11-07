"""A first-come-first-served queue of processes in a folder, and the slot its head
takes: wt-ship's queue for a main tree's flag, and heavy's for CPU-heavy work.

Each entry and each slot names the process holding it, on its first line:
`<session> <since> host=<host> pid=<pid> start=<start time>`. One whose process ran
on this host and has ended is cleared, under a lock in the queue's folder, so two
waiters never clear the same dead slot and delete the next holder's. Lines after the
first are the tool's own (heavy keeps the job's folder and command there).
"""

import fcntl
import os
import re
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# An entry is named for when its waiter joined, so names sort in queue order. Other
# names (the lock, an entry still being written) aren't entries.
ENTRY = re.compile(r"\d{20}-\d+")


def started(pid):
    """When process pid started (field 22 of /proc/PID/stat); None when there is no
    such process, "" when it can't be read (a /proc that hides other users')."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    return stat.rsplit(")", 1)[1].split()[19]


class Holder(NamedTuple):
    """Who holds a slot or a queue entry: the session and since when, then the host,
    PID and start time of the process holding it."""

    session: str
    since: str
    host: str | None = None
    pid: str | None = None
    start: str | None = None

    @classmethod
    def now(cls, session):
        pid = str(os.getpid())
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        return cls(session, stamp, socket.gethostname(), pid, started(pid))

    @classmethod
    def parse(cls, text):
        words = text.split("\n", 1)[0].split()
        fields = dict(word.split("=", 1) for word in words[2:] if "=" in word)
        return cls(
            words[0] if words else "?",
            words[1] if len(words) > 1 else "?",
            fields.get("host"),
            fields.get("pid"),
            fields.get("start"),
        )

    def line(self):
        return (
            f"{self.session} {self.since} host={self.host} pid={self.pid}"
            f" start={self.start}\n"
        )

    def gone(self):
        """Whether its process has certainly ended: it ran on this host, and its PID
        is free or names a process that started at another time. A holder without
        those fields, or from another host, may be alive."""
        if None in (self.host, self.pid, self.start) or not self.pid.isdigit():
            return False
        if self.host != socket.gethostname():
            return False
        now = started(self.pid)
        return now is None or (now != "" and now != self.start)

    def __str__(self):
        return f"{self.session}, since {self.since}"


def read(path):
    """The text at path, or None when it's gone."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


@contextmanager
def locked(folder):
    """Hold the queue's lock while clearing a dead slot."""
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def join(folder, holder, details=""):
    """Add holder's entry to the queue in folder, with details on the lines after its
    first, written whole so a reader never sees it half written. Its path."""
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / f"{time.time_ns():020d}-{os.getpid()}"
    part = folder / f".{entry.name}.part"
    part.write_text(holder.line() + details)
    part.rename(entry)
    return entry


def ahead(folder, entry):
    """The live waiters that joined before entry (all of them, when entry is None),
    oldest first, as (entry path, its text); ended ones are removed."""
    found = []
    for other in sorted(folder.iterdir()):
        if entry is not None and other.name >= entry.name:
            break
        if not ENTRY.fullmatch(other.name) or not other.is_file():
            continue
        text = read(other)
        if text is None:
            continue
        if Holder.parse(text).gone():
            other.unlink(missing_ok=True)
        else:
            found.append((other, text))
    return found


def holder_of(slot, folder):
    """The text of the slot at path slot, or None when it's free; a slot whose process
    has ended is cleared first, under the queue's lock."""
    with locked(folder):
        text = read(slot)
        if text is not None and Holder.parse(text).gone():
            slot.unlink()
            return None
    return text


def take(slot, text):
    """Make the slot at path slot hold text: True when it was free, False when another
    process holds it."""
    try:
        with slot.open("x") as file:
            file.write(text)
    except FileExistsError:
        return False
    return True


def release(slot, text):
    """Free the slot while it still holds text, this holder's; another's stays."""
    if read(slot) == text:
        slot.unlink(missing_ok=True)
