"""Fail a test that leaves processes running, after killing them.

conftest.py imports the two autouse fixtures here. Each opens a watch before the
fixtures of its scope are set up and closes it after they are torn down: one per test,
and one per test module for its module-scoped fixtures, which pytest sets up before any
test's own. Opening, a watch lists the pids in /proc and puts a tag in os.environ,
LEFTOVER_PROCESSES_TAG=<pytest's pid>-<n>, after the tag it replaces
(`<outer>/<pid>-<n>`, so a pytest run inside a test tags under the test). Processes
started meanwhile inherit it unless given an environment built without it.

Closing, it judges each process that started since it opened and still runs, by its
anchor: the first process up its parents that was already running at the opening.

1. The anchor is pytest: the test started it. A leftover.
2. The anchor is one of pytest's ancestors or init, which adopt orphans: orphaned out
   of the test's tree when its parent exited, or started by pytest's ancestors. A
   leftover when its tag is this watch's, or chained under it; or, carrying no tag,
   when its working directory (even a deleted one) or its command line lies under
   pytest's basetemp, the worker's own under xdist. Init is named apart: in a
   container, a process started by `docker exec` has no parent, so init isn't among
   pytest's ancestors.
3. Any other anchor, such as another worker, another session's shell or a module
   fixture's server: never a leftover.

So a module fixture's process kept for later tests predates each test's watch, and
counts only if it outlives the module's fixtures. Processes of other users are skipped.

Each leftover still running after GRACE_SECONDS ($LEFTOVER_PROCESSES_GRACE, for the
tests of this module) is killed with SIGKILL, with the process group it leads, if it
leads one, as a process started in a session of its own does. One in a group it doesn't
lead, such as pytest's, is killed alone. The fixture then fails, naming each: pid, age
and command line. pytest reports that as an error at the teardown of the test, or of
the module's last test.

Rejected: making pytest a child subreaper, so every orphan anchors at it. Orphans then
become pytest's zombies, which the tools under test see as alive, and reaping them
steals exit statuses from subprocess.Popen.

Standard library and pytest only, rather than lib/procmatch.py, since filedrawer's
bin/tests keeps a verbatim copy and has no llm-toolbox: change both.
"""

import itertools
import os
import re
import signal
import time
from pathlib import Path
from typing import NamedTuple

import pytest

TAG = "LEFTOVER_PROCESSES_TAG"
GRACE_SECONDS = 2.0
SHOWN_CHARS = 200
PROC = Path("/proc")
_watches = itertools.count()


def _pids() -> set[str]:
    return {name for name in os.listdir(PROC) if name.isdigit()}


def _read(*path: str) -> bytes | None:
    """The file's bytes, None when it can't be read. os.read rather than pathlib or
    open(), at a third of the cost: a watch reads a stat file per process started in it."""
    try:
        fd = os.open(os.path.join(PROC, *path), os.O_RDONLY)
    except OSError:
        return None
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


class _Stat(NamedTuple):
    """What a watch needs from /proc/PID/stat."""

    running: bool  # not a zombie, which has exited and only waits to be reaped
    parent: str
    started: int  # clock ticks after boot


def _stat(pid: str) -> _Stat | None:
    stat = _read(pid, "stat")
    if stat is None:
        return None
    # After the name, which is in parentheses and may hold spaces.
    fields = stat[stat.rindex(b")") + 2 :].split()
    return _Stat(fields[0] != b"Z", fields[1].decode(), int(fields[19]))


def _running(pid: str) -> bool:
    stat = _stat(pid)
    return stat is not None and stat.running


def _ancestors(pid: str) -> list[str]:
    found = []
    while (stat := _stat(pid)) and stat.parent != "0" and stat.parent not in found:
        pid = stat.parent
        found.append(pid)
    return found


def _age(pid: str) -> float:
    stat, uptime = _stat(pid), _read("uptime")
    if stat is None or uptime is None:
        return 0.0
    started = stat.started / os.sysconf("SC_CLK_TCK")
    return max(0.0, float(uptime.split()[0]) - started)


def _command_line(pid: str) -> str:
    raw = _read(pid, "cmdline") or b""
    line = raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")
    line = re.sub(r"[\r\n\t]+", " ", line)
    if len(line) > SHOWN_CHARS:
        line = f"{line[:SHOWN_CHARS]}…[{len(line)} chars]"
    return line


def _tag(pid: str) -> bytes:
    """The process's tag, empty when it has none or its environment can't be read."""
    prefix = TAG.encode() + b"="
    for entry in (_read(pid, "environ") or b"").split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return b""


class Watch:
    """The processes started from its opening, judged at its closing (module docstring),
    in a pytest whose basetemp is `basetemp`."""

    def __init__(self, basetemp: Path):
        self.previous = os.environ.get(TAG)
        own = f"{os.getpid()}-{next(_watches)}"
        self.tag = f"{self.previous}/{own}" if self.previous else own
        self.basetemp = str(basetemp)
        # Not followed by a character that would continue its last name.
        self.basetemp_pattern = re.compile(
            re.escape(os.fsencode(self.basetemp)) + rb"(?![\w.-])"
        )
        self.before = _pids()
        os.environ[TAG] = self.tag

    def close(self) -> list[str]:
        """Restores the tag it replaced, kills what was left over, and returns a line
        naming each: pid, age and command line."""
        if self.previous is None:
            os.environ.pop(TAG, None)
        else:
            os.environ[TAG] = self.previous
        stats = {pid: _stat(pid) for pid in _pids() - self.before}
        parents = {pid: stat and stat.parent for pid, stat in stats.items()}
        running = [pid for pid, stat in stats.items() if stat and stat.running]
        if not running:
            return []
        me = str(os.getpid())
        adopters = {*_ancestors(me), "1"}
        left = []
        for pid in running:
            anchor = _anchor(pid, parents)
            if anchor == me or (anchor in adopters and self._orphan_of_test(pid)):
                left.append(pid)
        grace = float(os.environ.get("LEFTOVER_PROCESSES_GRACE", GRACE_SECONDS))
        left = _still_running([pid for pid in left if _mine(pid)], grace)
        lines = [f"  {pid}  {_age(pid):.1f} s  {_command_line(pid)}" for pid in left]
        _kill(left)
        return lines

    def _orphan_of_test(self, pid: str) -> bool:
        tag = _tag(pid)
        if tag:
            return tag == self.tag.encode() or tag.startswith(f"{self.tag}/".encode())
        try:
            # A deleted folder reads as "<path> (deleted)", which still matches.
            cwd = os.fsencode(os.readlink(PROC / pid / "cwd"))
        except OSError:
            cwd = b""
        return bool(
            self.basetemp_pattern.match(cwd)
            or self.basetemp_pattern.search(_read(pid, "cmdline") or b"")
        )


def _anchor(pid: str, parents: dict[str, str | None]) -> str | None:
    """The first of pid's parents that isn't a key of `parents`, the processes started
    in the watch; None when one of those has gone."""
    seen = set()
    while pid is not None and pid in parents and pid not in seen:
        seen.add(pid)
        pid = parents[pid]
    return pid


def _mine(pid: str) -> bool:
    try:
        return (PROC / pid).stat().st_uid == os.getuid() and _running(pid)
    except OSError:
        return False


def _still_running(pids: list[str], seconds: float) -> list[str]:
    """Those of `pids` still running after up to `seconds`, polled: a process the test
    stopped without waiting for may take a moment to exit, longer under load."""
    deadline = time.monotonic() + seconds
    while (pids := [pid for pid in pids if _running(pid)]) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.02)
    return pids


def _kill(pids: list[str]) -> None:
    """SIGKILL to each pid, with the process group it leads; then waits a while for
    them to go. A group a leftover leads holds only what the test started, never
    pytest's or its ancestors' group."""
    for pid in pids:
        try:
            if os.getpgid(int(pid)) == int(pid):
                os.killpg(int(pid), signal.SIGKILL)
            else:
                os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _still_running(pids, 5)


def _guard(basetemp: Path, owner: str):
    watch = Watch(basetemp)
    yield
    lines = watch.close()
    if lines:
        pytest.fail(
            f"{owner} left {len(lines)} process(es) running, now killed with any"
            " process group each leads (pid, age, command line):\n" + "\n".join(lines),
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _leftover_processes_of_test(tmp_path_factory):
    """Named to sort first among conftest.py's names, which pytest sets up in that
    order, so it opens before every other fixture of the test and closes after them."""
    yield from _guard(tmp_path_factory.getbasetemp(), "the test")


@pytest.fixture(scope="module", autouse=True)
def _leftover_processes_of_module(request, tmp_path_factory):
    yield from _guard(
        tmp_path_factory.getbasetemp(), f"{request.node.nodeid}'s module fixtures"
    )
