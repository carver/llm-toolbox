"""heavy's queue, shared by bin/heavy and agent-commit-gate: two lanes on this VM, each
running one CPU-heavy job at a time at full priority (its holder), first come first
served, once the cores the job would use are quiet; and beside it, when there's room,
one more at nice 10 (its sharer).

A long job (`--long`) gets the last quarter of the cores; while one runs or waits, a
normal job gets the rest, and otherwise all of them. With fewer than 4 cores there is
no quarter to give, and a long job takes the normal lane. A lane's head starts as its
holder once its cores have stayed under half busy at every sample for
HEAVY_QUIET_SECONDS, or at the first sample when it arrived to a free lane with nobody
waiting.

A waiter that asks to (`--share`) starts beside the holder, as its lane's sharer,
when the share slot is free, the holder doesn't run `--alone`, 4 GiB of memory are
free, and either both jobs' history says the waiter's usual cores fit in 75% of the
holder's cores beside the holder's load, or the holder's cores have averaged under 30%
busy for HEAVY_SHARE_SECONDS. The first waiter in line that may share does: a waiter
that may steps aside for each one ahead of it that asked to share and fits too, by its
own samples and their history, for SHARE_PATIENCE seconds times its place among them,
so the order holds however late each one's polls come. A sharer runs on the holder's
cores, at nice 10 and first in line for the OOM killer, with CARGO_BUILD_JOBS capped
at the cores the holder leaves.

Each lane is a lib/fifo.py queue in HEAVY_DIR/<lane>/, its holder's slot the file
`slot` there and its sharer's `share`. A job's cores are decided, and its slot taken,
under one lock for both lanes, so a long and a normal job starting together never
share cores.

It reads HEAVY_DIR, HEAVY_QUIET_SECONDS, HEAVY_POLL (seconds per sample),
HEAVY_SHARE_SECONDS, HEAVY_STALL_SECONDS (before the PR hint), HEAVY_BUSY (the tests'
stand-in for measuring), HEAVY_MEMINFO and HEAVY_VMSTAT (the tests' stand-ins for
/proc/meminfo and /proc/vmstat), as bin/heavy's -h describes them; HEAVY_SLOT, which a
job's environment carries so a heavy inside it runs at once; and RUSTC_WRAPPER, to
start sccache's server before pinning.
"""

import math
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import cores
import fifo
import heavyhistory
from fifo import Holder

LANES = ("normal", "long")
SLOTS = ("slot", "share")  # a lane's holder, and its sharer
QUIET_SHARE = 0.5
SHARE_NICE = 10
SHARE_FIT = 0.75  # of the holder's cores: its load and a sharer's usual cores, at most
SHARE_IDLE = 0.3  # how busy, at most, a holder's cores average for anyone to share
SHARE_MEMORY = 4 * 2**30  # bytes of MemAvailable a sharer needs
# More, for a waiter that last found too little: memory that hovers at the floor
# doesn't flip its reason, and its waiting line, at every poll.
SHARE_MEMORY_AGAIN = SHARE_MEMORY + 2**29
# Seconds, and polls, at least, that a waiter steps aside for the first sharing waiter
# ahead of it that fits too (twice that for the second, and so on): long enough for a
# starved one to take its place, short enough that a stopped one holds up sharing only
# that long.
SHARE_PATIENCE = 5.0
SHARE_PATIENCE_POLLS = 5
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


def free_memory():
    """MemAvailable in bytes, from /proc/meminfo (or the HEAVY_MEMINFO file), or None
    when it can't be read."""
    source = os.environ.get("HEAVY_MEMINFO") or "/proc/meminfo"
    try:
        text = Path(source).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        words = rest.split()
        if name == "MemAvailable" and words and words[0].isdigit():
            return int(words[0]) * 1024
    return None


def oom_kills():
    """How many processes the out-of-memory killer has ended since the VM booted,
    from /proc/vmstat (or the HEAVY_VMSTAT file), or None when it can't be read."""
    source = os.environ.get("HEAVY_VMSTAT") or "/proc/vmstat"
    try:
        text = Path(source).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name, _, count = line.partition(" ")
        if name == "oom_kill" and count.strip().isdigit():
            return int(count)
    return None


def stamped(text):
    """A waiter's text with the time on its first line made now, for the slot it
    takes: its entry's says when it joined the queue, its slot's when it started."""
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    return Holder.parse(text)._replace(since=fifo.stamp()).line() + rest


def first_line(text):
    return text.split("\n", 1)[0]


def fields(text):
    """The `name=value` lines after an entry's or slot's first."""
    return dict(line.split("=", 1) for line in text.splitlines()[1:] if "=" in line)


def doing(text):
    """What an entry's or slot's job runs, as `cmd` in its folder."""
    about = fields(text)
    return f"`{about.get('cmd', '?')}` in {about.get('cwd', '?')}"


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
    hold everyone's compiles to that job's cores. It never idles out, or a sharer's
    cargo would start the next one, niced, for everyone."""
    wrapper = os.environ.get("RUSTC_WRAPPER", "")
    path = shutil.which(wrapper) if wrapper else None
    if path and Path(path).name == "sccache":
        # A server that's already running makes this fail, harmlessly.
        subprocess.run(
            [path, "--start-server"],
            capture_output=True,
            check=False,
            timeout=60,
            env=os.environ | {"SCCACHE_IDLE_TIMEOUT": "0"},
        )


def lower_priority(nice):
    """Run this process, and what it starts next, at nice (or nicer), first in line
    for the OOM killer."""
    with suppress(OSError):
        now = os.getpriority(os.PRIO_PROCESS, 0)
        os.setpriority(os.PRIO_PROCESS, 0, max(now, nice))
    with suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000\n")


@dataclass
class Job:
    """What a job runs with: its cores, an environment that lets a heavy inside it
    run at once and caps cargo's parallelism, and its nice. nested: it runs inside
    another job, which holds the slot. Its caller tells it how CMD exited.
    oom_kills_at_start: the VM's count of out-of-memory kills when it started."""

    cpus: list
    env: dict
    nice: int = 0
    nested: bool = False
    status: int | None = None
    oom_kills_at_start: int | None = field(default_factory=oom_kills)

    def pin(self):
        """Pin this process to the job's cores, and nice it for a sharer; what it
        starts next inherits both."""
        if not self.nested and os.getpriority(os.PRIO_PROCESS, 0) <= 0:
            start_sccache()
        try:
            os.sched_setaffinity(0, self.cpus)
        except OSError as error:
            note(
                f"heavy: can't pin to cores {span(self.cpus)} ({error.strerror}),"
                " so it runs on any."
            )
        if self.nice:
            lower_priority(self.nice)

    def exited(self, status):
        """Say how CMD ended: a run that exits 0 goes into the command's history. One
        that fails while the VM's count of OOM kills rose hears that the killer may
        have taken one of its processes: the command on top seldom shows it (cargo
        exits 101 when a rustc is killed, pytest 1 when a worker is). A heavy inside
        a job leaves that line to the job's, which hears the same failure."""
        self.status = status
        if status == 0 or self.nested:
            return
        now = oom_kills()
        if None in (self.oom_kills_at_start, now) or now <= self.oom_kills_at_start:
            return
        sharing = (
            " A job sharing a lane, as this one did, is the first the killer takes."
            if self.nice
            else ""
        )
        note(
            "heavy: the out-of-memory killer ended a process while this job ran,"
            f" perhaps one of the job's.{sharing} If the failure looks odd, run it"
            " again."
        )


class Start(NamedTuple):
    """A job that took its place: the slot's path and text, its cores, its cargo jobs
    and its nice."""

    slot: Path
    text: str
    cpus: list
    jobs: int
    nice: int


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


def slot_text(lane, name="slot"):
    """The text of lane's slot (its holder's, or with name="share" its sharer's), or
    None when it's free."""
    folder = lane_folder(lane)
    return fifo.slot_text(folder / name, folder, stale=stale)


def lane_cores(lane, cpus):
    """The cores a job in lane would run on, if it started now: the long lane's
    quarter, or for the normal lane every core, less the quarter while a long job
    runs or waits."""
    parts = split(cpus)
    if lane == "long":
        return parts[1]
    if parts is None:
        return cpus
    long_busy = any(slot_text("long", name) for name in SLOTS)
    if long_busy or fifo.ahead(lane_folder("long"), None):
        return parts[0]
    return cpus


def inside_job():
    """Whether this process runs inside a job holding a slot (HEAVY_SLOT names it)."""
    token = os.environ.get("HEAVY_SLOT")
    return bool(token) and any(
        first_line(slot_text(lane, name) or "") == token
        for lane in LANES
        for name in SLOTS
    )


@contextmanager
def turn(long=False, command="", hint=True, alone=False, share=False, front=False):
    """Wait for this job's turn in its lane and for its cores to go quiet, or, with
    share, for room beside its lane's holder; then hold the slot while the body runs,
    yielding the Job to run with. Inside a job that holds a slot, yield at once.
    hint=False leaves out the PR hint, for a commit check, which has nothing to push
    yet. alone: no job shares the lane beside this one. front: join the lane's queue
    ahead of every waiter that didn't ask to (fifo.join's front), for wt-ship's checks
    while it holds the main tree; the running job isn't stopped. A body that calls
    job.exited(0) has its run remembered. Raises QueueError when the queue's folder
    can't be used."""
    if inside_job():
        if alone:
            note("heavy: --alone does nothing inside a job, which runs at once.")
        yield Job(sorted(os.sched_getaffinity(0)), dict(os.environ), nested=True)
        return
    cpus = sorted(os.sched_getaffinity(0))
    lane = "long" if long and split(cpus) else "normal"
    folder = lane_folder(lane)
    me = Holder.mine()
    known_as = heavyhistory.key(os.getcwd(), command)
    # One line, or a command's own lines would read as the entry's fields.
    one_line = command.replace("\n", "\\n")
    about = f"cwd={os.getcwd()}\ncmd={one_line}\nkey={known_as}\n"
    if alone:
        about += "alone=1\n"
    if share:
        about += "share=1\n"
    try:
        entry = fifo.join(folder, me, about, front=front)
    except OSError as error:
        raise QueueError(f"{folder}: {error.strerror}") from None
    started = None
    try:
        started = Waiter(
            lane, folder, me.line() + about, entry, hint, cpus, alone, share
        ).wait()
        env = dict(os.environ) | {
            "HEAVY_SLOT": first_line(started.text),
            "CARGO_BUILD_JOBS": str(started.jobs),
        }
        job = Job(started.cpus, env, nice=started.nice)
        run = heavyhistory.Run()
        yield job
        if job.status == 0:
            heavyhistory.record(base(), known_as, *run.cores())
    finally:
        entry.unlink(missing_ok=True)
        if started is not None:
            fifo.release(started.slot, started.text)


class Snapshot(NamedTuple):
    """A lane as a waiter saw it at one poll: its holder's and sharer's slot texts
    (None when free), and the waiters ahead, as fifo.ahead gives them."""

    held: str | None
    shared: str | None
    ahead: list


class Verdict(NamedTuple):
    """Whether a waiter may share: the holder's load, in cores, that let it in (None
    when it may not), and a key and a line saying why not."""

    load: float | None
    key: object = None
    line: str = ""


class Watch:
    """How busy the holder's cores were at each poll, since the holder or its sharer
    last changed."""

    def __init__(self, keep):
        self.keep, self.subject, self.samples = keep, None, []

    def add(self, subject, share):
        if subject != self.subject:
            self.subject, self.samples = subject, []
        self.samples = [*self.samples, share][-self.keep :]

    def mean(self, seconds):
        """How busy on average over the last seconds, or None before a full window: a
        sample a poll, so a clock that jumps can't fill one."""
        count = max(1, round(seconds / knob("HEAVY_POLL", 1)))
        if len(self.samples) < count:
            return None
        return sum(self.samples[-count:]) / count


class Bid:
    """A waiter's bid to share its lane: it watches the holder's cores at each poll,
    judges whether it fits beside the holder, and takes the share slot unless a waiter
    ahead of it fits too."""

    def __init__(self, lane, folder, text, entry, cpus, say):
        self.lane, self.folder, self.text, self.entry = lane, folder, text, entry
        self.cpus, self.say = cpus, say
        self.key = fields(text).get("key")
        poll = knob("HEAVY_POLL", 1)
        windows = (knob("HEAVY_QUIET_SECONDS", 10), knob("HEAVY_SHARE_SECONDS", 90))
        self.watch = Watch(max(1, *(round(w / poll) for w in windows)))
        self.short_of_memory = False  # whether it last found too little free memory
        # Sharing waiters ahead that fit too: since when, by entry name, for this
        # watch's holder and sharer.
        self.fit_since, self.fit_subject = {}, None

    def poll(self, seen):
        """Watch the holder in seen for a poll and, when this waiter may share its
        lane and is the first in line that may, start beside it: the Start, or
        None."""
        holder_cpus = unspan(fields(seen.held).get("cores", ""))
        holder_cpus = holder_cpus or lane_cores(self.lane, self.cpus)
        subject = (first_line(seen.held), first_line(seen.shared or ""))
        self.watch.add(subject, busy_now(holder_cpus))
        if subject != self.fit_subject:
            self.fit_since, self.fit_subject = {}, subject
        history = heavyhistory.History(base())
        verdict = self.refusal(seen) or self.fit(seen, holder_cpus, self.key, history)
        if verdict.load is None:
            # The waiters ahead keep their clocks: one whose own fit comes and goes
            # still passes a stopped waiter ahead in time.
            self.say("share", verdict.key, verdict.line)
            return None
        # Read the line again: it may have changed while this waiter sampled.
        ahead = fifo.ahead(self.folder, self.entry)
        if not self.first_in_line(seen, holder_cpus, ahead, history):
            return None
        return self.take(seen.held, holder_cpus, verdict.load)

    def refusal(self, seen):
        """The Verdict when nothing may share the lane now, whoever asks: an `--alone`
        holder, a sharer already there, too little free memory; or None."""
        if fields(seen.held).get("alone"):
            line = "heavy: the lane's holder runs --alone, so nothing shares it."
            return Verdict(None, "alone", line)
        if seen.shared is not None:
            sharer = Holder.parse(seen.shared)
            line = (
                f"heavy: {sharer.session}'s {doing(seen.shared)} already shares"
                f" the {self.lane} lane."
            )
            return Verdict(None, ("taken", first_line(seen.shared)), line)
        memory = free_memory()
        floor = SHARE_MEMORY_AGAIN if self.short_of_memory else SHARE_MEMORY
        self.short_of_memory = memory is None or memory < floor
        if self.short_of_memory:
            free = "unknown" if memory is None else f"{memory / 2**30:.1f} GiB"
            line = (
                f"heavy: too little free memory to share the lane ({free}; it"
                f" takes {SHARE_MEMORY // 2**30} GiB)."
            )
            return Verdict(None, "memory", line)
        return None

    def fit(self, seen, holder_cpus, key, history):
        """Whether the command known as key fits beside the holder, by both jobs'
        history and the watch's recent samples, or else by the holder's cores
        idling. The lines say why not for this waiter's own command."""
        count = len(holder_cpus)
        mine = history.usual(key)
        theirs = history.usual(fields(seen.held).get("key"))
        recent = self.watch.mean(knob("HEAVY_QUIET_SECONDS", 10))
        known = None not in (mine, theirs, recent)
        if known:
            load = max(theirs, recent * count)
            if load + mine <= SHARE_FIT * count:
                return Verdict(load)
        idle_for = knob("HEAVY_SHARE_SECONDS", 90)
        idle = self.watch.mean(idle_for)
        if idle is not None and idle < SHARE_IDLE:
            return Verdict(idle * count)
        later = (
            f"it can share the lane at nice {SHARE_NICE} once the holder's cores"
            f" average under {SHARE_IDLE:.0%} busy for {idle_for:g} s"
        )
        if mine is None or theirs is None:
            whose = "this command" if mine is None else "the holder's command"
            later = f"no passing run of {whose} yet, so {later}"
        if known:
            line = (
                f"heavy: its usual {mine:.1f} cores don't fit beside the holder's"
                f" {load:.1f} of {count}; {later}."
            )
            return Verdict(None, "fit", line)
        return Verdict(None, "watch", f"heavy: {later}.")

    def first_in_line(self, seen, holder_cpus, ahead, history):
        """Whether no waiter ahead of this one that asked to share fits beside the
        holder too, as this waiter's samples and their history see it, or each that
        does has fit, without taking the place, for SHARE_PATIENCE times its place
        among them: so the second of them can't pass the first while both wait out
        a stopped one. Whether one ahead fits doesn't depend on when its own polls
        come, so a starved one keeps its turn."""
        now = time.monotonic()
        patience = max(SHARE_PATIENCE, SHARE_PATIENCE_POLLS * knob("HEAVY_POLL", 1))
        sharing = [(path.name, fields(text)) for path, text in ahead]
        sharing = [(name, about) for name, about in sharing if about.get("share")]
        names = {name for name, _ in sharing}
        self.fit_since = {n: s for n, s in self.fit_since.items() if n in names}
        place, waiting = 0, False
        for name, about in sharing:  # every clock starts now, not only the first's
            if self.fit(seen, holder_cpus, about.get("key"), history).load is None:
                continue
            place += 1
            since = self.fit_since.setdefault(name, now)
            waiting = waiting or now - since < patience * place
        return not waiting

    def take(self, held, holder_cpus, load):
        """Take the share slot beside the holder whose slot text is held, under the
        lock both lanes share: the Start, or None when the holder changed, runs
        alone, or another waiter shares first."""
        with fifo.locked(base()):
            now = slot_text(self.lane)
            if now is None or first_line(now) != first_line(held):
                return None
            if fields(now).get("alone") or slot_text(self.lane, "share") is not None:
                return None
            allowed = lane_cores(self.lane, self.cpus)
            cpus = [cpu for cpu in holder_cpus if cpu in allowed] or allowed
            room = math.floor(SHARE_FIT * len(holder_cpus) - load)
            jobs = max(1, min(len(cpus), room))
            text = stamped(self.text) + f"cores={span(cpus)}\njobs={jobs}\n"
            if not fifo.take(self.folder / "share", text, self.folder):
                return None
        self.entry.unlink(missing_ok=True)
        holder = Holder.parse(held)
        note(
            f"heavy: sharing the {self.lane} lane beside {holder.session}'s"
            f" {doing(held)}, at nice {SHARE_NICE} on cores {span(cpus)},"
            f" {jobs} cargo jobs."
        )
        return Start(self.folder / "share", text, cpus, jobs, SHARE_NICE)


class Waiter:
    """One job's wait: to head its lane's queue with the slot free and its cores
    quiet, or for room beside the lane's holder."""

    def __init__(self, lane, folder, text, entry, hint, cpus, alone, share):
        self.lane, self.folder, self.text, self.entry = lane, folder, text, entry
        self.hint, self.cpus, self.alone = hint, cpus, alone
        self.said = {}
        self.bid = Bid(lane, folder, text, entry, cpus, self.say) if share else None

    def wait(self):
        """The Start of the job, once it has taken a slot."""
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
            seen = self.look()
            blocker = self.blocker(seen)
            if blocker is not None:
                self.say("lane", *blocker)
                waited, head_since, quiet_since = True, None, None
                if self.bid is None or seen.held is None:
                    time.sleep(knob("HEAVY_POLL", 1))
                    continue
                started = self.bid.poll(seen)
                if started is not None:
                    return started
                continue
            head_since = head_since or time.monotonic()
            measured = lane_cores(self.lane, self.cpus)
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
                "lane",
                "quiet",
                f"heavy: waiting for quiet cores: {span(measured)} are {share:.0%} busy,"
                f" and need to stay under {QUIET_SHARE:.0%} for {quiet_for:g} s.",
            )
            if not hinted and time.monotonic() - head_since >= stall_after:
                note(PR_HINT)
                hinted = True

    def look(self):
        return Snapshot(
            slot_text(self.lane),
            slot_text(self.lane, "share"),
            fifo.ahead(self.folder, self.entry),
        )

    def crowd(self):
        """The jobs already in this lane: its holder and the waiters ahead."""
        held = slot_text(self.lane) is not None
        return held + len(fifo.ahead(self.folder, self.entry))

    def blocker(self, seen):
        """(a key, a line) naming what this job waits on, or None when it heads its
        lane and its slot is free: the holder's, and for `--alone` the sharer's too."""
        held, ahead = seen.held, seen.ahead
        if held is None and not ahead:
            if not (self.alone and seen.shared is not None):
                return None
            sharer = Holder.parse(seen.shared)
            return (first_line(seen.shared), 0), (
                f"heavy: waiting for {sharer.session}'s {doing(seen.shared)}, which"
                f" shares the {self.lane} lane, to end: --alone shares no cores."
            )
        first = held if held is not None else ahead[0][1]
        holder = Holder.parse(first)
        if held is not None:
            line = (
                f"{holder.session} is running {doing(first)}"
                f" since {holder.since[11:16]}"
            )
            if ahead:
                line += f"; {len(ahead)} waiting ahead"
        else:
            line = f"{len(ahead)} ahead, the first {holder.session}'s {doing(first)}"
        key = (first_line(first), len(ahead))
        return key, f"heavy: waiting for the {self.lane} lane: {line}."

    def start(self):
        """Decide the job's cores and take the slot, under the lock both lanes share:
        the Start, or None when the slot went first, a long job's quarter is still in
        a normal job's hands, or (for `--alone`) the lane has a sharer."""
        with fifo.locked(base()):
            if self.alone and slot_text(self.lane, "share") is not None:
                return None
            if self.lane == "long":
                in_use, user = set(), None
                for name in SLOTS:
                    text = slot_text("normal", name)
                    if text is not None:
                        in_use |= set(unspan(fields(text).get("cores", "")))
                        user = user or text
                quarter = split(self.cpus)[1]
                if in_use & set(quarter):
                    self.say(
                        "lane",
                        ("cores", first_line(user)),
                        f"heavy: waiting for cores {span(quarter)}, which a normal job"
                        " started before this one is using.",
                    )
                    return None
            cpus = lane_cores(self.lane, self.cpus)
            text = stamped(self.text) + f"cores={span(cpus)}\n"
            if fifo.take(self.folder / "slot", text, self.folder):
                return Start(self.folder / "slot", text, cpus, len(cpus), 0)
        return None

    def say(self, about, key, line):
        """Print line once for each change in what the job waits on (about "lane"), or
        in why it doesn't share (about "share")."""
        if key != self.said.get(about):
            note(line)
            self.said[about] = key


def status():
    """Each lane's cores and how busy they are, its holder and sharer, and its waiters
    in order, with each one's usual cores when its history has them."""
    cpus = sorted(os.sched_getaffinity(0))
    parts = split(cpus)
    history = heavyhistory.History(base())

    def usual(text):
        found = history.usual(fields(text).get("key"))
        return "" if found is None else f", usually {found:.1f} cores"

    lines = []
    for lane in LANES:
        if lane == "long" and parts is None:
            lines.append(
                "long lane: too few cores to split; long jobs use the normal lane"
            )
            continue
        lane_cpus = lane_cores(lane, cpus)
        lines.append(
            f"{lane} lane, cores {span(lane_cpus)}: {busy_now(lane_cpus):.0%} busy"
        )
        held = slot_text(lane)
        if held is not None:
            holder, about = Holder.parse(held), fields(held)
            lines.append(
                f"  running for {ago(holder)}: {doing(held)}, {holder.session},"
                f" on cores {about.get('cores', '?')}{usual(held)}"
            )
        shared = slot_text(lane, "share")
        if shared is not None:
            sharer, about = Holder.parse(shared), fields(shared)
            lines.append(
                f"  sharing at nice {SHARE_NICE} for {ago(sharer)}: {doing(shared)},"
                f" {sharer.session}, on cores {about.get('cores', '?')},"
                f" {about.get('jobs', '?')} cargo jobs{usual(shared)}"
            )
        for place, (_, text) in enumerate(fifo.ahead(lane_folder(lane), None), 1):
            holder = Holder.parse(text)
            lines.append(
                f"  {place}. waiting for {ago(holder)}: {doing(text)},"
                f" {holder.session}{usual(text)}"
            )
    return "\n".join(lines)
