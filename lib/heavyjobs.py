"""heavy's record of the jobs it ran, so a run that something slowed can be traced
afterwards: which jobs ran beside it, for whom, and how they got in.

Each job a heavy queues or runs outside the queue adds two JSON lines to
HEAVY_DIR/jobs.jsonl, one as it starts and one as it ends, sharing the job's `id`; a
job whose heavy was killed leaves only the first. A heavy inside a job adds none: its
work is the job's.

The start line holds when the job joined the queue (`joined`) and started (`at_start`),
epoch seconds; the process (`session`, `pid`, `proc_start`, `host`, and `parents`, its
parents' PIDs and names); its folder (`cwd`) and command (`cmd`, cut as `shown` says,
`cut` when words were dropped, and `key`, heavy's hash of its repo and whole command;
`cwd` and `key` are None when its folder was removed); how it ran (`lane`, `how`:
`slot`, `share` beside the holder named in `beside`, or `outside` the queue as a
--few-seconds job; the flags it asked for; its `cores` and `nice`). The end line holds
`at_end`, `exit` (CMD's status, or None), `signal` (the signal that stopped heavy, or
None), `cmd_pid` (CMD's PID, None when heavy's caller doesn't say), and the CPU-seconds
the job used (`cpu`), other work used on its cores meanwhile (`other_cpu`) and sccache's
server used (`sccache_cpu`), when they could be measured.

A line is appended in one write, without the queue's lock. Past TRIM_BYTES the file
is cut to its newest KEEP_LINES lines, by a rename: a line another heavy appends in
that moment can be lost. Nothing here may fail a job: a record that can't be written,
or a bug in writing it, is a note on stderr, and heavy's stop signals wait while a
line is written, so one can't be lost to the record.
"""

import json
import os
import re
import shlex
import signal
import socket
import sys
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import fifo

VERSION = 1
NAME = "jobs.jsonl"
CHARS = 120  # of a command's words, at most
TRIM_BYTES = 2 * 2**20
KEEP_LINES = 2000  # about a thousand jobs, a busy day's
PARENTS = 6  # of a job's parent processes, at most
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
BARE_OPTION = re.compile(r"-[A-Za-z0-9]|--[A-Za-z0-9][A-Za-z0-9-]*")


def path(queue):
    return Path(queue) / NAME


def shown(command):
    """(the words of command a record keeps, whether any were dropped). They stop
    before the first word that may be a value: one holding `=` (`--token=X`,
    `TOKEN=X`) or `@` (a URL's credentials), one after an option (`--token X`, `-p
    server`), or an option that isn't a bare `-X` or `--word`, which may hold its
    value (`-pSECRET`); and within CHARS. A secret that isn't a value, a bare word
    after the subcommand (`./deploy prod TOKEN`), is still kept."""
    try:
        words, join = shlex.split(command), shlex.join
    except ValueError:  # unbalanced quotes, as in "wt-ship's checks": shown as typed
        words, join = command.split(), " ".join
    kept = []
    for word in words:
        if kept and (
            "=" in word
            or "@" in word
            or (word.startswith("-") and not BARE_OPTION.fullmatch(word))
            or (kept[-1].startswith("-") and not word.startswith("-"))
        ):
            break
        if len(join([*kept, word])) > CHARS:
            break
        kept.append(word)
    return join(kept), len(kept) < len(words)


def parents(pid):
    """Process pid's parents, nearest first, as [pid, name] pairs, up to PARENTS."""
    found = []
    while len(found) < PARENTS:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat.rsplit(")", 1)[1].split()[1])
        if pid <= 1:
            break
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            name = "?"
        found.append([pid, name])
    return found


def guarded(write):
    """Run write() with heavy's stop signals held back, so a stop that comes meanwhile
    acts once it's done; any failure is a note."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
    try:
        write()
    except Exception as error:  # noqa: BLE001 - the record must never fail a job
        reason = error.strerror if isinstance(error, OSError) else str(error)
        reason = f"{type(error).__name__}: {(reason or '')[:100]}"
        print(f"heavy: can't record this job ({reason}).", file=sys.stderr, flush=True)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


def append(queue, line):
    """Append one line to the record, in one write; past TRIM_BYTES, cut it."""
    data = (json.dumps(line) + "\n").encode()
    target = path(queue)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        size = os.fstat(fd).st_size
    finally:
        os.close(fd)
    if size > TRIM_BYTES:
        # Bytes, so one that isn't UTF-8 can't stop the cut.
        kept = b"".join(target.read_bytes().splitlines(keepends=True)[-KEEP_LINES:])
        part = target.with_name(f".{target.name}.{os.getpid()}.part")
        try:
            part.write_bytes(kept)
            part.rename(target)
        finally:
            part.unlink(missing_ok=True)


def started(queue, fields):
    """Record a job's start: fields and who runs it. Returns its id, for ended()."""
    pid = os.getpid()
    now = time.time()
    ident = f"{pid}-{now:.6f}"

    def write():
        line = {"v": VERSION, "id": ident, "at_start": now, "pid": pid}
        line |= {
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "proc_start": fifo.started(str(pid)),
            "host": socket.gethostname(),
            "parents": parents(pid),
        }
        append(queue, line | fields)

    guarded(write)
    return ident


def ended(queue, ident, fields):
    """Record a job's end: what fields() gives, called only here, so a failure in it
    is a note too."""
    guarded(
        lambda: append(
            queue, {"v": VERSION, "id": ident, "at_end": time.time()} | fields()
        )
    )


def jobs(queue):
    """The recorded jobs, oldest first: each its start and end lines merged. A line
    that isn't a JSON object with an id is skipped, as is a job with no start."""
    found = {}
    try:
        text = path(queue).read_text(errors="replace")
    except OSError:
        return []
    for raw in text.splitlines():
        with suppress(ValueError):
            line = json.loads(raw)
            if isinstance(line, dict) and isinstance(line.get("id"), str):
                found.setdefault(line["id"], {}).update(line)
    whole = [job for job in found.values() if number(job.get("at_start")) is not None]
    return sorted(whole, key=lambda job: job["at_start"])


def number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if ok else None


def running(job):
    """Whether a job with no end recorded still runs: its process is alive."""
    pid = job.get("pid")
    holder = fifo.Holder(
        "?",
        "?",
        job.get("host"),
        None if pid is None else str(pid),
        job.get("proc_start"),
    )
    return pid is not None and not holder.gone()


def span_of(job, now):
    """(start, end) of a job: an unfinished one runs to now."""
    end = number(job.get("at_end"))
    return job["at_start"], now if end is None else end


def overlapping(found, start, end, now=None):
    """The jobs of found that ran at some moment between start and end, as (job,
    seconds of overlap) pairs."""
    now = time.time() if now is None else now
    pairs = []
    for job in found:
        begun, done = span_of(job, now)
        if begun <= end and done >= start:
            pairs.append((job, max(0.0, min(done, end) - max(begun, start))))
    return pairs


def moment(text, now=None):
    """An epoch time from text: ISO (`2026-11-24T23:16`, with or without an offset),
    `HH:MM[:SS]`, the latest past such moment, or an age (`90s`, `30m`, `2h`, `1d`) back
    from now. Raises ValueError."""
    now = time.time() if now is None else now
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1:] in units and text[:-1].replace(".", "", 1).isdigit():
        return now - float(text[:-1]) * units[text[-1]]
    if ":" in text and "-" not in text and "T" not in text:
        clock = time.strptime(text, "%H:%M:%S" if text.count(":") == 2 else "%H:%M")
        today = datetime.fromtimestamp(now).astimezone()
        found = today.replace(
            hour=clock.tm_hour, minute=clock.tm_min, second=clock.tm_sec, microsecond=0
        )
        if found.timestamp() > now:
            found -= timedelta(days=1)
        return found.timestamp()
    found = datetime.fromisoformat(text)
    return (found if found.tzinfo else found.astimezone()).timestamp()


def window(text, now=None):
    """(start, end) epoch times from `A..B`, or one moment as both. A start later than
    its end, as `23:50..00:20` reads, is the day before. Raises ValueError."""
    if ".." in text:
        first, _, second = text.partition("..")
        start, end = moment(first, now), moment(second, now)
        if start > end and ":" in first and "-" not in first:
            start -= 86400
        if start > end:
            raise ValueError(f"{text} ends before it starts")
        return start, end
    found = moment(text, now)
    return found, found


def seconds(span):
    if span < 10:
        return f"{span:.1f}s"
    span = round(span)
    if span < 60:
        return f"{span}s"
    if span < 3600:
        return f"{span // 60}m{span % 60:02d}s"
    return f"{span // 3600}h{span // 60 % 60:02d}m"


def cpu(seconds_used):
    """CPU-seconds, as `0.4s` or `3540s`."""
    return f"{seconds_used:.{1 if seconds_used < 10 else 0}f}s"


def clock(when):
    return datetime.fromtimestamp(when).astimezone().strftime("%m-%d %H:%M:%S")


def outcome(job):
    if number(job.get("at_end")) is None:
        return "running" if running(job) else "no end recorded"
    if isinstance(job.get("signal"), int):
        return f"stopped by signal {job['signal']}"
    status = job.get("exit")
    if isinstance(status, int) and status < 0:
        return f"killed by signal {-status}"
    return f"exit {'?' if status is None else status}"


def mode(job):
    """How the job ran: its lane, the flags it asked for, and where it ran."""
    words = [str(job.get("lane", "?"))]
    words += [flag for flag, name in FLAGS if job.get(name)]
    if job.get("how") == "outside":
        words.append("outside the queue")
    beside = job.get("beside")
    beside = beside if isinstance(beside, dict) else {}
    if job.get("how") == "share" and beside.get("pid"):
        words.append(f"beside pid {beside['pid']}")
    return " ".join(words)


FLAGS = (
    ("--long", "long"),
    ("--alone", "alone"),
    ("--share", "share"),
    ("--few-seconds", "quick"),
    ("at the front", "front"),
)


def who(job):
    """The job's session, or with none its nearest named parents."""
    if job.get("session"):
        return str(job["session"])[:8]
    found = job.get("parents")
    found = found if isinstance(found, list) else []
    pairs = [pair for pair in found if isinstance(pair, list) and len(pair) == 2]
    names = [str(name) for _, name in pairs][:3]
    return "via " + "<".join(names) if names else "no session"


def describe(job, now=None):
    """One line saying what the job was, when, and how it went."""
    now = time.time() if now is None else now
    begun, done = span_of(job, now)
    waited = max(0.0, begun - (number(job.get("joined")) or begun))
    used = ""
    if number(job.get("cpu")) is not None:
        used = f", cpu {cpu(job['cpu'])}"
        if number(job.get("other_cpu")) is not None:
            used += f", other work {cpu(job['other_cpu'])} on its cores"
    home = os.path.expanduser("~")
    cwd = str(job.get("cwd", "?"))
    cwd = "~" + cwd[len(home) :] if home != "/" and cwd.startswith(home) else cwd
    return (
        f"{clock(begun)} {seconds(done - begun):>7} {outcome(job)}, {mode(job)},"
        f" waited {seconds(waited)}{used}; {who(job)} pid {job.get('pid')}"
        f" in {cwd}: {job.get('cmd', '?')}"
    )


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def brief(job, now):
    """A short line for a job: its start, length, how it ran, its CPU, and its
    command in its folder's last name."""
    begun, done = span_of(job, now)
    used = f", cpu {cpu(job['cpu'])}" if number(job.get("cpu")) is not None else ""
    folder = os.path.basename(str(job.get("cwd") or "?").rstrip("/")) or "/"
    return (
        f"{clock(begun)} {seconds(done - begun)} {mode(job)}{used}; {who(job)}"
        f" in {folder}: {job.get('cmd', '?')}"
    )


def beside(queue, pid, since, most=3, now=None):
    """Lines for what ran beside pid's job that started at or after since: a summary
    (how many jobs overlapped it, how many of them at full priority, the CPU other
    work used on its cores) and the `most` that used the most CPU. Empty when no such
    job is on the record."""
    now = time.time() if now is None else now
    found = jobs(queue)
    mine = [job for job in found if job.get("pid") == pid and job["at_start"] >= since]
    if not mine:
        return []
    target = mine[-1]
    start, end = span_of(target, now)
    pairs = overlapping([job for job in found if job is not target], start, end, now)
    full = sum(1 for job, _ in pairs if not job.get("nice"))
    line = f"heavy: {plural(len(pairs), 'job')} overlapped it ({full} at full priority)"
    other = number(target.get("other_cpu"))
    if other is not None:
        cores = other / max(end - start, 1e-9)
        line += f"; other work used {cpu(other)} on its cores ({cores:.1f} cores)"
    heaviest = sorted(pairs, key=lambda pair: -(number(pair[0].get("cpu")) or 0))
    lines = [line + "."]
    lines += [f"  {brief(job, now)}" for job, _ in heaviest[:most]]
    return lines


def report(queue, since=None, around=None, now=None):
    """What `heavy history` prints: the jobs that started since `since` (by default,
    with no `around`, the last hour), or those that ran during `around`, an epoch
    (start, end) or a PID, whose latest job the lines are about."""
    now = time.time() if now is None else now
    found = jobs(queue)
    if not found:
        return "heavy: no jobs recorded."
    if around is None:
        start = now - 3600 if since is None else since
        shown_jobs = [job for job in found if job["at_start"] >= start]
        older = len(found) - len(shown_jobs)
        head = f"heavy: {plural(len(shown_jobs), 'job')} since {clock(start)}"
        head += f" ({older} older; --since to see them)." if older else "."
        return "\n".join([head, *(describe(job, now) for job in shown_jobs)])
    pool = [job for job in found if since is None or job["at_start"] >= since]
    if isinstance(around, int):
        mine = [job for job in found if around in (job.get("pid"), job.get("cmd_pid"))]
        if not mine:
            return f"heavy: no job recorded with pid {around}."
        target = mine[-1]
        start, end = span_of(target, now)
        pool = [job for job in pool if job is not target]
        about = f"pid {around}'s `{target.get('cmd', '?')}`"
    else:
        start, end = around
        target, about = None, "that time"
    pairs = overlapping(pool, start, end, now)
    head = (
        f"heavy: {plural(len(pairs), 'job')} overlapped {about}"
        f" ({clock(start)} to {clock(end)})."
    )
    lines = [head]
    if target is not None:
        lines.append(describe(target, now))
    lines += [
        f"  {describe(job, now)} (overlap {seconds(overlap)})" for job, overlap in pairs
    ]
    return "\n".join(lines)
