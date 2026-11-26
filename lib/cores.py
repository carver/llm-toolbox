"""How busy a set of cores is, from /proc/stat's per-core counters. sample() keeps the
newest pair of snapshots it took, so a later measure of the same moment can reuse
them (recent) rather than wait another sample's length."""

import time
from pathlib import Path
from typing import NamedTuple


class Ticks(NamedTuple):
    """A core's counters: busy ticks, all ticks, and the steal ticks among the busy
    ones, the time the host ran something else on its vCPU."""

    busy: int
    total: int
    steal: int


def span(cpus):
    """Cores as `0-11`, or a list when they aren't one run."""
    if len(cpus) > 1 and cpus == list(range(cpus[0], cpus[-1] + 1)):
        return f"{cpus[0]}-{cpus[-1]}"
    return ",".join(map(str, cpus))


def snapshot(path="/proc/stat"):
    return Path(path).read_text()


class Sample(NamedTuple):
    """Two snapshots of path, seconds apart, taken by this process, which used own
    CPU-seconds meanwhile; ended is when, on the monotonic clock."""

    path: str
    before: str
    after: str
    seconds: float
    own: float
    ended: float


newest = None  # the Sample this process took last


def sample(path, seconds):
    """Snapshot path, sleep seconds and snapshot it again: a Sample, kept as newest."""
    global newest
    began, own = time.monotonic(), time.process_time()
    before = snapshot(path)
    time.sleep(seconds)
    after = snapshot(path)
    ended = time.monotonic()
    newest = Sample(
        path, before, after, ended - began, time.process_time() - own, ended
    )
    return newest


def recent(path, seconds):
    """The newest Sample of path when it lasted at least half of seconds and ended
    less than half of them ago, so it covers about the last seconds; else None."""
    found = newest
    if found is None or found.path != path or found.seconds < seconds / 2:
        return None
    return found if time.monotonic() - found.ended < seconds / 2 else None


def counters(text):
    """{core number: Ticks} from a /proc/stat snapshot. Idle and iowait count as
    idle, steal as busy; guest time is already inside user time."""
    found = {}
    for line in text.splitlines():
        name, *fields = line.split()
        if not name.startswith("cpu") or name == "cpu":
            continue
        ticks = [int(field) for field in fields[:8]]
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        steal = ticks[7] if len(ticks) > 7 else 0
        found[int(name[3:])] = Ticks(sum(ticks) - idle, sum(ticks), steal)
    return found


def load(before, after, own_ticks=0):
    """(busy vCPU-equivalents, {core: busy share}) between two snapshots: every core's
    busy time, less the host's steal and own_ticks (the measuring process's own), over
    the time that passed on one core; and each core's busy share, steal left out.
    None when no time passed."""
    start, end = counters(before), counters(after)
    seen = [core for core in end if core in start]
    spent, shares = {}, {}
    for core in seen:
        total = end[core].total - start[core].total
        working = end[core].busy - end[core].steal
        spent[core] = working - (start[core].busy - start[core].steal), total
        shares[core] = spent[core][0] / total if total > 0 else 0.0
    passed = sum(total for _, total in spent.values())
    if passed <= 0:
        return None
    working = sum(ticks for ticks, _ in spent.values()) - own_ticks
    return max(0.0, working) / (passed / len(seen)), shares


def busy(before, after, cores):
    """The share of the named cores' time spent busy between two snapshots; 0.0 when
    no time passed on them."""
    start, end = counters(before), counters(after)
    seen = [core for core in cores if core in start and core in end]
    spent = sum(end[core][0] - start[core][0] for core in seen)
    total = sum(end[core][1] - start[core][1] for core in seen)
    return spent / total if total > 0 else 0.0
