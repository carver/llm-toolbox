"""Find processes by name or command line, never counting the caller.

`pkill -f PATTERN` and `pgrep -f PATTERN` also match the shell that runs
them, because the pattern is in that shell's own command line. Everything
here skips the current process and all its ancestors.
"""

import os
import re
import sys
import time
from collections.abc import Iterator
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


def parents(pid: int) -> Iterator[int]:
    """`pid`'s parent, then its parent's, and so on up the tree, stopping at a loop."""
    seen = {pid}
    while (parent := parent_of(pid)) and parent not in seen:
        yield parent
        seen.add(parent)
        pid = parent


def clock(seconds: float) -> str:
    """Like ps's etime: MM:SS, H:MM:SS, or D-HH:MM:SS."""
    rest = int(seconds)
    days, rest = divmod(rest, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}-{hours:02}:{minutes:02}:{secs:02}"
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes:02}:{secs:02}"


def ancestors(pid: int) -> set[int]:
    return {pid, *parents(pid)}


def running_in(folder: Path) -> dict[int, str]:
    """pid -> command line of each process we can read whose current directory or
    executable is `folder` or inside it, even after that was deleted. Never the
    caller, its ancestors, or its pipeline siblings (`wt-ship | tee`): processes in
    the caller's group whose parent is one of its ancestors."""
    folder = folder.resolve()
    mine = ancestors(os.getpid())
    group = os.getpgrp()
    found = {}
    for pid, (_, cmdline) in processes().items():
        fields = _stat_fields(pid)
        if pid in mine or fields is None:
            continue
        if int(fields[2]) == group and int(fields[1]) in mine:  # stat's pgrp, ppid
            continue
        if any(_link_inside(PROC / str(pid) / link, folder) for link in ("cwd", "exe")):
            found[pid] = cmdline
    return found


def _link_inside(link: Path, folder: Path) -> bool:
    try:
        target = os.readlink(link)
    except OSError:  # another user's, exited mid-scan, or a kernel thread
        return False
    return Path(target.removesuffix(" (deleted)")).is_relative_to(folder)


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


def started_by_notes(found: dict[int, str]) -> dict[int, str]:
    """pid -> "  (started by PID)" for each match that another match started, directly
    or further up its tree, naming the nearest; "" for the rest. The usual case is a job
    and the wrapper shell that launched it, `bash -c "... job ..."`, whose own command
    line matches too."""
    notes = {}
    for pid in found:
        starter = next((p for p in parents(pid) if p in found), None)
        notes[pid] = f"  (started by {starter})" if starter else ""
    return notes


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
        notes = started_by_notes(found)
        for pid, cmdline in sorted(found.items()):
            print(
                f"  {pid}  {shortened(cmdline, pattern, full)}{notes[pid]}",
                file=sys.stderr,
            )
        if full and any(notes.values()):  # a wrapper shell matches only by command line
            print(
                f"{prog}: a match started by another is usually a job and the shell that"
                " launched it; anchor the pattern with ^ to the command you mean"
                " (-f '^python3 job.py'), or start jobs with bgrun and use the PID it prints",
                file=sys.stderr,
            )
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
