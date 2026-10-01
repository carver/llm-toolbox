"""Find processes by name or command line, never counting the caller.

`pkill -f PATTERN` and `pgrep -f PATTERN` also match the shell that runs
them, because the pattern is in that shell's own command line. Everything
here skips the current process and all its ancestors.
"""

import os
import re
import sys
import time
from pathlib import Path

PROC = Path("/proc")
NONE_MATCHED = 1
SEVERAL_MATCHED = 2


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _stat_fields(pid: int) -> list[bytes] | None:
    """The stat fields after the name, which is in parentheses and may contain spaces or ")"."""
    stat = _read(PROC / str(pid) / "stat")
    return None if stat is None else stat[stat.rindex(b")") + 2 :].split()


def parent_of(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return None if fields is None else int(fields[1])


def ancestors(pid: int) -> set[int]:
    seen = {pid}
    while (parent := parent_of(pid)) and parent not in seen:
        seen.add(parent)
        pid = parent
    return seen


def processes() -> dict[int, tuple[str, str]]:
    """pid -> (name, full command line) for every process we can see."""
    found = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        comm = _read(entry / "comm")
        cmdline = _read(entry / "cmdline")
        if comm is None or cmdline is None:
            continue
        name = comm.decode(errors="replace").strip()
        full = (
            cmdline.replace(b"\0", b" ").decode(errors="replace").strip() or f"[{name}]"
        )
        found[int(entry.name)] = (name, full)
    return found


def matches(pattern: str, full: bool) -> dict[int, str]:
    """pid -> command line of every match: exact name, or with `full` a regex
    over the command line. Never the caller or its ancestors."""
    regex = re.compile(pattern)
    skip = ancestors(os.getpid())
    return {
        pid: cmdline
        for pid, (name, cmdline) in processes().items()
        if pid not in skip and (regex.search(cmdline) if full else name == pattern)
    }


def exactly_one(pattern: str, full: bool, prog: str) -> tuple[int, str] | int:
    """The single match as (pid, command line), or, after explaining on
    stderr, the exit status for none (NONE_MATCHED) or several (SEVERAL_MATCHED)."""
    found = matches(pattern, full)
    if not found:
        print(f"{prog}: no process matches {pattern!r}", file=sys.stderr)
        return NONE_MATCHED
    if len(found) > 1:
        print(
            f"{prog}: {len(found)} processes match {pattern!r}; acted on none:",
            file=sys.stderr,
        )
        for pid, cmdline in sorted(found.items()):
            print(f"  {pid}  {cmdline}", file=sys.stderr)
        return SEVERAL_MATCHED
    ((pid, cmdline),) = found.items()
    return pid, cmdline


def command_line(pid: int) -> str | None:
    return processes().get(pid, (None, None))[1]


def running(pid: int) -> bool:
    """Whether `pid` still runs. A zombie has exited and only waits for its parent to reap it."""
    fields = _stat_fields(pid)
    return fields is not None and fields[0] != b"Z"


def wait_gone(pid: int, seconds: float | None, interval: float = 0.05) -> bool:
    """Waits for `pid` to exit; `None` waits forever. False on timeout."""
    deadline = None if seconds is None else time.monotonic() + seconds
    while running(pid):
        if deadline is not None and time.monotonic() > deadline:
            return False
        time.sleep(interval)
    return True
