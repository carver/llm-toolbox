"""What heavy remembers of each command's runs, so a waiter whose command usually keeps
few cores busy can share a lane: the cores and seconds of its last few runs that
exited 0.

A command is known by its key, a hash of its repo and its command line: every worktree
of a repo shares its history, since the repo is git's common folder. The history lives
in one file, history.json in the queue's folder, written whole under the queue's lock.
A file that's missing, unreadable or the wrong shape counts as no history, and a write
that fails is noted and skipped, so neither stops a job.

It reads HEAVY_CORES_USED, a number that stands in for the cores a run kept busy (the
tests' stand-in for measuring).
"""

import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import fifo

VERSION = 1
RUNS = 5  # runs kept per command
COMMANDS = 200  # commands kept, the most recently run
MAX_AGE = 30 * 24 * 3600  # seconds since a command last ran before it's dropped
GIT_TIMEOUT = 5


def key(folder, command):
    """The key of command run in folder: its repo (git's common folder, so worktrees
    share one) or, outside git, the folder itself, with the command."""
    repo = str(folder)
    with suppress(OSError, subprocess.SubprocessError):
        found = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
        if found.returncode == 0 and found.stdout.strip():
            repo = found.stdout.strip()
    return hashlib.sha256(f"{repo}\0{command}".encode()).hexdigest()[:16]


def path(queue):
    return Path(queue) / "history.json"


def read(queue):
    """The history file's contents as a dict, or {} when it's missing or unreadable."""
    try:
        data = json.loads(path(queue).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runs(entry):
    """An entry's runs that have the right shape: [cores, seconds] pairs of numbers."""
    found = entry.get("runs") if isinstance(entry, dict) else None
    if not isinstance(found, list):
        return []
    return [
        run
        for run in found
        if isinstance(run, list)
        and len(run) == 2
        and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in run)
    ]


class History:
    """The usual cores of the commands heavy has seen, as read at one moment."""

    def __init__(self, queue):
        commands = read(queue).get("commands")
        self.commands = commands if isinstance(commands, dict) else {}

    def usual(self, command_key):
        """The most cores any of the command's recorded runs kept busy, or None."""
        found = runs(self.commands.get(command_key))
        return max(cores for cores, _ in found) if found else None


def record(queue, command_key, cores, seconds):
    """Add a run to the command's history, under the queue's lock. A history that
    can't be saved (a full disk, the queue's folder gone) is noted and left be."""
    try:
        with fifo.locked(Path(queue)):
            data = read(queue)
            if isinstance(data.get("version"), int) and data["version"] > VERSION:
                return  # a newer heavy's file: leave it be
            new = json.dumps(added(data, command_key, cores, seconds))
            fifo.write_whole(path(queue), new)
    except OSError as error:
        print(
            f"heavy: can't save its history ({error.strerror}), so this run"
            " isn't remembered.",
            file=sys.stderr,
            flush=True,
        )


def added(data, command_key, cores, seconds):
    """The history data with a run added to the command's entry, and the commands
    past MAX_AGE or COMMANDS dropped; other keys, at each level, stay as they were."""
    commands = data.get("commands")
    commands = dict(commands) if isinstance(commands, dict) else {}
    now = time.time()
    entry = commands.get(command_key)
    commands[command_key] = (entry if isinstance(entry, dict) else {}) | {
        "runs": (runs(entry) + [[round(cores, 3), round(seconds, 3)]])[-RUNS:],
        "at": now,
    }
    kept = sorted(
        (
            (name, entry)
            for name, entry in commands.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("at"), (int, float))
            and now - entry["at"] <= MAX_AGE
        ),
        key=lambda item: item[1]["at"],
        reverse=True,
    )[:COMMANDS]
    return data | {"version": VERSION, "commands": dict(kept)}


class Run:
    """A run's measure: the CPU time of this process's reaped children (CMD and what
    it waited for) over the wall time, from when it starts to when it ends."""

    def __init__(self):
        self.cpu, self.since = cpu_time(), time.monotonic()

    def cores(self):
        """(the cores the run kept busy on average, its seconds)."""
        seconds = time.monotonic() - self.since
        fake = os.environ.get("HEAVY_CORES_USED")
        if fake:
            return float(fake), seconds
        return (cpu_time() - self.cpu) / seconds if seconds > 0 else 0.0, seconds


def cpu_time():
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    return used.ru_utime + used.ru_stime
