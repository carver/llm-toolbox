"""The machine's speed: how fast it runs a fixed CPU loop now, against how fast it
usually does, so a run slowed by the host is told from one that's slow on its own.

The host can throttle this VM's CPU, and the sandbox can't see it: cgroup cpu.stat
showed `nr_throttled 0`, /proc/cpuinfo a fixed 2496 MHz, and there is no cpufreq
(2026-11-26, the afternoon the host throttled it). The owner then said of a timing:
"The CPU was throttled really low, so I wouldn't trust that 262 s number right now."
So heavy times the probe, a fixed pure-Python loop, after every job, in a child it
leaves running once the job's slot is free (probe_after_exit), and inline at the
start and end of an `--alone` one, and bin/machine-speed probes from cron every 5
minutes, pinned to vCPU 0, to mail the owner when the machine stays slow; a job's end
is judged by the newest probe of the last RECENT_SECONDS whose factor is known
(latest), and the speed factor is the probe's usual process time over its process time
now: 1.0 is the usual speed, 0.4 is 2.5 times slower. Process time, since this guest's
kernel charges stolen time to the running
task (CONFIG_PARAVIRT_TIME_ACCOUNTING is unset), so both kinds of host throttling, a
lower clock and a descheduled vCPU, slow it, where wall time also counts in-guest
load. The wall time is logged only.

Load slows the probe too: this VM is power-bound, its 16 vCPUs an i7-1260P's 8 P-core
threads and 8 E-cores on one power budget, and the probe took about 16 ms at a load
average of 1.5 against 8 ms on a quiet machine (2026-11-26). So the probe is judged
against probes taken on the same vCPU under the same load. It runs pinned to one vCPU
of the job's cores, the least busy over the HEAVY_POLL seconds before it (the lowest
of a tie), so P-cores and E-cores each get their own usual time. The load is the whole
machine's busy vCPUs over those seconds, from /proc/stat (HEAVY_STAT in the tests),
less the host's steal, which is throttling rather than load, and less heavy's own CPU
time; heavy's own last sample of /proc/stat stands in for the wait when it covers
those seconds (lib/cores.py, recent), as the one before an `--alone` job's start does.
Or it is the load while the loop runs, the loop's own CPU left out, when that is
busier: an after-exit probe's HEAVY_POLL seconds come as the next job starts, before
it ramps up, and the loop then runs under its full load, which would otherwise go in
too low a bucket and read as a slow machine. Its bucket is under 1, 1 to 3, 3 to 6, or
6 or more busy vCPUs: past about 6 the power budget is spent. The guest can't see a
busy hyperthread sibling, so probes within a bucket stay somewhat noisy.

The usual time is the fast end of the recent probes on this machine, this vCPU and
this bucket: the BASELINE_SHARE nearest-rank percentile of the process times of the
last BASELINE_DAYS days, from at least BASELINE_PROBES of them; with fewer, or with no
load known, the factor is unknown (None). A machine is its hostname, core count and
memory (MemTotal, to the nearest GiB), in the file's name, so a sandbox recreated on
other hardware starts its own. `heavy speed` prints the usual times (table).

The file is `perf-promises/speed-<machine>.tsv` in the git common dir of the
llm-toolbox checkout holding this module, beside perf-promises' logs, or
`$PERF_PROMISES_DIR/speed-<machine>.tsv` when that is set (the tests set it). A line a
probe, appended in one write: its time (ISO, UTC), wall seconds, process seconds, the
steal share of the job's cores over the job (`-` when not measured), the job's cores,
the vCPU the probe ran on, the load (`-` when unknown), `probe`, or `fake` for a
line HEAVY_SPEED_PROBE made, and the factor (`-` when unknown; a line from before it
has none). Past TRIM_AT lines it is cut to its newest KEEP.
HEAVY_SPEED_PROBE names a file holding `SECONDS [LOAD]`, which stand in for the
probe's seconds, wall and process both, and the load before it (default 0), on the
job's first core, with no wait and no pinning; it is read only while PERF_PROMISES_DIR
is set, and only then do `fake` lines count toward the usual time, so a test's fakes
can't move a real baseline. HEAVY_SPEED_AFTER_EXIT=0 starts no child, read only
while PERF_PROMISES_DIR is set too.

Nothing here raises: a probe or a file that fails is a Reading with a problem, for
the job's one note."""

import fcntl
import functools
import json
import math
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import cores
import perfpromises

LOOPS = 300_000
TRIES = 3  # timed runs, after one to warm up; the median counts
BASELINE_DAYS = 3
BASELINE_PROBES = 20
BASELINE_SHARE = 0.1
# The load buckets' lower edges, in busy vCPUs: under 1, 1 to 3, 3 to 6, 6 or more.
EDGES = (1, 3, 6)
BUCKETS = ("under 1", "1-3", "3-6", "6+")
TRIM_AT, KEEP = 4000, 2000
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
RECENT_SECONDS = 300  # how old a probe may be to judge a job's end by
CHILD_SECONDS = 10  # past which the after-exit probe is killed by its own alarm
SPELL_FRESH = 20 * 60  # how old a slow spell's newest known probe may be to warn of it
SLOW_SHOWN = 1 / 3  # the newest known factor under which a slow spell warns: 3x slower


class Reading(NamedTuple):
    """One probe: the speed factor (None when unknown), its wall and process seconds
    (None when it failed), and what went wrong, if anything."""

    factor: float | None
    wall: float | None = None
    process: float | None = None
    problem: str | None = None
    age: float | None = None  # seconds since a logged probe was taken; None: just now
    load: float | None = None  # busy vCPUs the probe was judged under, when known


def latest(now=None, within=RECENT_SECONDS):
    """The newest probe of the last `within` seconds that counts (no fakes, unless
    HEAVY_SPEED_PROBE is read) and whose factor is known, as logged: a Reading of
    its factor and its age; with none, a Reading of an unknown factor."""
    try:
        path = speed_file()
        if path is None:
            return Reading(None)
        try:
            text = path.read_text(errors="replace")
        except FileNotFoundError:
            return Reading(None)
    except Exception as error:  # noqa: BLE001 - a probe must never fail a job
        return Reading(None, problem=why(error))
    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=within)
    for raw in reversed(text.splitlines()):
        parts = raw.split("\t")
        if len(parts) < 8 or parts[7] not in marks():
            continue
        try:
            when = datetime.fromisoformat(parts[0])
            wall, process = float(parts[1]), float(parts[2])
            factor = None if len(parts) < 9 or parts[8] == "-" else float(parts[8])
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        if when < since:
            break
        if factor is None:
            continue
        return Reading(
            factor, wall, process, age=max(0.0, (now - when).total_seconds())
        )
    return Reading(None)


def probe_lock(path):
    """The lock file beside the speed file path, which the after-exit probe holds
    while it runs."""
    return path.with_name(f".{path.name}.probe")


def after_exit_off():
    """HEAVY_SPEED_AFTER_EXIT=0, honoured only while PERF_PROMISES_DIR is set."""
    return bool(os.environ.get("PERF_PROMISES_DIR")) and (
        os.environ.get("HEAVY_SPEED_AFTER_EXIT") == "0"
    )


def probe_after_exit(cpus, steal=None):
    """Start the probe of a job that has freed its slot, on one of its cpus, in a
    child of its own session that heavy doesn't wait for, unless another such child
    holds the lock beside the speed file: then none. The child takes the lock from
    this process, ignores SIGHUP, ends within CHILD_SECONDS (its own alarm stops it
    past that), and writes only the speed file. A child that can't be started is no
    probe and no note: the job's note was told before its slot was freed."""
    if after_exit_off():
        return
    try:
        path = speed_file()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(probe_lock(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    ",".join(map(str, sorted(cpus))),
                    "-" if steal is None else f"{steal:.6f}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                start_new_session=True,
                pass_fds=(lock,),
                preexec_fn=child_signals,  # noqa: PLW1509 - heavy runs no threads
            )
        finally:
            os.close(lock)
    except Exception:  # noqa: BLE001, S110 - a probe must never fail a job
        pass


def child_signals():
    """In the after-exit probe's child, before it execs: ignore SIGHUP and set the
    alarm. Both survive the exec, so the alarm runs from before the interpreter
    starts, and an import or a file on a hung mount can't keep the child past
    CHILD_SECONDS."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.alarm(CHILD_SECONDS)


def after_exit(argv):
    """The after-exit probe's child: `machinespeed.py CPUS STEAL`, CPUS
    comma-separated and STEAL `-` when unknown, with SIGHUP ignored and its alarm
    set (child_signals). It holds the lock it inherited until it exits."""
    cpus = [int(cpu) for cpu in argv[0].split(",")]
    measure(cpus, None if argv[1] == "-" else float(argv[1]))


def loop(count):
    total = 0
    for number in range(count):
        total += number * number
    return total


def probe():
    """(wall, process) seconds of the loop: the median of TRIES runs after one to warm
    up."""
    loop(LOOPS)
    walls, processes = [], []
    for _ in range(TRIES):
        wall, process = time.perf_counter(), time.process_time()
        loop(LOOPS)
        walls.append(time.perf_counter() - wall)
        processes.append(time.process_time() - process)
    return statistics.median(walls), statistics.median(processes)


def faked():
    """The HEAVY_SPEED_PROBE file, honoured only while PERF_PROMISES_DIR is set."""
    if not os.environ.get("PERF_PROMISES_DIR"):
        return None
    return os.environ.get("HEAVY_SPEED_PROBE") or None


def bucket(load):
    """The load bucket's index in BUCKETS."""
    return sum(load >= edge for edge in EDGES)


def moment(cpus):
    """(vCPU, load) for a probe on cpus: the vCPU of them that was least busy over
    the HEAVY_POLL seconds before it (the lowest of a tie), and the busy vCPUs of the
    whole machine then, or None when no time passed on /proc/stat (or HEAVY_STAT).
    It reuses heavy's own sample of those seconds when it took one."""
    stat = os.environ.get("HEAVY_STAT") or "/proc/stat"
    poll = float(os.environ.get("HEAVY_POLL", "1"))
    window = cores.recent(stat, poll) or cores.sample(stat, poll)
    found = cores.load(
        window.before, window.after, window.own * os.sysconf("SC_CLK_TCK")
    )
    if found is None:
        return min(cpus), None
    load, shares = found
    return min(cpus, key=lambda cpu: (shares.get(cpu, 0.0), cpu)), load


def taken(cpus):
    """(vCPU, load, wall, process) of a probe on one of cpus, pinned there while it
    runs, the load the busier of the moment before it (moment) and the probe's own
    run, the probe's CPU left out; or the HEAVY_SPEED_PROBE file's `SECONDS [LOAD]`
    (no load: 0), on the first of cpus, unpinned, under PERF_PROMISES_DIR."""
    fake = faked()
    if fake:
        words = Path(fake).read_text().split()
        load = float(words[1]) if len(words) > 1 else 0.0
        return min(cpus), load, float(words[0]), float(words[0])
    vcpu, load = moment(cpus)
    stat = os.environ.get("HEAVY_STAT") or "/proc/stat"
    before = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {vcpu})
    try:
        start, own = cores.snapshot(stat), time.process_time()
        wall, process = probe()
        own = (time.process_time() - own) * os.sysconf("SC_CLK_TCK")
        during = cores.load(start, cores.snapshot(stat), own)
    finally:
        os.sched_setaffinity(0, before)
    if during is not None:
        load = during[0] if load is None else max(load, during[0])
    return vcpu, load, wall, process


@functools.cache
def machine():
    """This machine's name for its file: hostname, cores and memory."""
    host = re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname()) or "host"
    memory = "?"
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        words = rest.split()
        if name == "MemTotal" and words and words[0].isdigit():
            memory = f"{round(int(words[0]) / 2**20)}G"
    return f"{host}-{os.cpu_count()}cpu-{memory}"


@functools.cache
def home():
    """The perf-promises folder in llm-toolbox's git common dir, or None outside git."""
    common = perfpromises.common_dir(Path(__file__).resolve().parent.parent)
    return None if common is None else common / "perf-promises"


def speed_file():
    """The file, or None when there is nowhere to keep it (no git dir). The machine's
    name and the git dir are found once a process (machine and home are cached), since
    a job's end asks before it frees its slot."""
    root = os.environ.get("PERF_PROMISES_DIR")
    name = f"speed-{machine()}.tsv"
    if root:
        return Path(root) / name
    folder = home()
    return None if folder is None else folder / name


def alert_file():
    """bin/machine-speed's state beside the speed file: its cron probes and whether
    they hold a slow spell open; None when there is nowhere to keep it."""
    path = speed_file()
    return None if path is None else path.with_name(path.stem + ".alert.json")


def slow_warning(now=None):
    """One loud line while bin/machine-speed's cron probes hold a slow spell open (3
    probes in a row under 0.25x, each judged under its own load, until 3 at 0.5x or
    above) and the newest probe with a known factor is at most SPELL_FRESH seconds
    old and under SLOW_SHOWN, so a spell's recovery tail stays quiet; else None: for
    `heavy status`, `heavy waits` and perf-promises' session start. Never raises."""
    try:
        path = alert_file()
        if path is None:
            return None
        state = json.loads(path.read_text())
        readings = [r for r in state.get("readings", []) if isinstance(r, dict)]
        if state.get("slow") is not True or not readings:
            return None
        known = [
            r
            for r in readings
            if isinstance(r.get("factor"), (int, float))
            and not isinstance(r["factor"], bool)
        ]
        if not known or not 0 < known[-1]["factor"] < SLOW_SHOWN:
            return None
        factor = known[-1]["factor"]
        now = now or datetime.now(UTC)
        if (
            now - datetime.fromisoformat(known[-1]["at"])
        ).total_seconds() > SPELL_FRESH:
            return None  # no recent reading: an old spell says nothing about now
        since = datetime.fromisoformat(state["since"]).astimezone()
    except Exception:  # noqa: BLE001 - a warning must never fail what prints it
        return None
    return (
        f"WARNING: host CPU running {1 / factor:.0f}x slower than usual"
        f" ({factor:.2f}x its usual speed, slow since {since:%H:%M %Z}): check"
        " the host for throttling (PROCHOT)."
    )


def probes(path, now):
    """{(vCPU, bucket): [process seconds]} of the probes of the last BASELINE_DAYS
    days that count toward the usual time: no fakes, unless HEAVY_SPEED_PROBE is read."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return {}
    since = now - timedelta(days=BASELINE_DAYS)
    found = {}
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) < 8 or parts[7] not in marks() or parts[6] == "-":
            continue
        try:
            when, process = datetime.fromisoformat(parts[0]), float(parts[2])
            key = int(parts[5]), bucket(float(parts[6]))
        except ValueError:
            continue
        if when.tzinfo is not None and when >= since and process > 0:
            found.setdefault(key, []).append(process)
    return found


def marks():
    """The marks of the lines that count: `probe`, and `fake` while
    HEAVY_SPEED_PROBE is read."""
    return {"probe", "fake"} if faked() else {"probe"}


def usual(times):
    """The usual process seconds among times, or None from too few."""
    if len(times) < BASELINE_PROBES:
        return None
    ordered = sorted(times)
    return ordered[max(0, math.ceil(BASELINE_SHARE * len(ordered)) - 1)]


def measure(cpus, steal=None, stoppable=False):
    """Probe on one of cpus, judge it against the usual time of that vCPU under the
    same load and log it: a Reading. steal is the job's steal share of its cores,
    when known. With stoppable, a stop (an error carrying `signum`, as heavy's
    signal handler raises) is raised rather than read, so a stop before a job starts
    stops heavy as it would without the probe."""
    try:
        vcpu, load, wall, process = taken(cpus)
    except Exception as error:  # a probe must never fail a job
        if stoppable and getattr(error, "signum", None) is not None:
            raise
        return Reading(None, problem=why(error))
    try:
        path = speed_file()
        if path is None:
            return Reading(None, wall, process)
        now = datetime.now(UTC)
        base = None
        if load is not None:
            base = usual(probes(path, now).get((vcpu, bucket(load)), []))
        factor = None if base is None or process <= 0 else base / process
        fields = [
            now.isoformat(timespec="seconds"),
            f"{wall:.6f}",
            f"{process:.6f}",
            "-" if steal is None else f"{steal:.3f}",
            cores.span(sorted(cpus)),
            str(vcpu),
            "-" if load is None else f"{load:.2f}",
            "fake" if faked() else "probe",
            "-" if factor is None else f"{factor:.2f}",
        ]
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
        try:
            perfpromises.append(path, "\t".join(fields), TRIM_AT, KEEP)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    except Exception as error:
        if stoppable and getattr(error, "signum", None) is not None:
            raise
        return Reading(None, wall, process, why(error), load=load)
    return Reading(factor, wall, process, load=load)


def table(now=None):
    """`heavy speed`: the usual time of each vCPU under each load, with its count."""
    path = speed_file()
    if path is None:
        return "heavy speed: no git dir to keep the probes in, so none are kept."
    found = probes(path, now or datetime.now(UTC))
    if not found:
        return f"heavy speed: no probes in the last {BASELINE_DAYS} days ({path})."

    def cell(times):
        base = usual(times)
        if base is None:
            return f"- ({len(times)})" if times else "-"
        return f"{base * 1000:.1f} ms ({len(times)})"

    rows = [["vCPU", *BUCKETS]]
    for vcpu in sorted({vcpu for vcpu, _ in found}):
        times = [found.get((vcpu, index), []) for index in range(len(BUCKETS))]
        rows.append([str(vcpu), *map(cell, times)])
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    header = (
        "heavy speed: the probe's usual process time on each vCPU under each load"
        " (busy vCPUs in the second before it), the fastest tenth of the last"
        f" {BASELINE_DAYS} days' probes, from at least {BASELINE_PROBES}; the probes"
        f" in brackets ({path})."
    )
    lines = [header]
    lines += [
        "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows
    ]
    return "\n".join(lines)


def why(error):
    stop = getattr(error, "signum", None)
    if stop is not None:
        return f"heavy: can't measure the machine's speed (stopped by signal {stop})."
    return (
        "heavy: can't measure the machine's speed"
        f" ({type(error).__name__}: {str(error)[:100]})."
    )


if __name__ == "__main__":
    after_exit(sys.argv[1:])
