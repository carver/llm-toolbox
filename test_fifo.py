"""lib/fifo.py's lock: a terminal's stop signals wait until its holder lets go of it,
and a waiter held up for FIFO_LOCK_SECONDS names the holder, saying when it's stopped.
Each test holds the lock in small helper processes it controls."""

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
import fifo

# Take the lock in each folder argv[1] names (os.pathsep between them), one inside the
# other, as argv[2], log it in argv[3], and hold them until the file <name>.release
# appears beside the log; "raw" as argv[4] takes a bare flock instead, as a fifo that
# leaves no record of its holder would.
HELPER = f"""
import fcntl, os, sys, time
from contextlib import ExitStack
from pathlib import Path
sys.path.insert(0, {str(ROOT / "lib")!r})
import fifo

folders = [Path(part) for part in sys.argv[1].split(os.pathsep)]
name, log = sys.argv[2], Path(sys.argv[3])
release = log.parent / f"{{name}}.release"


def hold():
    with log.open("a") as out:
        out.write(f"{{name}} got\\n")
    while not release.exists():
        time.sleep(0.01)
    with log.open("a") as out:
        out.write(f"{{name}} releasing\\n")


if sys.argv[4:] == ["raw"]:
    with (folders[0] / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        hold()
else:
    with ExitStack() as stack:
        for folder in folders:
            stack.enter_context(fifo.locked(folder))
        hold()
time.sleep(60)  # stays alive, so the test can see it stopped after letting go
"""


@pytest.fixture
def helpers(tmp_path):
    """Start a helper that takes the lock in each of folders, one inside the other
    (by default the test's queue folder alone); every one is killed at the end,
    stopped or not. Each starts its own process group in the test's session, since a
    terminal's stop signal sent to an orphaned process group is dropped."""
    queue = tmp_path / "queue"
    queue.mkdir()
    log = tmp_path / "log"
    procs = []

    def start(name, folders=(queue,), env=None, raw=False):
        err = tmp_path / f"{name}.err"
        where = os.pathsep.join(map(str, folders))
        proc = subprocess.Popen(
            [sys.executable, "-c", HELPER, where, name, str(log)]
            + (["raw"] if raw else []),
            env=os.environ | (env or {}),
            stderr=err.open("w"),
            process_group=0,
        )
        procs.append(proc)
        return proc, err

    yield queue, log, start
    for proc in procs:
        proc.kill()
        proc.wait()


def wait_until(condition, what, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


def state(pid):
    """The process's state letter, from /proc/PID/stat."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def pending(pid, signum):
    """Whether signum waits, blocked, for the whole process."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("ShdPnd:"):
            return bool(int(line.split()[1], 16) >> (signum - 1) & 1)
    return False


def record(folder, pid, host=None):
    """Write the lock's holder record for process pid, as on host (this one by
    default)."""
    since = datetime.now().astimezone().isoformat(timespec="seconds")
    holder = fifo.Holder(
        "s-other", since, host or socket.gethostname(), str(pid), fifo.started(pid)
    )
    (folder / ".lock.holder").write_text(holder.line())


def test_a_waiter_names_a_stopped_holder_once_and_waits_for_it(tmp_path, helpers):
    folder, log, start = helpers
    holder, _ = start("holder")
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    os.kill(holder.pid, signal.SIGSTOP)  # SIGSTOP: the stuck holder is the scenario
    wait_until(lambda: state(holder.pid) == "T", "the holder to stop")
    for name in ("first", "second"):
        (tmp_path / f"{name}.release").touch()
    _, first_err = start("first", env={"FIFO_LOCK_SECONDS": "0.1"})
    named = f"PID {holder.pid}"
    wait_until(lambda: named in first_err.read_text(), "the waiter to name the holder")
    # A second waiter, which looks four times as seldom, names it too; by then the
    # first has looked again a few times.
    _, second_err = start("second", env={"FIFO_LOCK_SECONDS": "0.4"})
    wait_until(lambda: named in second_err.read_text(), "the second waiter's line")

    (tmp_path / "holder.release").touch()
    os.kill(holder.pid, signal.SIGCONT)
    wait_until(lambda: len(lines(log)) == 6, "both waiters to take the lock in turn")

    # Either waiter may hold the lock a moment while the other looks: count the
    # holder's lines alone.
    said = [line for line in lines(first_err) if named in line]
    assert len(said) == 1, said
    assert "stopped" in said[0], said
    assert f"kill -CONT {holder.pid}" in said[0], said
    assert str(folder) in said[0], said
    assert lines(log)[:2] == ["holder got", "holder releasing"]


def test_a_waiter_names_a_running_holder_without_calling_it_stopped(tmp_path, helpers):
    _, log, start = helpers
    holder, _ = start("holder")
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    (tmp_path / "waiter.release").touch()
    _, err = start("waiter", env={"FIFO_LOCK_SECONDS": "0.1"})
    # Well inside the 5 s a waiter that ignored FIFO_LOCK_SECONDS would take.
    wait_until(
        lambda: f"PID {holder.pid}" in err.read_text(), "the holder's line", seconds=4
    )
    (tmp_path / "holder.release").touch()
    wait_until(lambda: "waiter got" in lines(log), "the waiter to take the lock")

    said = err.read_text()
    assert "stopped" not in said, said
    assert "kill -CONT" not in said, said


@pytest.mark.parametrize("left", ["nothing", "a dead holder's record", "a folder"])
def test_a_waiter_behind_a_holder_that_left_no_record_says_so(tmp_path, helpers, left):
    queue, log, start = helpers
    if left == "a dead holder's record":  # one killed while it held the lock
        dead = subprocess.Popen(["sleep", "60"])
        record(queue, dead.pid)
        dead.kill()
        dead.wait()
    elif left == "a folder":  # a record it can't read
        (queue / ".lock.holder").mkdir()
    start("holder", raw=True)
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    (tmp_path / "waiter.release").touch()
    _, err = start("waiter", env={"FIFO_LOCK_SECONDS": "0.1"})
    wait_until(lambda: "no record" in err.read_text(), "the waiter to say so")
    (tmp_path / "holder.release").touch()
    wait_until(lambda: "waiter got" in lines(log), "the waiter to take the lock")

    assert len(lines(err)) == 1, err.read_text()


def test_a_holder_on_another_host_is_named_with_it_and_never_called_stopped(
    tmp_path, helpers
):
    queue, log, start = helpers
    holder, _ = start("holder", raw=True)
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    # A PID here that is stopped tells nothing about one on another host.
    os.kill(holder.pid, signal.SIGSTOP)  # SIGSTOP: a helper, stopped holding the lock
    wait_until(lambda: state(holder.pid) == "T", "the holder to stop")
    record(queue, holder.pid, host="far-away")
    (tmp_path / "waiter.release").touch()
    _, err = start("waiter", env={"FIFO_LOCK_SECONDS": "0.1"})
    wait_until(lambda: "far-away" in err.read_text(), "the waiter to name the holder")
    (tmp_path / "holder.release").touch()
    os.kill(holder.pid, signal.SIGCONT)
    wait_until(lambda: "waiter got" in lines(log), "the waiter to take the lock")

    said = err.read_text()
    assert f"PID {holder.pid} on far-away" in said, said
    assert "stopped" not in said, said


def test_a_terminal_stop_inside_the_lock_waits_for_its_release(tmp_path, helpers):
    _, log, start = helpers
    holder, _ = start("holder")
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    os.kill(holder.pid, signal.SIGTSTP)
    wait_until(
        lambda: state(holder.pid) == "T" or pending(holder.pid, signal.SIGTSTP),
        "the stop to land",
    )
    assert state(holder.pid) != "T", "stopped while it held the lock"

    (tmp_path / "holder.release").touch()
    (tmp_path / "next.release").touch()
    start("next")
    wait_until(lambda: "next got" in lines(log), "another process to take the lock")
    wait_until(lambda: state(holder.pid) == "T", "the holder to stop once it let go")


def test_a_terminal_stop_waits_while_an_outer_lock_is_held_too(tmp_path, helpers):
    queue, log, start = helpers
    lane = queue / "lane"  # as heavy takes its queue's lock, then a lane's inside it
    lane.mkdir()
    start("lane", folders=[lane])
    wait_until(lambda: "lane got" in lines(log), "the lane's lock to be held")
    (tmp_path / "both.release").touch()
    both, err = start("both", folders=[queue, lane], env={"FIFO_LOCK_SECONDS": "0.1"})
    # Holding the outer lock, it waits for the lane's, napping between tries.
    wait_until(lambda: "waiting" in err.read_text(), "it to wait for the lane's lock")
    os.kill(both.pid, signal.SIGTSTP)
    wait_until(
        lambda: state(both.pid) == "T" or pending(both.pid, signal.SIGTSTP),
        "the stop to land",
    )
    assert state(both.pid) != "T", "stopped while it held the outer lock"

    (tmp_path / "lane.release").touch()
    (tmp_path / "next.release").touch()
    wait_until(lambda: "both releasing" in lines(log), "it to take both locks")
    start("next")
    wait_until(lambda: "next got" in lines(log), "another process to take the outer")
    wait_until(lambda: state(both.pid) == "T", "it to stop once it let go of both")


def test_heavy_names_a_stopped_holder_of_its_queues_lock_and_runs_after_it(
    tmp_path, helpers
):
    _, log, start = helpers
    queue = Path(os.environ["HEAVY_DIR"])  # each test's own (conftest.py)
    queue.mkdir(parents=True, exist_ok=True)
    holder, _ = start("holder", folders=[queue])
    wait_until(lambda: "holder got" in lines(log), "the holder to take the lock")
    os.kill(holder.pid, signal.SIGSTOP)  # SIGSTOP: the stuck holder is the scenario
    wait_until(lambda: state(holder.pid) == "T", "the holder to stop")
    err = tmp_path / "heavy.err"
    job = subprocess.Popen(
        [str(ROOT / "bin" / "heavy"), "--", "sh", "-c", f'echo "heavy ran" >> "{log}"'],
        env=os.environ | {"FIFO_LOCK_SECONDS": "0.1"},
        stdout=subprocess.DEVNULL,
        stderr=err.open("w"),
        cwd=tmp_path,
    )
    try:
        named = f"kill -CONT {holder.pid}"
        wait_until(lambda: named in err.read_text(), "heavy to name the holder")
        (tmp_path / "holder.release").touch()
        os.kill(holder.pid, signal.SIGCONT)
        status = job.wait(timeout=30)
    finally:
        if job.poll() is None:
            job.kill()
            job.wait()

    said = [line for line in lines(err) if f"PID {holder.pid}" in line]
    assert status == 0, err.read_text()
    assert len(said) == 1 and said[0].startswith("heavy: "), said
    assert lines(log) == ["holder got", "holder releasing", "heavy ran"]
