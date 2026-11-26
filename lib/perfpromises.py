"""Performance promises: a repo's perf-promises.toml says how long a command run
through heavy takes, and every run of it is measured against that.

The file at a repo's top holds `[[promise]]` tables, each with `name` ([a-z0-9-],
unique in the file), `command` (a Python regex, searched in the job's command words
as heavy records them, the first word cut to its last path part), `seconds` (the
promised running time of the whole run), `measured` (how the number was got),
`measured_seconds` (what that measurement gave) and `source` (where the promise was
made), and optionally `counts`, the exit statuses whose runs count toward the median
(default [0]). llm-toolbox's own file, the one beside this module's folder, covers jobs
in every repo, since its tools run everywhere; in llm-toolbox itself the tree's own
copy is read instead, so a worktree's edited promise works there.

When a heavy job ends (heavyqueue's `recorded`), after_job matches it against the job
repo's file and llm-toolbox's, appends the run to each matching promise's log, and
returns a one-line note when the run took twice its promise or more. A run counts
toward the promise when its status is in `counts` or a signal stopped it (at least its
elapsed time), it didn't run beside a holder (--share), and other work used at most
LOAD_SHARE of its cores meanwhile; the rest are logged and left out. Its time is the
monotonic time from the job's start to its end, never its wait in the queue, and it
counts at that time times the machine's speed factor as it ended (the newest probe
of the last few minutes whose factor is known, lib/machinespeed.py), at most 1
(unknown counts as 1): a run that took BROKEN_RATIO times its promise only because
the machine was slow is logged and left out too.

A promise is judged on its counted runs of the last COUNT_DAYS days whose log line
carries its current `seconds` and its `command`'s hash, so a worktree's edited
promise doesn't skew main's: below JUDGED_RUNS runs it isn't judged; it is broken
when the median of its newest JUDGED_RUNS is BROKEN_RATIO times its seconds or more.
A promise whose seconds are above DODGE_RATIO times its measured_seconds, that
matched jobs once but none in UNMATCHED_DAYS days, or most of whose last LEFT_OUT_OF
runs (of those whose exit counts, or of all of them when it has fewer) were left
out, is flagged too. One that never matched a job isn't, since a tripwire for a
dropped command is meant never to match; status says so.

The log is `<git common dir>/perf-promises/<name>.tsv` of the repo whose file holds
the promise, shared by its worktrees, or `$PERF_PROMISES_DIR/<repo>/<name>.tsv` when
that is set (the tests set it). Each run is one tab-separated line, appended in one
write: its end (ISO, UTC), seconds, exit (a number, or `sigN`), load (`-`, `share`
or `load=0.62`), the promised seconds, the command's hash, session and folder, and the
speed factor (`0.40`, or `-`; a line from before it has none). Past
TRIM_AT lines it is cut to its newest KEEP lines, under a lock; a line appended while
it's cut can be lost. A line that can't be read is skipped. PERF_PROMISES_GLOBAL names
another file to stand in for llm-toolbox's (tests).

Nothing here may fail a job: any failure is the one note a job gets."""

import fcntl
import hashlib
import math
import os
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import tomllib

FILE = "perf-promises.toml"
KEYS = ("name", "command", "seconds", "measured", "measured_seconds", "source")
NAME = re.compile(r"[a-z0-9-]+")
LOAD_SHARE = 0.5  # of a job's cores, at most, other work may use for its run to count
COUNT_DAYS = 14
JUDGED_RUNS = 5
BROKEN_RATIO = 2.0
DODGE_RATIO = 3.0
UNMATCHED_DAYS = 7
LEFT_OUT_OF = 10  # flagged when most of its last this many runs were left out
TRIM_AT, KEEP = 400, 200
GIT_TIMEOUT = 5
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass
class Promise:
    name: str
    command: str
    seconds: float
    measured: str
    measured_seconds: float
    source: str
    counts: tuple = (0,)

    @property
    def digest(self):
        return hashlib.sha256(self.command.encode()).hexdigest()[:8]

    def matches(self, words):
        return re.search(self.command, words) is not None


class Repo(NamedTuple):
    name: str
    common: Path | None  # its git common dir, None outside git


@dataclass
class PromiseFile:
    """One perf-promises.toml: its promises, the problems reading it, and its repo
    (found when first asked, since that runs git)."""

    path: Path
    promises: list
    problems: list
    _repo: Repo | None = field(default=None, repr=False)

    def repo(self):
        if self._repo is None:
            common = common_dir(self.path.parent)
            name = (common.parent if common else self.path.parent).name
            self._repo = Repo(name, common)
        return self._repo

    def log(self, promise):
        """The promise's log file, or None when there is nowhere to keep it."""
        name, common = self.repo()
        root = os.environ.get("PERF_PROMISES_DIR")
        if root:
            return Path(root) / name / f"{promise.name}.tsv"
        return (
            None if common is None else common / "perf-promises" / f"{promise.name}.tsv"
        )


def common_dir(folder):
    """The git common dir of folder's repo, absolute, or None outside git."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = found.stdout.strip()
    return Path(out) if found.returncode == 0 and out else None


def load(path):
    """The PromiseFile in path. A file that can't be read or parsed has no promises and one
    problem; an entry that's wrong is left out with a problem naming it."""
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return PromiseFile(Path(path), [], [f"{path}: can't read it ({error})"])
    promises, problems, names = [], [], set()
    entries = data.get("promise", [])
    if not isinstance(entries, list):
        return PromiseFile(
            Path(path), [], [f"{path}: `promise` must be an array of tables"]
        )
    for at, entry in enumerate(entries, 1):
        label = entry.get("name") if isinstance(entry, dict) else None
        label = f"promise {label!r}" if isinstance(label, str) else f"promise #{at}"
        why = wrong(entry, names)
        if why:
            problems.append(f"{path}: {label} {why}, so it's left out")
            continue
        names.add(entry["name"])
        promises.append(
            Promise(
                **{key: entry[key] for key in KEYS},
                counts=tuple(entry.get("counts", (0,))),
            )
        )
    return PromiseFile(Path(path), promises, problems)


def wrong(entry, names):
    """What's wrong with a promise's table, or None."""
    if not isinstance(entry, dict):
        return "isn't a table"
    missing = [key for key in KEYS if key not in entry]
    if missing:
        return "lacks " + ", ".join(missing)
    extra = sorted(set(entry) - set(KEYS) - {"counts"})
    if extra:
        return "has unknown keys " + ", ".join(extra)
    if not isinstance(entry["name"], str) or not NAME.fullmatch(entry["name"]):
        return "has a name that isn't [a-z0-9-]"
    if entry["name"] in names:
        return "repeats a name"
    for key in ("command", "measured", "source"):
        if not isinstance(entry[key], str) or not entry[key]:
            return f"has a {key} that isn't text"
    try:
        re.compile(entry["command"])
    except re.error as error:
        return f"has a command that isn't a regex ({error})"
    for key in ("seconds", "measured_seconds"):
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return f"has {key} that isn't a positive number"
    counts = entry.get("counts", [0])
    if not isinstance(counts, list) or not all(
        isinstance(code, int) and not isinstance(code, bool) for code in counts
    ):
        return "has counts that aren't a list of exit statuses"
    return None


def top_of(folder):
    """The top of folder's git tree, the nearest folder holding .git, or None."""
    for place in (folder, *folder.parents):
        if (place / ".git").exists():
            return place
    return None


def global_file():
    """llm-toolbox's own promises, which cover every repo."""
    named = os.environ.get("PERF_PROMISES_GLOBAL")
    return Path(named) if named else Path(__file__).resolve().parent.parent / FILE


def promise_files(folder):
    """The PromiseFiles that cover jobs in folder: its repo's file, when it has one, and
    llm-toolbox's, unless the repo is llm-toolbox, whose own copy then stands."""
    found = []
    top = top_of(Path(folder))
    local = None if top is None else top / FILE
    if local is not None and local.is_file():
        found.append(load(local))
    shared = global_file()
    if shared.is_file() and (local is None or shared.resolve() != local.resolve()):
        toml = load(shared)
        own_common = found[0].repo().common if found else None
        if own_common is None or own_common != toml.repo().common:
            found.append(toml)
    return found


def words_of(command):
    """The command's words as heavy records them, for matching: the first word cut
    to its last path part (`bin/heavy` and `/x/heavy` both read `heavy`)."""
    try:
        words = shlex.split(command)
    except ValueError:  # an unbalanced quote, as in "wt-ship's checks"
        words = command.split()
    if words:
        words[0] = words[0].rsplit("/", 1)[-1]
    return " ".join(words)


def short(seconds):
    return f"{seconds:.1f}" if seconds < 10 else f"{seconds:.0f}"


@dataclass
class Ran:
    """A finished job, as after_job judges it."""

    folder: str
    words: str
    seconds: float
    status: int | None
    signum: int | None
    how: str
    load: float | None  # other work's share of the job's cores, when measured
    session: str | None
    speed: float | None = None  # the machine's speed factor as it ended, when known
    speed_age: float | None = None  # seconds since that probe; None: as it ended

    def exit_text(self):
        if self.signum is not None:
            return f"sig{self.signum}"
        if self.status is not None and self.status < 0:
            return f"sig{-self.status}"
        return str(self.status)

    def load_text(self):
        if self.how == "share":
            return "share"
        if self.load is not None and self.load > LOAD_SHARE:
            return f"load={self.load:.2f}"
        return "-"


def at_usual_speed(seconds, speed):
    """A run's seconds as the machine's usual speed would have taken: its seconds
    times its speed factor, at most 1 (unknown: the seconds)."""
    return seconds if speed is None else seconds * min(speed, 1.0)


def slowness_explains(promise, seconds, speed):
    """Whether the machine's slowness alone takes the run to twice its promise or
    more: it does, and at the usual speed it wouldn't."""
    limit = BROKEN_RATIO * promise.seconds
    return seconds >= limit > at_usual_speed(seconds, speed)


def counted(promise, exit_text, load_text="-"):
    """Whether a run, by its exit and load fields, counts toward the promise's median
    (slowness_explains may still leave it out); with no load_text, whether its exit
    does, as a run's note asks."""
    if load_text != "-":
        return False
    if exit_text.startswith("sig"):
        return True
    try:
        return int(exit_text) in promise.counts
    except ValueError:
        return False


def after_job(job, folder, command, how, signum, tally):
    """At a heavy job's end: log it under each promise it matches and set
    job.promise_note to the one note the job gets, if any (a run of twice its
    promise or more, or a failure here), which this prints on stderr unless
    job.tell_promise is False, as the commit gate's is: it puts it in its pass
    line. A stop that comes meanwhile ends only the check (heavy's handler raises
    an Exception), so the job keeps its status; heavy's stop signals wait only
    while a line is appended, so no log is left with half a line."""
    try:
        job.promise_note = judge_job(job, folder, command, how, signum, tally)
        if job.promise_note is None:  # else a speed probe's problem goes untold
            readings = (tally.speed_at_start, tally.end().speed)
            job.promise_note = next(
                (r.problem for r in readings if r is not None and r.problem), None
            )
    except Exception as error:  # noqa: BLE001 - a promise must never fail a job
        stop = getattr(error, "signum", None)
        reason = (
            f"stopped by signal {stop}"
            if stop is not None
            else f"{type(error).__name__}: {str(error)[:100]}"
        )
        job.promise_note = f"heavy: can't check its perf promises ({reason})."
    if job.promise_note and job.tell_promise:
        tell(job.promise_note)


def tell(note):
    """Print note on stderr, if it can be: a stderr that's closed or whose reader has
    gone (`heavy -- x 2>&1 | head`) mustn't change the job's status. Python's own
    flush at exit would then fail on the note's bytes and exit 120, so stderr goes
    to /dev/null instead, where nothing more could be read anyway."""
    try:
        print(note, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - OSError, or ValueError for a closed file
        with suppress(Exception):  # nothing left to tell
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stderr.fileno())
            os.close(devnull)


def judge_job(job, folder, command, how, signum, tally):
    if folder is None or not Path(folder).is_dir():
        return None
    status = job.status
    if status is None and signum is None:
        return None
    words = words_of(command)
    files = promise_files(folder)
    matched = []  # the first match in each file
    for toml in files:
        first = next((p for p in toml.promises if p.matches(words)), None)
        if first is not None:
            matched.append((toml, first))
    if not matched:
        problems = [problem for toml in files for problem in toml.problems]
        return f"heavy: {problems[0]}." if problems else None
    used = tally.end()
    share = None
    if used.other is not None and used.seconds > 0:
        share = used.other / (used.seconds * max(1, len(job.cpus)))
    ran = Ran(
        folder,
        words,
        used.seconds,
        status,
        signum,
        how,
        share,
        os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
        used.speed.factor,
        used.speed.age,
    )
    notes = []
    for toml, promise in matched:
        log = toml.log(promise)
        if log is not None:
            line = line_of(ran, promise)
            before = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
            try:
                append(log, line)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, before)
        ratio = ran.seconds / promise.seconds
        if ratio >= BROKEN_RATIO and counted(promise, ran.exit_text()):
            notes.append((ratio, toml, promise))
    if not notes:
        return None
    _, toml, promise = max(notes, key=lambda item: item[0])
    usual = at_usual_speed(ran.seconds, ran.speed)
    note = f"heavy: {promise.name} ran {short(ran.seconds)} s"
    if usual < ran.seconds:
        note += f" ({short(usual)} s at the machine's {ran.speed:.2f}x speed"
        if ran.speed_age is not None:
            note += f", measured {ago(ran.speed_age)}"
        note += ")"
    note += (
        f", {usual / promise.seconds:.1f}x its promised {short(promise.seconds)} s"
        f" ({toml.repo().name}/{FILE})"
    )
    load = ran.load_text()
    if load == "share":
        note += ", but it shared a lane (--share), so it doesn't count"
    elif load != "-":
        note += (
            f", but other work used {ran.load:.0%} of its cores, so it doesn't count"
        )
    elif slowness_explains(promise, ran.seconds, ran.speed):
        note += ", so it doesn't count"
    return note + ": perf-promises status"


def ago(seconds):
    """`40 s ago` or `2 min ago`."""
    if round(seconds) < 60:
        return f"{round(seconds)} s ago"
    return f"{round(seconds / 60)} min ago"


def line_of(ran, promise, now=None):
    when = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    fields = [
        when.isoformat(timespec="seconds"),
        f"{ran.seconds:.1f}",
        ran.exit_text(),
        ran.load_text(),
        f"{promise.seconds:g}",
        promise.digest,
        ran.session or "-",
        ran.folder,
        "-" if ran.speed is None else f"{ran.speed:.2f}",
    ]
    return "\t".join(text.replace("\t", " ").replace("\n", " ") for text in fields)


def append(log, line, trim_at=TRIM_AT, keep=KEEP):
    """Append one line in one write; past trim_at lines, cut to the newest keep."""
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    if log.read_bytes().count(b"\n") <= trim_at:
        return
    lock = os.open(log.with_name(f".{log.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = log.read_bytes().splitlines(keepends=True)
        if len(lines) > trim_at:
            part = log.with_name(f".{log.name}.{os.getpid()}.part")
            try:
                part.write_bytes(b"".join(lines[-keep:]))
                part.rename(log)
            finally:
                part.unlink(missing_ok=True)
    finally:
        os.close(lock)


@dataclass
class Logged:
    when: datetime
    seconds: float
    exit: str
    load: str
    promised: float
    digest: str
    speed: float | None  # the machine's speed factor, None unknown or not logged


def logged(log):
    """The log's lines it can read, oldest first."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return []
    found = []
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) < 6:
            continue
        try:
            when = datetime.fromisoformat(parts[0])
            if when.tzinfo is None:
                continue
            speed = parts[8] if len(parts) > 8 else "-"
            found.append(
                Logged(
                    when,
                    float(parts[1]),
                    parts[2],
                    parts[3],
                    float(parts[4]),
                    parts[5],
                    None if speed == "-" else float(speed),
                )
            )
        except ValueError:
            continue
    return found


def nearest_rank(ordered, share):
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


@dataclass
class Verdict:
    """A promise judged on its log: runs logged (any definition), runs counted, the
    median of the newest JUDGED_RUNS counted (None below that), their p90 of every
    counted run, whether it's broken, the flags, and whether any run of its current
    command is logged."""

    logged: int
    counted: int
    median: float | None
    p90: float | None
    broken: bool
    flags: list
    matched: bool


def judge(toml, promise, now=None):
    now = datetime.now(UTC) if now is None else now
    log = toml.log(promise)
    lines = [] if log is None else logged(log)
    same_command = [line for line in lines if line.digest == promise.digest]
    # The runs whose exit counts, and of them the ones left out for load or speed.
    finished = [
        line
        for line in same_command
        if line.promised == promise.seconds
        and line.when >= now - timedelta(days=COUNT_DAYS)
        and counted(promise, line.exit)
    ]
    recent = [
        line
        for line in finished
        if counted(promise, line.exit, line.load)
        and not slowness_explains(promise, line.seconds, line.speed)
    ]
    times = [at_usual_speed(line.seconds, line.speed) for line in recent]
    middle = (
        statistics.median(times[-JUDGED_RUNS:]) if len(times) >= JUDGED_RUNS else None
    )
    p90 = nearest_rank(sorted(times), 0.9) if times else None
    broken = middle is not None and middle >= BROKEN_RATIO * promise.seconds
    flags = []
    if promise.seconds > DODGE_RATIO * promise.measured_seconds:
        flags.append(
            f"promises {short(promise.seconds)} s, over {DODGE_RATIO:g}x the"
            f" {short(promise.measured_seconds)} s measured"
        )
    if same_command and same_command[-1].when < now - timedelta(days=UNMATCHED_DAYS):
        flags.append(f"matched no job in {UNMATCHED_DAYS} days")
    last = finished[-LEFT_OUT_OF:]
    kept = {id(line) for line in recent}
    left_out = sum(id(line) not in kept for line in last)
    if 2 * left_out > len(last):
        flags.append(
            f"{left_out} of its last {len(last)} runs were left out, for the"
            " machine's speed or load"
        )
    matched = bool(same_command)
    return Verdict(len(lines), len(times), middle, p90, broken, flags, matched)


def status_lines(folder, now=None):
    """What `perf-promises status` prints, and whether any promise is broken."""
    files = promise_files(folder)
    if not files:
        return [f"perf-promises: no {FILE} covers {folder}."], False
    lines, any_broken = [], False
    for toml in files:
        lines.append(f"{toml.repo().name} ({toml.path}):")
        lines += [f"  problem: {problem}" for problem in toml.problems]
        for promise in toml.promises:
            verdict = judge(toml, promise, now)
            any_broken |= verdict.broken
            line = (
                f"  {promise.name}: promised {short(promise.seconds)} s,"
                f" {verdict.logged} runs logged"
            )
            if verdict.median is None:
                line += f", n={verdict.counted}, not judged"
            else:
                line += (
                    f", last-{JUDGED_RUNS} median {short(verdict.median)} s"
                    f" ({verdict.median / promise.seconds:.1f}x)"
                )
            if verdict.p90 is not None:
                line += f", p90 {short(verdict.p90)} s"
            if verdict.broken:
                line += ": BROKEN"
            line += "".join(f"; {flag}" for flag in verdict.flags)
            if not verdict.matched:
                line += "; never matched"
            lines.append(line)
    return lines, any_broken


def alarm_lines(folder, now=None):
    """The lines a new session sees: broken promises, flagged ones, and files that
    can't be read, of folder's repo and llm-toolbox's."""
    lines = []
    for toml in promise_files(folder):
        repo = toml.repo().name
        lines += [
            f"perf-promises: {problem}; perf-promises status"
            for problem in toml.problems
        ]
        for promise in toml.promises:
            verdict = judge(toml, promise, now)
            if verdict.broken:
                lines.append(
                    f"perf promise broken in {repo}: {promise.name} median"
                    f" {short(verdict.median)} s over its last {JUDGED_RUNS} runs,"
                    f" {verdict.median / promise.seconds:.1f}x the"
                    f" {short(promise.seconds)} s promised ({promise.source});"
                    " perf-promises status"
                )
            lines += [
                f"perf promise in {repo} flagged: {promise.name} {flag}"
                f" ({promise.source}); perf-promises status"
                for flag in verdict.flags
            ]
    return lines
