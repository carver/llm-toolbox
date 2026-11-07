"""heavy's queue, shared by bin/heavy and agent-commit-gate: two lanes on this VM,
each running one CPU-heavy job at a time, first come first served, once the cores
the job would use are quiet.

A long job (`--long`) gets the last quarter of the cores; while one runs, a normal
job gets the rest, and otherwise all of them. With fewer than 4 cores there is no
quarter to give, and a long job takes the normal lane. A lane's head starts once
its cores have stayed under half busy at every sample for HEAVY_QUIET_SECONDS, or at
the first sample when it arrived to a free lane with nobody waiting. Each lane is a
lib/fifo.py queue in HEAVY_DIR/<lane>/, its slot the file `slot` there. A job's
cores are decided, and its slot taken, under one lock for both lanes, so a long and
a normal job starting together never share cores.
"""

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import cores
import fifo
from fifo import Holder

LANES = ("normal", "long")
QUIET_SHARE = 0.5
PR_HINT = (
    "heavy: the queue is busy. For build and test results without the wait, you can"
    " push a topic branch and open a PR, so GitHub's CI runs them (never main)."
)


class QueueError(Exception):
    """heavy couldn't use its queue's folder."""


def knob(name, default):
    return float(os.environ.get(name, default))


def base():
    return Path(os.environ.get("HEAVY_DIR", "/tmp/heavy"))


def note(line):
    print(line, file=sys.stderr, flush=True)


def span(cpus):
    """Cores as `0-11`, or a list when they aren't one run."""
    if len(cpus) > 1 and cpus == list(range(cpus[0], cpus[-1] + 1)):
        return f"{cpus[0]}-{cpus[-1]}"
    return ",".join(map(str, cpus))


def unspan(text):
    """The cores span() wrote."""
    found = []
    for part in text.split(","):
        low, _, high = part.partition("-")
        if low.isdigit():
            found.extend(range(int(low), int(high or low) + 1))
    return found


def split(cpus):
    """(the cores a normal job keeps while a long job runs, the long job's quarter),
    or None when there are too few cores to split."""
    if len(cpus) < 4:
        return None
    quarter = len(cpus) // 4
    return cpus[:-quarter], cpus[-quarter:]


def busy_now(cpus):
    """How busy cpus are over the next HEAVY_POLL seconds (or, in tests, the share
    the HEAVY_BUSY file names)."""
    poll = knob("HEAVY_POLL", 1)
    fake = os.environ.get("HEAVY_BUSY")
    if fake:
        time.sleep(poll)
        return float(Path(fake).read_text())
    before = cores.snapshot()
    time.sleep(poll)
    return cores.busy(before, cores.snapshot(), cpus)


def first_line(text):
    return text.split("\n", 1)[0]


def fields(text):
    """The `name=value` lines after an entry's or slot's first."""
    return dict(line.split("=", 1) for line in text.splitlines()[1:] if "=" in line)


def ago(holder):
    """How long since the holder joined or started, as `12m` or `1h05m`."""
    try:
        since = datetime.fromisoformat(holder.since)
    except ValueError:
        return "?"
    minutes = int((datetime.now().astimezone() - since).total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def start_sccache():
    """Start sccache's server, when cargo compiles through it, before this process is
    pinned: the server runs every rustc on the VM, so one started inside a job would
    hold everyone's compiles to that job's cores."""
    wrapper = os.environ.get("RUSTC_WRAPPER", "")
    path = shutil.which(wrapper) if wrapper else None
    if path and Path(path).name == "sccache":
        # A server that's already running makes this fail, harmlessly.
        subprocess.run(
            [path, "--start-server"], capture_output=True, check=False, timeout=60
        )


class Job(NamedTuple):
    """What a job runs with: its cores, and an environment that lets a heavy inside
    it run at once and caps cargo's parallelism at its cores."""

    cpus: list
    env: dict

    def pin(self):
        """Pin this process to the job's cores; what it starts next inherits them."""
        start_sccache()
        try:
            os.sched_setaffinity(0, self.cpus)
        except OSError as error:
            note(
                f"heavy: can't pin to cores {span(self.cpus)} ({error.strerror}),"
                " so it runs on any."
            )


def stale(holder):
    """Every heavy slot names its process, so one that doesn't is dead too."""
    return holder.pid is None or holder.gone()


def lane_folder(lane):
    folder = base() / lane
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise QueueError(f"{folder}: {error.strerror}") from None
    return folder


def slot_text(lane):
    """The text of lane's slot, or None when it's free."""
    folder = lane_folder(lane)
    return fifo.slot_text(folder / "slot", folder, stale=stale)


def inside_job():
    """Whether this process runs inside a job holding a slot (HEAVY_SLOT names it)."""
    token = os.environ.get("HEAVY_SLOT")
    return bool(token) and any(
        first_line(slot_text(lane) or "") == token for lane in LANES
    )


@contextmanager
def turn(long=False, command="", hint=True):
    """Wait for this job's turn in its lane and for its cores to go quiet, then hold
    the lane's slot while the body runs, yielding the Job to run with. Inside a job
    that holds a slot, yield at once. hint=False leaves out the PR hint, for a commit
    check, which has nothing to push yet. Raises QueueError when the queue's folder
    can't be used."""
    if inside_job():
        yield Job(sorted(os.sched_getaffinity(0)), dict(os.environ))
        return
    cpus = sorted(os.sched_getaffinity(0))
    lane = "long" if long and split(cpus) else "normal"
    folder = lane_folder(lane)
    me = Holder.mine()
    about = f"cwd={os.getcwd()}\ncmd={command}\n"
    try:
        entry = fifo.join(folder, me, about)
    except OSError as error:
        raise QueueError(f"{folder}: {error.strerror}") from None
    taken = None
    try:
        taken, job_cpus = Waiter(
            lane, folder, me.line() + about, entry, hint, cpus
        ).wait()
        env = dict(os.environ) | {
            "HEAVY_SLOT": first_line(taken),
            "CARGO_BUILD_JOBS": str(len(job_cpus)),
        }
        yield Job(job_cpus, env)
    finally:
        entry.unlink(missing_ok=True)
        if taken is not None:
            fifo.release(folder / "slot", taken)


class Waiter:
    """One job's wait: first in its lane's queue, the slot free, its cores quiet."""

    def __init__(self, lane, folder, text, entry, hint, cpus):
        self.lane, self.folder, self.text, self.entry = lane, folder, text, entry
        self.hint, self.cpus = hint, cpus
        self.said = None

    def wait(self):
        """(the slot's text, the job's cores), once it has taken the slot."""
        quiet_for = knob("HEAVY_QUIET_SECONDS", 10)
        stall_after = knob("HEAVY_STALL_SECONDS", 600)
        crowd = self.crowd()
        hinted = not self.hint or crowd >= 2
        if self.hint and crowd >= 2:
            note(PR_HINT)
        # A job that arrives to a free lane, nobody waiting, starts at the first quiet
        # sample; one that waited, or saw its cores busy, needs the whole window.
        waited = crowd > 0
        head_since = quiet_since = None
        while True:
            blocker = self.blocker()
            if blocker is not None:
                self.say(*blocker)
                waited, head_since, quiet_since = True, None, None
                time.sleep(knob("HEAVY_POLL", 1))
                continue
            head_since = head_since or time.monotonic()
            measured = self.lane_cores()
            share = busy_now(measured)
            if share < QUIET_SHARE:
                quiet_since = quiet_since or time.monotonic()
                if not waited or time.monotonic() - quiet_since >= quiet_for:
                    started = self.start()
                    if started is not None:
                        self.entry.unlink(missing_ok=True)
                        return started
                    waited, quiet_since = True, None  # taken first, or cores in use
                continue
            waited, quiet_since = True, None
            self.say(
                "quiet",
                f"heavy: waiting for quiet cores: {span(measured)} are {share:.0%} busy,"
                f" and need to stay under {QUIET_SHARE:.0%} for {quiet_for:g} s.",
            )
            if not hinted and time.monotonic() - head_since >= stall_after:
                note(PR_HINT)
                hinted = True

    def crowd(self):
        """The jobs already in this lane: its holder and the waiters ahead."""
        held = slot_text(self.lane) is not None
        return held + len(fifo.ahead(self.folder, self.entry))

    def blocker(self):
        """(a key, a line) naming what this job waits on, or None when it heads its
        lane and the slot is free."""
        held = slot_text(self.lane)
        ahead = fifo.ahead(self.folder, self.entry)
        if held is None and not ahead:
            return None
        first = held if held is not None else ahead[0][1]
        holder, about = Holder.parse(first), fields(first)
        doing = f"`{about.get('cmd', '?')}` in {about.get('cwd', '?')}"
        if held is not None:
            line = f"{holder.session} is running {doing} since {holder.since[11:16]}"
            if ahead:
                line += f"; {len(ahead)} waiting ahead"
        else:
            line = f"{len(ahead)} ahead, the first {holder.session}'s {doing}"
        key = (first_line(first), len(ahead))
        return key, f"heavy: waiting for the {self.lane} lane: {line}."

    def lane_cores(self):
        """The cores this job would run on, if it started now."""
        parts = split(self.cpus)
        if self.lane == "long":
            return parts[1]
        return parts[0] if parts and slot_text("long") is not None else self.cpus

    def start(self):
        """Decide the job's cores and take the slot, under the lock both lanes share:
        (the slot's text, the cores), or None when the slot went first or a long job's
        quarter is still in a normal job's hands."""
        with fifo.locked(base()):
            if self.lane == "long":
                normal = slot_text("normal")
                in_use = (
                    set(unspan(fields(normal).get("cores", ""))) if normal else set()
                )
                quarter = split(self.cpus)[1]
                if in_use & set(quarter):
                    self.say(
                        ("cores", first_line(normal)),
                        f"heavy: waiting for cores {span(quarter)}, which a normal job"
                        " started before this one is using.",
                    )
                    return None
            cpus = self.lane_cores()
            text = self.text + f"cores={span(cpus)}\n"
            if fifo.take(self.folder / "slot", text, self.folder):
                return text, cpus
        return None

    def say(self, key, line):
        """Print line once for each change in what the job waits on."""
        if key != self.said:
            note(line)
            self.said = key


def status():
    """Each lane's cores and how busy they are, its holder, and its waiters in order."""
    cpus = sorted(os.sched_getaffinity(0))
    parts = split(cpus)
    long_running = slot_text("long") is not None
    lines = []
    for lane in LANES:
        if lane == "long" and parts is None:
            lines.append(
                "long lane: too few cores to split; long jobs use the normal lane"
            )
            continue
        if lane == "long":
            lane_cpus = parts[1]
        else:
            lane_cpus = parts[0] if parts and long_running else cpus
        lines.append(
            f"{lane} lane, cores {span(lane_cpus)}: {busy_now(lane_cpus):.0%} busy"
        )
        held = slot_text(lane)
        if held is not None:
            holder, about = Holder.parse(held), fields(held)
            lines.append(
                f"  running for {ago(holder)}: `{about.get('cmd', '?')}` in"
                f" {about.get('cwd', '?')}, {holder.session},"
                f" on cores {about.get('cores', '?')}"
            )
        for place, (_, text) in enumerate(fifo.ahead(lane_folder(lane), None), 1):
            holder, about = Holder.parse(text), fields(text)
            lines.append(
                f"  {place}. waiting for {ago(holder)}: `{about.get('cmd', '?')}` in"
                f" {about.get('cwd', '?')}, {holder.session}"
            )
    return "\n".join(lines)
