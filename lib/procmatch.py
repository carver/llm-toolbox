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
LISTED_CHARS = 200
LISTED_HEAD = 80  # of those, the command line's start
LISTED_CONTEXT = 40  # on each side of the match


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _stat_fields(pid: int) -> list[bytes] | None:
    """The stat fields after the name, which is in parentheses and may contain spaces or ")"."""
    stat = _read(PROC / str(pid) / "stat")
    return None if stat is None else stat[stat.rindex(b")") + 2 :].split()


def elapsed(pid: int) -> float | None:
    """Seconds since `pid` started, or None if it is gone."""
    fields = _stat_fields(pid)
    uptime = _read(PROC / "uptime")
    if fields is None or uptime is None:
        return None
    started = int(fields[19]) / os.sysconf("SC_CLK_TCK")  # stat field 22, starttime
    return max(0.0, float(uptime.split()[0]) - started)


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
    regex = re.compile(pattern) if full else None
    skip = ancestors(os.getpid())
    return {
        pid: cmdline
        for pid, (name, cmdline) in processes().items()
        if pid not in skip and (regex.search(cmdline) if regex else name == pattern)
    }


def shortened(cmdline: str, pattern: str, full: bool) -> str:
    """`cmdline` short enough to list: whole if it fits, else its start and,
    with `full`, the text around the match, so each line shows why it matched.
    Newlines and tabs become spaces, so each process stays on one line."""
    cmdline = re.sub(r"[\r\n\t]+", " ", cmdline)
    if len(cmdline) <= LISTED_CHARS:
        return cmdline
    note = f"…[{len(cmdline)} chars]"
    found = re.search(pattern, cmdline) if full else None
    if found is None or found.end() <= LISTED_HEAD:
        return cmdline[:LISTED_CHARS] + note
    start = max(found.start() - LISTED_CONTEXT, LISTED_HEAD)
    end = min(
        found.end() + LISTED_CONTEXT, len(cmdline), start + LISTED_CHARS - LISTED_HEAD
    )
    gap = "…" if start > LISTED_HEAD else ""
    return cmdline[:LISTED_HEAD] + gap + cmdline[start:end] + note


def none_matched(pattern: str, prog: str, full: bool) -> int:
    """Says nothing matched, on stderr, and returns the exit status for it. A name that
    matches nothing but would match command lines with -f gets those listed: a common
    slip is passing a command line without -f."""
    print(f"{prog}: no process matches {pattern!r}", file=sys.stderr)
    if not full:
        try:
            would = matches(pattern, full=True)
        except re.error:
            would = {}
        if would:
            print(
                f"{prog}: without -f the pattern is an exact process name;"
                " with -f it would match:",
                file=sys.stderr,
            )
            for pid, cmdline in sorted(would.items()):
                print(f"  {pid}  {shortened(cmdline, pattern, True)}", file=sys.stderr)
    return NONE_MATCHED


def exactly_one(pattern: str, full: bool, prog: str) -> tuple[int, str] | int:
    """The single match as (pid, command line), or, after explaining on
    stderr, the exit status for none (NONE_MATCHED) or several (SEVERAL_MATCHED)."""
    found = matches(pattern, full)
    if not found:
        return none_matched(pattern, prog, full)
    if len(found) > 1:
        print(
            f"{prog}: {len(found)} processes match {pattern!r}; acted on none:",
            file=sys.stderr,
        )
        for pid, cmdline in sorted(found.items()):
            print(f"  {pid}  {shortened(cmdline, pattern, full)}", file=sys.stderr)
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
