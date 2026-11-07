"""How busy a set of cores is, from /proc/stat's per-core counters."""

from pathlib import Path


def snapshot(path="/proc/stat"):
    return Path(path).read_text()


def counters(text):
    """{core number: (busy ticks, all ticks)} from a /proc/stat snapshot. Idle and
    iowait count as idle; guest time is already inside user time."""
    found = {}
    for line in text.splitlines():
        name, *fields = line.split()
        if not name.startswith("cpu") or name == "cpu":
            continue
        ticks = [int(field) for field in fields[:8]]
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        found[int(name[3:])] = (sum(ticks) - idle, sum(ticks))
    return found


def busy(before, after, cores):
    """The share of the named cores' time spent busy between two snapshots; 0.0 when
    no time passed on them."""
    start, end = counters(before), counters(after)
    seen = [core for core in cores if core in start and core in end]
    spent = sum(end[core][0] - start[core][0] for core in seen)
    total = sum(end[core][1] - start[core][1] for core in seen)
    return spent / total if total > 0 else 0.0
