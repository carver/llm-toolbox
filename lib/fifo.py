"""A first-come-first-served queue of processes in a folder, and the slot its head
takes: wt-ship's queue for a main tree's flag, and heavy's for CPU-heavy work.

Each entry and each slot names the process holding it, on its first line:
`<session> <since> host=<host> pid=<pid> start=<start time>`. One whose process ran
on this host and has ended is cleared, under a lock in the queue's folder, so two
waiters never clear the same dead slot and delete the next holder's. Lines after the
first are the tool's own (heavy keeps the job's folder and command there).

The lock is held briefly, many times a second across the VM, so its holder must never
stop while it holds it: the stop signals a terminal sends (Ctrl-Z's SIGTSTP, SIGTTIN,
SIGTTOU) wait, blocked, until it lets go. SIGSTOP can't wait, so the holder records
itself in `.lock.holder` beside the lock, and a process that has waited for the lock
FIFO_LOCK_SECONDS (5; the tests shorten it) prints a line on stderr naming the holder,
and how to continue it when it's stopped, then keeps waiting.
"""

import fcntl
import os
import re
import signal
import socket
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# An entry is named for when its waiter joined, so names sort in queue order. Other
# names (the lock, an entry still being written) aren't entries.
ENTRY = re.compile(r"\d{20}-\d+")
# What a terminal sends to stop a process; SIGSTOP can't be blocked.
TERMINAL_STOPS = {signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU}
HOLDER = ".lock.holder"  # the lock's holder's record, beside the lock
FIRST_NAP, LAST_NAP = 0.001, 0.05  # seconds between tries for a held lock


def stat_fields(pid):
    """Process pid's /proc/PID/stat fields from its state on (field 3), None when
    there is no such process, [] when it can't be read (a /proc that hides other
    users')."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return []
    return stat.rsplit(")", 1)[1].split()


def started(pid):
    """When process pid started (field 22 of /proc/PID/stat); None when there is no
    such process or it has ended (a zombie its parent hasn't reaped), "" when it
    can't be read (a /proc that hides other users')."""
    fields = stat_fields(pid)
    if fields is None:
        return None
    if not fields:
        return ""
    return None if fields[0] == "Z" else fields[19]


def stopped(pid):
    """Whether process pid is stopped (state T), as SIGSTOP or Ctrl-Z leaves it."""
    fields = stat_fields(pid)
    return bool(fields) and fields[0] == "T"


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
        return cls(session, stamp(), socket.gethostname(), pid, started(pid))

    @classmethod
    def mine(cls):
        """This process, for this Claude Code session."""
        return cls.now(
            os.environ.get("CLAUDE_CODE_SESSION_ID", "") or "unknown-session"
        )

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


def stamp():
    """Now, as a holder's `since`: local time to the second, with its offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read(path):
    """The text at path, or None when it's gone."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


@contextmanager
def locked(folder):
    """Hold the lock in folder, with the terminal's stop signals blocked until it's
    released. One already blocked stays so: a lock taken inside another keeps the
    outer one's block."""
    record = folder / HOLDER
    with (folder / ".lock").open("a") as lock:
        before = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINAL_STOPS)
        try:
            wait_for(lock, folder, callers_mask=before)
            try:
                with suppress(OSError):
                    write_whole(record, Holder.mine().line())
                yield
            finally:
                with suppress(OSError):
                    record.unlink(missing_ok=True)
        finally:
            # Also when an exception came between the flock and the try above. A
            # lock not held is a no-op.
            fcntl.flock(lock, fcntl.LOCK_UN)
            signal.pthread_sigmask(signal.SIG_SETMASK, before)


def wait_for(lock, folder, callers_mask):
    """Take the lock on file lock, called with the stop signals blocked, and return
    with them blocked still. flock has no timeout, so a held lock is tried again after
    naps that grow to LAST_NAP, with the caller's signal mask back for each nap: a
    Ctrl-Z stops a waiter that holds no other lock at once, and one inside another
    lock once it lets go of that. Every FIFO_LOCK_SECONDS of the wait, it names a
    holder not named yet."""
    patience = float(os.environ.get("FIFO_LOCK_SECONDS", "5"))
    look_at = time.monotonic() + patience
    watch = Watch(folder)
    nap = FIRST_NAP
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        if time.monotonic() >= look_at:
            watch.look()
            look_at = time.monotonic() + patience
        signal.pthread_sigmask(signal.SIG_SETMASK, callers_mask)
        time.sleep(nap)
        signal.pthread_sigmask(signal.SIG_BLOCK, TERMINAL_STOPS)
        nap = min(nap * 2, LAST_NAP)


class Watch:
    """A waiter's looks at the holder of the lock in folder. It names each holder
    once, on stderr, and a holder with no record only at the second look in a row
    that finds none: a holder has none for a moment as it takes the lock and as it
    lets go."""

    def __init__(self, folder):
        self.folder = folder
        self.named = set()  # the holders' record lines, None for no record
        self.unrecorded = False  # the last look found no record

    def look(self):
        holder = recorded(self.folder / HOLDER)
        again, self.unrecorded = self.unrecorded, holder is None
        key = None if holder is None else holder.line()
        if (holder is None and not again) or key in self.named:
            return
        self.named.add(key)
        print(holder_line(self.folder, holder), file=sys.stderr, flush=True)


def recorded(path):
    """The holder the record at path names, or None when it can't be read or its
    process has ended (killed while it held the lock)."""
    try:
        holder = Holder.parse(path.read_text())
    except (OSError, UnicodeDecodeError):
        return None
    return None if holder.pid is None or holder.gone() else holder


def holder_line(folder, holder):
    """The line naming the holder of the lock in folder (None: it left no record)."""
    me = f"{Path(sys.argv[0]).name}: waiting for the lock in {folder}"
    if holder is None:
        return f"{me}, whose holder left no record of itself."
    where = "" if holder.host == socket.gethostname() else f" on {holder.host}"
    line = (
        f"{me}, held since {holder.since[11:19]} by PID {holder.pid}{where}"
        f" (session {holder.session})"
    )
    if not where and stopped(holder.pid):
        line += f", which is stopped: `kill -CONT {holder.pid}` continues it"
    return line + "."


# What a front entry's number is less than its join time by: every front entry sorts
# ahead of every ordinary one, and front ones keep their order among themselves.
FRONT = 10**18


def join(folder, holder, details="", front=False):
    """Add holder's entry to the queue in folder, with details on the lines after its
    first, written whole so a reader never sees it half written. Its path. The queue
    goes in the order of the entries' names, which start with the join time; front
    puts the entry ahead of every ordinary one, after earlier front ones."""
    folder.mkdir(parents=True, exist_ok=True)
    number = time.time_ns() - (FRONT if front else 0)
    entry = folder / f"{number:020d}-{os.getpid()}"
    write_whole(entry, holder.line() + details)
    return entry


def write_whole(path, text):
    """Write text to path whole: beside it first, then renamed into place, so a
    reader never finds it half written. An entry rewritten so keeps its place."""
    part = path.parent / f".{path.name}.{os.getpid()}.part"
    try:
        part.write_text(text)
        part.rename(path)
    finally:
        part.unlink(missing_ok=True)


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


def slot_text(slot, folder, stale=Holder.gone):
    """The text of the slot at path slot, or None when it's free. A slot whose holder
    is stale (by default, its process has ended) is cleared first, under the queue's
    lock."""
    with locked(folder):
        text = read(slot)
        if text is not None and stale(Holder.parse(text)):
            slot.unlink()
            return None
    return text


def take(slot, text, folder):
    """Make the slot at path slot hold text: True when it was free, False when another
    process holds it. The text goes in whole: written beside the queue in folder, then
    linked into place, so a reader never finds a slot half written."""
    part = folder / f".slot.{os.getpid()}.part"
    part.write_text(text)
    try:
        os.link(part, slot)
    except FileExistsError:
        return False
    finally:
        part.unlink(missing_ok=True)
    return True


def release(slot, text):
    """Free the slot while it still holds text, this holder's; another's stays."""
    if read(slot) == text:
        slot.unlink(missing_ok=True)
