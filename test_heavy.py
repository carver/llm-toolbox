"""heavy runs a CPU-heavy command once it's its turn in its lane and its cores are
quiet, and lets a second job share a lane, niced, when there's room. The tests run it
as a process with its queue in tmp_path, files standing in for how busy the cores are
(HEAVY_BUSY) and for free memory (HEAVY_MEMINFO, too little to share unless a test
asks for the `sharing` fixture), and short waits."""

import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import wait_or_warn

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "bin" / "heavy"
sys.path.insert(0, str(ROOT / "lib"))
import cores
import fifo


def set_busy(busy, share):
    """Put `share` in the busy file whole, by a rename: heavy reads the file while a
    test may be changing it, and a plain write shows it an empty file between the
    truncate and the write."""
    part = busy.with_name(f"{busy.name}.part")
    part.write_text(f"{share}\n")
    part.replace(busy)


def meminfo(path, gib):
    """Put `gib` GiB of MemAvailable in the meminfo file, whole, as set_busy does."""
    part = path.with_name(f"{path.name}.part")
    part.write_text(f"MemTotal: 16777216 kB\nMemAvailable: {int(gib * 2**20)} kB\n")
    part.replace(path)


@pytest.fixture
def box(tmp_path):
    """The queue folder, the busy file (quiet to start) and an env to run heavy in,
    with too little free memory for a job to share a lane."""
    busy = tmp_path / "busy"
    set_busy(busy, "0.0")
    meminfo(tmp_path / "meminfo", 1)
    env = os.environ | {
        "HEAVY_DIR": str(tmp_path / "heavy"),
        "HEAVY_BUSY": str(busy),
        "HEAVY_MEMINFO": str(tmp_path / "meminfo"),
        "HEAVY_QUIET_SECONDS": "0.2",
        "HEAVY_QUICK_SECONDS": "0.1",
        "HEAVY_POLL": "0.05",
        "HEAVY_SHARE_SECONDS": "600",
        "HEAVY_STALL_SECONDS": "600",
        "CLAUDE_CODE_SESSION_ID": "s-test",
    }
    for name in ("HEAVY_SLOT", "HEAVY_CORES_USED"):
        env.pop(name, None)
    return tmp_path, busy, env


@pytest.fixture
def sharing(box):
    """Room to share: 16 GiB free, and a holder idle for 0.3 s lets a waiter in."""
    tmp_path, _, env = box
    meminfo(tmp_path / "meminfo", 16)
    env["HEAVY_SHARE_SECONDS"] = "0.3"


@pytest.fixture
def background(box):
    """Start heavy in the background; every run is killed and reaped at the end."""
    procs = []
    tmp_path, _, env = box

    def start(*args, session="s-test", stderr=None, cwd=None, **extra):
        """stderr: a file to write heavy's stderr to, to read while it runs."""
        proc = subprocess.Popen(
            [str(SCRIPT), *args],
            env=env | {"CLAUDE_CODE_SESSION_ID": session, **extra},
            stdout=subprocess.PIPE,
            stderr=stderr.open("w") if stderr else subprocess.PIPE,
            text=True,
            cwd=cwd or tmp_path,
        )
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A command heavy failed to stop still holds the pipes: don't wait on it.
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()


def heavy(env, *args, cwd=None, **extra):
    """Run heavy to its end, by default in the test's folder: heavy asks git for the
    repo of the folder it runs in, and a test leaves this checkout alone."""
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env | extra,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd or Path(env["HEAVY_DIR"]).parent,
    )


def wait_until(condition, what, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def lane(tmp_path, name="normal"):
    return tmp_path / "heavy" / name


def entries(tmp_path, name="normal"):
    folder = lane(tmp_path, name)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.name[:1].isdigit())


def held(tmp_path, name="normal"):
    return (lane(tmp_path, name) / "slot").exists()


def blocker(tmp_path, name):
    """A command that marks it started, then runs until its release file appears."""
    started, release = tmp_path / f"{name}.started", tmp_path / f"{name}.release"
    command = [
        "sh",
        "-c",
        (
            f'echo "start {name}" >> "{tmp_path}/log"; touch "{started}";'
            f' while [ ! -e "{release}" ]; do sleep 0.05; done;'
            f' echo "end {name}" >> "{tmp_path}/log"'
        ),
    ]
    return command, started, release


def finish(proc):
    out, err = proc.communicate(timeout=30)
    return proc.returncode, out, err


def test_it_runs_the_command_with_its_output_and_status(box):
    _, _, env = box

    done = heavy(env, "--", "sh", "-c", "echo out; echo err >&2; exit 3")

    assert done.returncode == 3
    assert done.stdout == "out\n"
    assert done.stderr == "err\n"


def test_a_command_that_doesnt_exist_exits_127(box):
    _, _, env = box

    done = heavy(env, "--", "no-such-command-anywhere")

    assert done.returncode == 127
    assert done.stderr.count("\n") == 1, done.stderr
    assert "no-such-command-anywhere" in done.stderr


def test_jobs_run_one_at_a_time_in_the_order_they_came(box, background):
    tmp_path, _, _ = box
    jobs = {name: blocker(tmp_path, name) for name in ("a", "b", "c")}

    a = background("--", *jobs["a"][0])
    wait_until(jobs["a"][1].exists, "a to start")
    b = background("--", *jobs["b"][0])
    wait_until(lambda: len(entries(tmp_path)) == 1, "b to queue")
    c = background("--", *jobs["c"][0])
    wait_until(lambda: len(entries(tmp_path)) == 2, "c to queue")
    for name in ("a", "b", "c"):
        jobs[name][2].touch()
        if name != "c":
            nxt = {"a": "b", "b": "c"}[name]
            wait_until(jobs[nxt][1].exists, f"{nxt} to start")

    for proc in (a, b, c):
        status, _, err = finish(proc)
        assert status == 0, err
    assert (tmp_path / "log").read_text().split("\n")[:-1] == [
        "start a",
        "end a",
        "start b",
        "end b",
        "start c",
        "end c",
    ]
    assert entries(tmp_path) == [] and not held(tmp_path)


def test_the_head_waits_for_its_cores_to_go_quiet(box, background):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")
    ran, log = tmp_path / "ran", tmp_path / "err"

    job = background("--", "touch", str(ran), stderr=log)
    wait_until(lambda: "waiting for quiet cores" in log.read_text(), "the busy reading")
    assert not ran.exists()
    set_busy(busy, "0.1")
    status, _, _ = finish(job)

    assert status == 0, log.read_text()
    assert ran.exists()


def test_a_heavy_inside_a_job_runs_at_once(box):
    tmp_path, _, env = box
    inner = tmp_path / "inner"

    done = heavy(env, "--", str(SCRIPT), "--", "touch", str(inner))

    assert done.returncode == 0, done.stderr
    assert inner.exists()


def test_a_slot_whose_process_is_gone_is_cleared(box):
    tmp_path, _, env = box
    gone = subprocess.Popen(["sleep", "60"])
    start = fifo.started(gone.pid)
    gone.kill()
    gone.wait()
    folder = lane(tmp_path)
    folder.mkdir(parents=True)
    (folder / "slot").write_text(
        f"s-gone 2026-11-07T01:02:03+00:00 host={os.uname().nodename}"
        f" pid={gone.pid} start={start}\ncwd=/x\ncmd=make\n"
    )

    done = heavy(env, "--", "true")

    assert done.returncode == 0, done.stderr
    assert not held(tmp_path)


def test_a_killed_waiter_doesnt_hold_up_the_queue(box, background):
    tmp_path, _, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    killed = background("--", "true", session="s-killed")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the waiter to queue")
    killed.kill()
    killed.communicate()
    after = background("--", "true", session="s-after")
    release.touch()

    for proc in (a, after):
        status, _, err = finish(proc)
        assert status == 0, err
    assert entries(tmp_path) == []


def test_stopping_a_running_job_stops_its_command_and_frees_the_slot(box, background):
    tmp_path, _, _ = box
    pid_file = tmp_path / "pid"

    job = background("--", "sh", "-c", f'echo $$ > "{pid_file}"; exec sleep 60')
    wait_until(lambda: pid_file.exists() and pid_file.read_text().strip(), "the job")
    child = int(pid_file.read_text())
    job.send_signal(signal.SIGTERM)
    status, _, _ = finish(job)

    assert status == 128 + signal.SIGTERM
    wait_until(lambda: not Path(f"/proc/{child}").exists(), "the command to stop")
    assert not held(tmp_path)


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_stopping_a_waiting_job_leaves_the_queue(box, background, stop):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")

    job = background("--", "true")
    wait_until(lambda: entries(tmp_path), "the job to queue")
    job.send_signal(stop)
    status, _, err = finish(job)

    assert status == 128 + stop
    assert "Traceback" not in err
    assert entries(tmp_path) == []


def affinity_of(tmp_path, name):
    """A command that writes its CPUs to a file, then runs until released."""
    out, release = tmp_path / f"{name}.cpus", tmp_path / f"{name}.release"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import os, pathlib, time\n"
        f"cpus = sorted(os.sched_getaffinity(0))\n"
        f"pathlib.Path({str(out)!r}).write_text(' '.join(map(str, cpus)))\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.05)\n"
    )
    return [sys.executable, str(script)], out, release


def test_a_long_job_gets_a_quarter_of_the_cores_and_a_normal_job_the_rest(
    box, background
):
    tmp_path, _, _ = box
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        pytest.skip("needs at least 4 CPUs to split")
    quarter = cpus[len(cpus) - len(cpus) // 4 :]
    rest = cpus[: len(cpus) - len(cpus) // 4]
    long_cmd, long_out, long_release = affinity_of(tmp_path, "long")
    normal_cmd, normal_out, normal_release = affinity_of(tmp_path, "normal")

    long_job = background("--long", "--", *long_cmd)
    wait_until(long_out.exists, "the long job to start")
    normal_job = background("--", *normal_cmd)
    wait_until(normal_out.exists, "the normal job to start beside it")
    long_release.touch()
    normal_release.touch()

    for proc in (long_job, normal_job):
        status, _, err = finish(proc)
        assert status == 0, err
    assert long_out.read_text().split() == [str(c) for c in quarter]
    assert normal_out.read_text().split() == [str(c) for c in rest]


def test_a_normal_job_alone_gets_every_core(box):
    tmp_path, _, env = box
    command, out, release = affinity_of(tmp_path, "solo")
    release.touch()

    done = heavy(env, "--", *command)

    assert done.returncode == 0, done.stderr
    assert out.read_text().split() == [str(c) for c in sorted(os.sched_getaffinity(0))]


def test_the_pr_hint_comes_with_the_third_job_not_the_second(box, background):
    tmp_path, _, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    second = background("--", "true", session="s-second")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the second to queue")
    third = background("--", "true", session="s-third")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the third to queue")
    release.touch()

    results = {
        name: finish(proc)
        for name, proc in (("a", a), ("second", second), ("third", third))
    }
    assert all(status == 0 for status, _, _ in results.values()), results
    assert "PR" not in results["second"][2]
    hint = [ln for ln in results["third"][2].splitlines() if "PR" in ln]
    assert len(hint) == 1 and "never main" in hint[0], results["third"][2]


def test_a_head_stalled_on_busy_cores_hints_at_a_pr_once(box, background):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")
    log = tmp_path / "err"

    job = background("--", "true", HEAVY_STALL_SECONDS="0.2", stderr=log)
    wait_until(lambda: "PR" in log.read_text(), "the stall's hint")
    # Some polls after the hint, which mustn't repeat it.
    wait_until(lambda: len(entries(tmp_path)) == 1, "the job still queued")
    set_busy(busy, "0.1")
    status, _, _ = finish(job)

    err = log.read_text()
    assert status == 0, err
    assert len([ln for ln in err.splitlines() if "PR" in ln]) == 1, err


def test_status_names_the_holder_the_waiters_and_how_busy_the_cores_are(
    box, background
):
    tmp_path, _, env = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command, session="s-holder")
    wait_until(started.exists, "a to start")
    waiter = background("--", "make", "check", session="s-waiter")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the waiter to queue")

    shown = heavy(env, "status")
    release.touch()
    for proc in (a, waiter):
        finish(proc)

    assert shown.returncode == 0, shown.stderr
    text = shown.stdout
    assert "s-holder" in text and "start a" in text
    assert "s-waiter" in text and "make check" in text
    assert "busy" in text
    assert len(text.splitlines()) <= 6, text


def test_busy_counts_the_named_cores_from_two_snapshots():
    before = "cpu  1 1 1 1\ncpu0 10 0 10 80 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\n"
    after = "cpu  1 1 1 1\ncpu0 40 0 20 90 0 0 0 0\ncpu1 0 0 0 200 0 0 0 0\n"

    # cpu0 spent 40 of 50 ticks busy; cpu1 none of its 100.
    assert cores.busy(before, after, [0]) == pytest.approx(0.8)
    assert cores.busy(before, after, [1]) == pytest.approx(0.0)
    assert cores.busy(before, after, [0, 1]) == pytest.approx(40 / 150)


@pytest.mark.parametrize("text", ["", "s-old 2026-11-07T01:02:03+00:00\ncmd=make\n"])
def test_a_slot_that_names_no_process_is_cleared(box, text):
    # Left empty by a full disk or a kill mid-write, or written by something else:
    # every heavy writes its process into the slot, so one without is dead.
    tmp_path, _, env = box
    folder = lane(tmp_path)
    folder.mkdir(parents=True)
    (folder / "slot").write_text(text)

    done = heavy(env, "--", "true")

    assert done.returncode == 0, done.stderr
    assert not held(tmp_path)


def test_a_job_that_arrives_to_a_free_quiet_lane_starts_at_once(background):
    # One sample, not the window: a job that waited 600 s can't end in time, however
    # slowly a busy machine starts heavy, so no time bound has to allow for load.
    job = background("--", "true", HEAVY_QUIET_SECONDS="600")
    if not wait_or_warn(lambda: job.poll() is not None, "heavy never ran the job", 60):
        return
    status, _, err = finish(job)

    assert status == 0, err


def test_a_job_that_waited_still_needs_the_quiet_window(box, background):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")
    log = tmp_path / "err"

    job = background("--", "true", HEAVY_QUIET_SECONDS="0.6", stderr=log)
    wait_until(lambda: "waiting for quiet cores" in log.read_text(), "the busy reading")
    quiet = time.monotonic()
    set_busy(busy, "0.1")
    status, _, _ = finish(job)

    assert status == 0, log.read_text()
    assert time.monotonic() - quiet >= 0.6


def test_a_command_that_reads_the_terminal_isnt_stopped(box):
    # In its own process group, CMD needs the terminal handed over, or a read stops
    # it (SIGTTIN) while it holds the slot.
    if not Path("/usr/bin/script").exists():
        pytest.skip("needs script(1) for a terminal")
    tmp_path, _, env = box
    out = tmp_path / "got"
    command = f"{SCRIPT} -- sh -c 'read line; echo \"$line\" > {out}'"

    done = subprocess.run(
        ["script", "-qec", command, "/dev/null"],
        input="hello\n",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert out.read_text() == "hello\n"


def test_a_second_signal_while_stopping_still_ends_the_command(box, background):
    tmp_path, _, _ = box
    pid_file = tmp_path / "pid"
    # A command that ignores TERM and INT, so only the KILL after the grace ends it.
    job = background(
        "--",
        "sh",
        "-c",
        f"trap '' TERM INT; echo $$ > \"{pid_file}\"; while :; do sleep 0.05; done",
        HEAVY_GRACE="0.5",
    )
    wait_until(lambda: pid_file.exists() and pid_file.read_text().strip(), "the job")
    child = int(pid_file.read_text())
    try:
        job.send_signal(signal.SIGTERM)
        time.sleep(0.1)  # inside the grace period, where a second signal used to escape
        job.send_signal(signal.SIGINT)
        wait_until(lambda: not Path(f"/proc/{child}").exists(), "the command to end")
    finally:
        # Should heavy fail to end it, the command would outlive the test, holding
        # its pipes and hanging the run.
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    finish(job)
    assert not held(tmp_path)


def test_a_long_and_a_normal_job_starting_together_never_share_cores(box, background):
    tmp_path, busy, _ = box
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        pytest.skip("needs at least 4 CPUs to split")
    set_busy(busy, "0.9")  # both wait as heads, then go quiet at the same moment
    long_cmd, long_out, long_release = affinity_of(tmp_path, "long")
    normal_cmd, normal_out, normal_release = affinity_of(tmp_path, "normal")
    long_log = tmp_path / "long.err"
    long_job = background("--long", "--", *long_cmd, stderr=long_log)
    normal_job = background("--", *normal_cmd)
    wait_until(
        lambda: entries(tmp_path, "long") and entries(tmp_path, "normal"), "both queued"
    )
    set_busy(busy, "0.1")
    # Either both start, on cores apart, or the normal job took every core first and
    # the long job waits for its quarter.
    wait_until(
        lambda: (
            (long_out.exists() and normal_out.exists())
            or (normal_out.exists() and "a normal job" in long_log.read_text())
        ),
        "both to start, or the long job to wait for its quarter",
    )
    both_ran = long_out.exists()
    if both_ran:
        long_cpus = set(long_out.read_text().split())
        normal_cpus = set(normal_out.read_text().split())
        assert not long_cpus & normal_cpus, (long_cpus, normal_cpus)
    normal_release.touch()
    wait_until(long_out.exists, "the long job to start")
    long_release.touch()
    for proc in (long_job, normal_job):
        status, _, err = finish(proc)
        assert status == 0, err


def test_a_long_job_on_too_few_cores_to_split_takes_the_normal_lane(box):
    tmp_path, _, env = box
    seen = tmp_path / "lanes"

    done = subprocess.run(
        [
            "taskset",
            "-c",
            "0",
            str(SCRIPT),
            "--long",
            "--",
            "sh",
            "-c",
            f'ls "{tmp_path}/heavy/normal" > "{seen}"',
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "slot" in seen.read_text().split()


@pytest.mark.parametrize("target", ["/etc", "not-executable"])
def test_a_command_that_cant_be_run_exits_126(box, target):
    tmp_path, _, env = box
    if target == "not-executable":
        target = str(tmp_path / "script.sh")
        Path(target).write_text("#!/bin/sh\necho hi\n")

    done = heavy(env, "--", target)

    assert done.returncode == 126
    assert done.stderr.count("\n") == 1, done.stderr
    assert target in done.stderr


def test_status_shows_how_long_jobs_ran_and_waited_and_the_holders_cores(
    box, background
):
    tmp_path, _, env = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command, session="s-holder")
    wait_until(started.exists, "a to start")
    waiter = background("--", "true", session="s-waiter")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the waiter to queue")

    shown = heavy(env, "status")
    release.touch()
    for proc in (a, waiter):
        finish(proc)

    cpus = sorted(os.sched_getaffinity(0))
    text = shown.stdout
    assert "running for 0m" in text and "waiting for 0m" in text, text
    assert f"on cores {cpus[0]}-{cpus[-1]}" in text, text


def job_report(tmp_path, name):
    """A command that writes its nice, oom_score_adj, CARGO_BUILD_JOBS and cores to a
    file, then runs until its release file appears."""
    out, release = tmp_path / f"{name}.report", tmp_path / f"{name}.release"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, os, pathlib, time\n"
        "report = {\n"
        "    'nice': os.nice(0),\n"
        "    'oom': int(pathlib.Path('/proc/self/oom_score_adj').read_text()),\n"
        "    'jobs': os.environ.get('CARGO_BUILD_JOBS'),\n"
        "    'cores': sorted(os.sched_getaffinity(0)),\n"
        "}\n"
        f"pathlib.Path({str(out)!r}).write_text(json.dumps(report))\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.05)\n"
    )
    return [sys.executable, str(script)], out, release


def report(out):
    return json.loads(out.read_text())


def learn(env, job, cores, cwd=None):
    """Run a job_report job once, released, so its history says it kept `cores` busy."""
    command, out, release = job
    release.touch()
    done = heavy(env, "--", *command, HEAVY_CORES_USED=str(cores), cwd=cwd)
    assert done.returncode == 0, done.stderr
    release.unlink()
    out.unlink()


def has_text(path, text):
    return lambda: path.exists() and text in path.read_text()


def usual_cores(status_text, needle):
    """The usual cores `heavy status` shows on the line naming needle, or None."""
    line = next(ln for ln in status_text.splitlines() if needle in ln)
    found = re.search(r"usually ([\d.]+) cores", line)
    return float(found.group(1)) if found else None


CPUS = sorted(os.sched_getaffinity(0))
BASE_NICE = os.nice(0)


def test_a_waiter_whose_history_fits_shares_the_lane_at_nice_10(
    box, background, sharing
):
    tmp_path, _, env = box
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    learn(env, holder, 1)
    learn(env, sharer, 1)

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background(
        "--share", "--", *sharer[0], HEAVY_SHARE_SECONDS="600"
    )  # history alone
    wait_until(sharer[1].exists, "the waiter to share the lane")
    shown = heavy(env, "status").stdout
    holder[2].touch()
    sharer[2].touch()
    for proc in (h, s):
        status, _, err = finish(proc)
        assert status == 0, err

    held, shared = report(holder[1]), report(sharer[1])
    assert held["nice"] == BASE_NICE and held["jobs"] == str(len(CPUS))
    assert shared["nice"] == max(BASE_NICE, 10)
    assert shared["oom"] == 1000
    assert shared["cores"] == CPUS
    # 75% of the cores, less the holder's usual core (its cores are idle).
    assert shared["jobs"] == str(max(1, math.floor(0.75 * len(CPUS) - 1)))
    assert "sharing at nice 10" in shown, shown


def test_a_waiter_whose_history_doesnt_fit_waits_for_the_holder(
    box, background, sharing
):
    tmp_path, _, env = box
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    learn(env, holder, 1)
    learn(env, waiter, 0.9 * len(CPUS))
    log = tmp_path / "waiter.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], HEAVY_SHARE_SECONDS="600", stderr=log)
    wait_until(has_text(log, "don't fit"), "the waiter to say it doesn't fit")
    assert not waiter[1].exists()
    holder[2].touch()
    wait_until(waiter[1].exists, "the waiter to start once the holder ends")
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err

    assert report(waiter[1])["nice"] == BASE_NICE  # it ran as the holder


def test_a_waiter_with_no_history_shares_once_the_holder_has_idled(
    box, background, sharing
):
    tmp_path, busy, _ = box
    set_busy(busy, "0.1")
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0])
    wait_until(sharer[1].exists, "the waiter to share the lane")
    holder[2].touch()
    sharer[2].touch()
    for proc in (h, s):
        status, _, err = finish(proc)
        assert status == 0, err

    shared = report(sharer[1])
    assert shared["nice"] == max(BASE_NICE, 10)
    # 75% of the cores, less the holder's load: 10% of them.
    assert shared["jobs"] == str(max(1, math.floor(0.75 * len(CPUS) - 0.1 * len(CPUS))))


def test_a_busy_holder_keeps_a_waiter_with_no_history_waiting(box, background, sharing):
    tmp_path, busy, _ = box
    set_busy(busy, "0.4")
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    log = tmp_path / "waiter.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], stderr=log)
    wait_until(has_text(log, "under 30%"), "the waiter to say what it waits for")
    assert not waiter[1].exists()
    holder[2].touch()
    finish(h)
    set_busy(busy, "0.1")
    wait_until(waiter[1].exists, "the waiter to start once the holder ends")
    waiter[2].touch()
    status, _, err = finish(w)

    assert status == 0, err
    assert report(waiter[1])["nice"] == BASE_NICE


def test_only_one_job_shares_a_lane_the_first_in_line(box, background, sharing):
    tmp_path, _, _ = box
    holder = job_report(tmp_path, "holder")
    first, second = job_report(tmp_path, "first"), job_report(tmp_path, "second")
    log = tmp_path / "second.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    one = background("--share", "--", *first[0])
    wait_until(lambda: len(entries(tmp_path)) == 1, "the first waiter to queue")
    two = background("--share", "--", *second[0], stderr=log)
    wait_until(first[1].exists, "the first waiter to share")
    wait_until(has_text(log, "already shares"), "the second to see the place taken")
    assert not second[1].exists()
    first[2].touch()
    wait_until(second[1].exists, "the second to share once the first ends")
    holder[2].touch()
    second[2].touch()
    for proc in (h, one, two):
        status, _, err = finish(proc)
        assert status == 0, err


def test_a_light_waiter_shares_ahead_of_a_heavy_one(box, background, sharing):
    tmp_path, _, env = box
    holder = job_report(tmp_path, "holder")
    big, light = job_report(tmp_path, "big"), job_report(tmp_path, "light")
    learn(env, holder, 1)
    learn(env, big, 0.9 * len(CPUS))
    learn(env, light, 1)
    history_only = {"HEAVY_SHARE_SECONDS": "600"}

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    b = background("--share", "--", *big[0], **history_only)
    wait_until(lambda: len(entries(tmp_path)) == 1, "the heavy waiter to queue")
    joined = time.monotonic()
    li = background("--share", "--", *light[0], **history_only)
    wait_until(light[1].exists, "the light waiter to share")
    # At once: it doesn't step aside for a waiter ahead that doesn't fit.
    assert time.monotonic() - joined < 4
    assert not big[1].exists()
    holder[2].touch()
    wait_until(big[1].exists, "the heavy waiter to take the holder's place")
    big[2].touch()
    light[2].touch()
    for proc in (h, b, li):
        status, _, err = finish(proc)
        assert status == 0, err

    assert report(big[1])["nice"] == BASE_NICE


@pytest.mark.parametrize("memory", ["short", "unreadable"])
def test_with_too_little_free_memory_nothing_shares(box, background, memory):
    tmp_path, _, env = box
    env["HEAVY_SHARE_SECONDS"] = "0.3"  # the holder idles, but memory is short
    if memory == "unreadable":
        env["HEAVY_MEMINFO"] = str(tmp_path / "no-such-meminfo")
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    log = tmp_path / "waiter.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], stderr=log)
    wait_until(has_text(log, "free memory"), "the waiter to say memory is short")
    assert not waiter[1].exists()
    holder[2].touch()
    wait_until(waiter[1].exists, "the waiter to start once the holder ends")
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err


def test_nothing_shares_beside_an_alone_holder(box, background, sharing):
    tmp_path, _, _ = box
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    log = tmp_path / "waiter.err"

    h = background("--alone", "--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], stderr=log)
    wait_until(has_text(log, "--alone"), "the waiter to say the holder runs alone")
    assert not waiter[1].exists()
    holder[2].touch()
    wait_until(waiter[1].exists, "the waiter to start once the holder ends")
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err


def test_an_alone_job_never_shares(box, background, sharing):
    tmp_path, _, _ = box
    holder = job_report(tmp_path, "holder")
    alone, other = job_report(tmp_path, "alone"), job_report(tmp_path, "other")

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    a = background("--alone", "--", *alone[0])
    wait_until(lambda: len(entries(tmp_path)) == 1, "the alone job to queue")
    o = background("--share", "--", *other[0])
    wait_until(other[1].exists, "the job behind it to share")  # the window passed
    assert not alone[1].exists()
    holder[2].touch()
    other[2].touch()
    wait_until(alone[1].exists, "the alone job to start once both end")
    alone[2].touch()
    for proc in (h, a, o):
        status, _, err = finish(proc)
        assert status == 0, err

    assert report(alone[1])["nice"] == BASE_NICE


def test_an_alone_head_waits_for_the_sharer_to_end(box, background, sharing):
    tmp_path, _, _ = box
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    alone = job_report(tmp_path, "alone")
    log = tmp_path / "alone.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0])
    wait_until(sharer[1].exists, "the waiter to share")
    a = background("--alone", "--", *alone[0], stderr=log)
    wait_until(lambda: len(entries(tmp_path)) == 1, "the alone job to queue")
    holder[2].touch()
    wait_until(has_text(log, "shares no cores"), "the alone job to wait for the sharer")
    assert not alone[1].exists()
    sharer[2].touch()
    wait_until(alone[1].exists, "the alone job to start once the sharer ends")
    alone[2].touch()
    for proc in (h, s, a):
        status, _, err = finish(proc)
        assert status == 0, err


def test_alone_inside_a_job_says_it_does_nothing(box):
    _, _, env = box

    done = heavy(env, "--", str(SCRIPT), "--alone", "--", "true")

    assert done.returncode == 0, done.stderr
    assert "--alone does nothing" in done.stderr


def test_while_a_long_job_waits_the_next_normal_job_leaves_it_its_quarter(
    box, background
):
    tmp_path, _, _ = box
    if len(CPUS) < 4:
        pytest.skip("needs at least 4 CPUs to split")
    quarter = CPUS[len(CPUS) - len(CPUS) // 4 :]
    first, second = affinity_of(tmp_path, "first"), affinity_of(tmp_path, "second")
    long_cmd, long_out, long_release = affinity_of(tmp_path, "long")

    one = background("--", *first[0])
    wait_until(first[1].exists, "the first normal job to start, on every core")
    # The long job is slow to see quiet cores, so the second normal job goes first.
    long_job = background("--long", "--", *long_cmd, HEAVY_QUIET_SECONDS="2")
    wait_until(lambda: entries(tmp_path, "long"), "the long job to queue")
    two = background("--", *second[0])
    wait_until(lambda: entries(tmp_path), "the second normal job to queue")
    first[2].touch()
    wait_until(second[1].exists, "the second normal job to start")
    wait_until(long_out.exists, "the long job to start beside it")
    second[2].touch()
    long_release.touch()
    for proc in (one, long_job, two):
        status, _, err = finish(proc)
        assert status == 0, err

    assert set(second[1].read_text().split()).isdisjoint(map(str, quarter))
    assert long_out.read_text().split() == [str(c) for c in quarter]


def test_a_long_job_waits_for_the_cores_a_normal_sharer_holds(box, background, sharing):
    tmp_path, _, _ = box
    if len(CPUS) < 4:
        pytest.skip("needs at least 4 CPUs to split")
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    long_cmd, long_out, long_release = affinity_of(tmp_path, "long")
    log = tmp_path / "long.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0])
    wait_until(sharer[1].exists, "the waiter to share, on every core")
    holder[2].touch()
    finish(h)
    long_job = background("--long", "--", *long_cmd, stderr=log)
    wait_until(has_text(log, "a normal job"), "the long job to wait for its cores")
    assert not long_out.exists()
    sharer[2].touch()
    wait_until(long_out.exists, "the long job to start once the sharer ends")
    long_release.touch()
    for proc in (s, long_job):
        status, _, err = finish(proc)
        assert status == 0, err


def test_status_shows_the_usual_cores_of_a_command_that_exited_0(box, background):
    tmp_path, _, env = box
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    learn(env, waiter, 2.5)

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--", *waiter[0], session="s-waiter")
    wait_until(lambda: entries(tmp_path), "the waiter to queue")
    shown = heavy(env, "status").stdout
    holder[2].touch()
    waiter[2].touch()
    for proc in (h, w):
        finish(proc)

    assert usual_cores(shown, "s-waiter") == 2.5, shown
    assert usual_cores(shown, "running for") is None, shown  # no history yet


def test_a_run_that_fails_leaves_no_history(box, background):
    tmp_path, _, env = box
    command, started, release = blocker(tmp_path, "a")
    passing = job_report(tmp_path, "passing")
    learn(env, passing, 2.5)
    flag = tmp_path / "pass"
    failing = ["sh", "-c", f'test -e "{flag}"']
    done = heavy(env, "--", *failing, HEAVY_CORES_USED="2.5")
    assert done.returncode == 1

    a = background("--", *command)
    wait_until(started.exists, "a to start")
    p = background("--", *passing[0], session="s-passing")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the passing one to queue")
    f = background("--", *failing, session="s-failing")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the failing one to queue")
    shown = heavy(env, "status").stdout
    flag.touch()
    passing[2].touch()
    release.touch()
    for proc in (a, p, f):
        finish(proc)

    assert usual_cores(shown, "s-passing") == 2.5, shown
    assert usual_cores(shown, "s-failing") is None, shown


def test_history_comes_from_the_commands_own_cpu_time(box, background):
    tmp_path, _, env = box
    burn = [
        sys.executable,
        "-c",
        "import time; end = time.time() + 0.5; any(iter(lambda: time.time() > end, 1))",
    ]
    rest = ["sleep", "0.5"]
    for command in (burn, rest):
        done = heavy(env, "--", *command)
        assert done.returncode == 0, done.stderr

    holder, started, release = blocker(tmp_path, "a")
    a = background("--", *holder)
    wait_until(started.exists, "a to start")
    b = background("--", *burn, session="s-burn")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the burner to queue")
    r = background("--", *rest, session="s-rest")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the sleeper to queue")
    shown = heavy(env, "status").stdout
    release.touch()
    for proc in (a, b, r):
        finish(proc)

    assert usual_cores(shown, "s-burn") >= 0.3, shown
    assert usual_cores(shown, "s-rest") <= 0.1, shown


def test_every_worktree_of_a_repo_shares_its_history(box, background):
    tmp_path, _, env = box
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", str(repo)], check=True)
    subprocess.run(
        [*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )
    subprocess.run(
        [*git, "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree)],
        check=True,
    )
    learn(env, job_report(tmp_path, "job"), 2.5, cwd=repo)

    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    w = background("--", *job_report(tmp_path, "job")[0], session="s-tree", cwd=tree)
    wait_until(lambda: entries(tmp_path), "the waiter to queue")
    shown = heavy(env, "status").stdout
    release.touch()
    (tmp_path / "job.release").touch()
    for proc in (a, w):
        finish(proc)

    assert usual_cores(shown, "s-tree") == 2.5, shown


@pytest.mark.parametrize("text", ["{not json", '{"version": 1, "commands": []}', "[]"])
def test_a_history_file_that_cant_be_read_counts_as_none(box, background, text):
    tmp_path, _, env = box
    (tmp_path / "heavy").mkdir()
    (tmp_path / "heavy" / "history.json").write_text(text)
    waiter = job_report(tmp_path, "waiter")
    learn(env, waiter, 2.5)  # the run passes, and is remembered in a file made anew

    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    w = background("--", *waiter[0], session="s-waiter")
    wait_until(lambda: entries(tmp_path), "the waiter to queue")
    shown = heavy(env, "status")
    release.touch()
    waiter[2].touch()
    for proc in (a, w):
        finish(proc)

    assert shown.returncode == 0, shown.stderr
    assert usual_cores(shown.stdout, "s-waiter") == 2.5, shown.stdout


def test_a_sharers_run_is_remembered_too(box, background, sharing):
    tmp_path, _, env = box
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0], HEAVY_CORES_USED="1.5")
    wait_until(sharer[1].exists, "the waiter to share")
    sharer[2].touch()
    finish(s)
    sharer[2].unlink()

    w = background(
        "--share", "--", *sharer[0], session="s-again", HEAVY_SHARE_SECONDS="600"
    )
    wait_until(lambda: entries(tmp_path), "the same command to queue again")
    shown = heavy(env, "status").stdout
    holder[2].touch()
    sharer[2].touch()
    for proc in (h, w):
        finish(proc)

    assert usual_cores(shown, "s-again") == 1.5, shown


def fake_sccache(tmp_path):
    """An env whose RUSTC_WRAPPER is an sccache that logs each start-server call's
    SCCACHE_IDLE_TIMEOUT and nice."""
    folder = tmp_path / "bin"
    folder.mkdir()
    log = tmp_path / "sccache.log"
    (folder / "sccache").write_text(
        "#!/bin/sh\n"
        f'echo "$1 timeout=${{SCCACHE_IDLE_TIMEOUT-unset}} nice=$(nice)" >> "{log}"\n'
    )
    (folder / "sccache").chmod(0o755)
    return log, {"RUSTC_WRAPPER": "sccache", "PATH": f"{folder}:{os.environ['PATH']}"}


def test_heavy_starts_sccache_so_it_never_idles_out(box):
    tmp_path, _, env = box
    log, extra = fake_sccache(tmp_path)

    done = heavy(env, "--", str(SCRIPT), "--", "true", **extra)  # a heavy inside a job

    assert done.returncode == 0, done.stderr
    starts = log.read_text().splitlines() if log.exists() else []
    # Once, by the outer heavy; none at all when the suite itself runs niced.
    assert starts == (["--start-server timeout=0 nice=0"] if BASE_NICE == 0 else [])


def test_a_niced_heavy_leaves_sccache_alone(box):
    tmp_path, _, env = box
    log, extra = fake_sccache(tmp_path)

    done = subprocess.run(
        ["nice", "-n", "5", str(SCRIPT), "--", "true"],
        env=env | extra,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert not log.exists()


def same_job(tmp_path):
    """One command for many jobs: each writes its report to JOB.report and runs until
    JOB.release appears, JOB from its environment, so all share one history."""
    script = tmp_path / "same.py"
    script.write_text(
        "import os, pathlib, time\n"
        f"folder = pathlib.Path({str(tmp_path)!r})\n"
        "name = os.environ['JOB']\n"
        "(folder / f'{name}.report').write_text(str(os.nice(0)))\n"
        "while not (folder / f'{name}.release').exists():\n"
        "    time.sleep(0.05)\n"
    )
    return [sys.executable, str(script)]


def test_the_waiter_ahead_shares_first_when_both_fit_at_once(box, background, sharing):
    tmp_path, _, env = box
    command = same_job(tmp_path)
    (tmp_path / "learn.release").touch()
    learned = heavy(env, "--", *command, JOB="learn", HEAVY_CORES_USED="1")
    assert learned.returncode == 0, learned.stderr
    history_only = {"HEAVY_SHARE_SECONDS": "600"}
    holder = background("--", *command, JOB="holder")
    wait_until((tmp_path / "holder.report").exists, "the holder to start")

    def round_of(n):
        """A sharer, then two waiters behind it; which of them shares once it ends."""
        report = {job: tmp_path / f"{job}{n}.report" for job in ("first", "second")}
        sharer = background("--share", "--", *command, JOB=f"sharer{n}", **history_only)
        wait_until((tmp_path / f"sharer{n}.report").exists, "a job to share")
        first = background("--share", "--", *command, JOB=f"first{n}", **history_only)
        wait_until(lambda: len(entries(tmp_path)) == 1, "the first waiter to queue")
        second = background("--share", "--", *command, JOB=f"second{n}", **history_only)
        wait_until(lambda: len(entries(tmp_path)) == 2, "the second waiter to queue")
        (tmp_path / f"sharer{n}.release").touch()
        wait_until(
            lambda: report["first"].exists() or report["second"].exists(),
            "a waiter to share",
        )
        went = "first" if report["first"].exists() else "second"
        for job in ("first", "second"):
            (tmp_path / f"{job}{n}.release").touch()
        for proc in (sharer, first, second):
            status, _, err = finish(proc)
            assert status == 0, err
        return went

    # Both waiters watch the lane anew when the sharer ends, so both fit at the same
    # moment, in whatever phase their polls are in; the one ahead must go first.
    went = [round_of(n) for n in range(6)]
    (tmp_path / "holder.release").touch()
    finish(holder)

    assert went == ["first"] * 6, went


def test_a_waiter_watches_the_holder_anew_once_the_sharer_ends(
    box, background, sharing
):
    tmp_path, busy, _ = box
    set_busy(busy, "0.1")
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    waiter = job_report(tmp_path, "waiter")
    log = tmp_path / "waiter.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0])
    wait_until(sharer[1].exists, "a job to share")
    w = background("--share", "--", *waiter[0], HEAVY_SHARE_SECONDS="1.5", stderr=log)
    wait_until(has_text(log, "already shares"), "the waiter to see the place taken")
    # The samples the waiter takes while the sharer runs mustn't count once it ends.
    # fixed sleep: long enough to gather a whole window of them, which heavy doesn't say
    time.sleep(1.6)
    sharer[2].touch()
    ended = time.monotonic()
    wait_until(waiter[1].exists, "the waiter to share")
    waited = time.monotonic() - ended
    holder[2].touch()
    waiter[2].touch()
    for proc in (h, s, w):
        status, _, err = finish(proc)
        assert status == 0, err

    assert waited >= 1.2, waited


def test_a_heavy_inside_a_sharer_runs_at_once(box, background, sharing):
    tmp_path, _, _ = box
    holder = job_report(tmp_path, "holder")
    inner = tmp_path / "inner"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", str(SCRIPT), "--", "touch", str(inner))
    wait_until(inner.exists, "the heavy inside the sharer to run")
    holder[2].touch()
    for proc in (h, s):
        status, _, err = finish(proc)
        assert status == 0, err


def test_a_holder_with_no_history_lets_nobody_in_by_history(box, background, sharing):
    tmp_path, _, env = box
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    learn(env, waiter, 1)  # it would fit beside a holder of 1 core
    log = tmp_path / "waiter.err"

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], HEAVY_SHARE_SECONDS="600", stderr=log)
    wait_until(
        has_text(log, "no passing run of the holder's command"),
        "the waiter to say the holder has no history",
    )
    # Some polls with a full history window, in which a waiter that took the
    # holder's load for 0 cores would share.
    time.sleep(0.5)  # fixed sleep: it must not share in them
    assert not waiter[1].exists()
    holder[2].touch()
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err


def test_free_memory_near_the_floor_says_so_once(box, background, sharing):
    tmp_path, _, _ = box
    holder, waiter = job_report(tmp_path, "holder"), job_report(tmp_path, "waiter")
    log = tmp_path / "waiter.err"
    meminfo(tmp_path / "meminfo", 1)

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    w = background("--share", "--", *waiter[0], stderr=log)
    wait_until(has_text(log, "free memory"), "the waiter to say memory is short")
    for _ in range(20):  # just under the floor, then just over, a poll or so each
        for gib in (3.9, 4.1):
            meminfo(tmp_path / "meminfo", gib)
            time.sleep(0.03)
    meminfo(tmp_path / "meminfo", 16)
    wait_until(waiter[1].exists, "the waiter to share once memory is plenty")
    holder[2].touch()
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err

    assert log.read_text().count("free memory") == 1, log.read_text()


def test_a_holder_whose_command_has_many_lines_can_still_be_shared(
    box, background, sharing
):
    tmp_path, _, _ = box
    started, release = tmp_path / "started", tmp_path / "release"
    holder = [
        "sh",
        "-c",
        f'touch "{started}"; while [ ! -e "{release}" ]; do sleep 0.05; done\nalone=1',
    ]
    waiter = job_report(tmp_path, "waiter")

    h = background("--", *holder)
    wait_until(started.exists, "the holder to start")
    w = background("--share", "--", *waiter[0])
    wait_until(waiter[1].exists, "the waiter to share")
    release.touch()
    waiter[2].touch()
    for proc in (h, w):
        status, _, err = finish(proc)
        assert status == 0, err


def test_a_job_that_removes_the_queue_still_exits_with_its_status(box):
    _, _, env = box

    # A run that exits 0 is remembered, in the queue's folder that is now gone.
    done = heavy(env, "--", "sh", "-c", 'rm -rf "$HEAVY_DIR"')

    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr, done.stderr


def test_history_keeps_what_another_heavy_wrote_into_it(box):
    tmp_path, _, env = box
    job = job_report(tmp_path, "job")
    learn(env, job, 1)
    path = tmp_path / "heavy" / "history.json"
    data = json.loads(path.read_text())
    data["elsewhere"] = "kept"
    (entry,) = data["commands"].values()
    entry["note"] = "kept"
    path.write_text(json.dumps(data))

    learn(env, job, 2)

    data = json.loads(path.read_text())
    (entry,) = data["commands"].values()
    assert data["elsewhere"] == "kept" and entry["note"] == "kept", data
    assert [cores for cores, _ in entry["runs"]] == [1, 2], data


def joined(entry):
    """When the waiter whose queue entry this is joined the queue."""
    return datetime.fromisoformat(fifo.Holder.parse(entry.read_text()).since)


def a_second_after(moment):
    """A condition: the clock has passed moment by a whole second, so what starts
    now carries a later time than moment does, at the seconds slots keep."""
    return lambda: datetime.now().astimezone() >= moment + timedelta(seconds=1)


def started_at(slot):
    return datetime.fromisoformat(fifo.Holder.parse(slot.read_text()).since)


def test_a_slot_says_when_its_job_started_not_when_it_joined(box, background):
    tmp_path, _, _ = box
    first, second = blocker(tmp_path, "a"), blocker(tmp_path, "b")
    a = background("--", *first[0])
    wait_until(first[1].exists, "a to start")
    b = background("--", *second[0])
    wait_until(lambda: entries(tmp_path), "b to queue")
    b_joined = joined(entries(tmp_path)[0])
    wait_until(a_second_after(b_joined), "a second to pass")
    first[2].touch()
    wait_until(second[1].exists, "b to start")
    b_started = started_at(lane(tmp_path) / "slot")
    second[2].touch()
    for proc in (a, b):
        finish(proc)

    assert b_started > b_joined, (b_joined, b_started)


def test_a_sharers_slot_says_when_it_started_sharing(box, background, sharing):
    tmp_path, _, _ = box
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    # The holder must idle for 1.5 s first, so the sharer starts a second after it
    # joined, at least.
    s = background("--share", "--", *sharer[0], HEAVY_SHARE_SECONDS="1.5")
    wait_until(lambda: entries(tmp_path), "the sharer to queue")
    s_joined = joined(entries(tmp_path)[0])
    wait_until(sharer[1].exists, "the waiter to share")
    s_started = started_at(lane(tmp_path) / "share")
    holder[2].touch()
    sharer[2].touch()
    for proc in (h, s):
        finish(proc)

    assert s_started > s_joined, (s_joined, s_started)


def failing(vmstat, ending, oom=True):
    """A command that ends as `ending` says (a shell line), first counting one more
    OOM kill in the stand-in vmstat when oom is set."""
    count = f'echo "oom_kill 1" > "{vmstat}"; ' if oom else ""
    return ["sh", "-c", count + ending]


@pytest.mark.parametrize(
    "ending, oom, status, told",
    [
        ("kill -9 $$", True, 128 + signal.SIGKILL, True),  # the command itself
        ("exit 101", True, 101, True),  # as cargo exits when a rustc is killed
        ("kill -9 $$", False, 128 + signal.SIGKILL, False),  # a person's SIGKILL
        ("exit 0", True, 0, False),  # another job's process was killed
    ],
)
def test_a_job_that_fails_while_the_oom_killer_acts_says_to_run_it_again(
    box, ending, oom, status, told
):
    tmp_path, _, env = box
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pgfault 10\noom_kill 0\n")

    done = heavy(env, "--", *failing(vmstat, ending, oom), HEAVY_VMSTAT=str(vmstat))

    assert done.returncode == status
    assert ("out-of-memory killer" in done.stderr) is told, done.stderr
    assert ("run it again" in done.stderr) is told, done.stderr


def test_a_heavy_inside_a_job_leaves_the_oom_line_to_the_job(box):
    tmp_path, _, env = box
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("oom_kill 0\n")
    inner = shlex.join([str(SCRIPT), "--", *failing(vmstat, "exit 1")])

    done = heavy(env, "--", "sh", "-c", inner, HEAVY_VMSTAT=str(vmstat))

    assert done.returncode == 1
    assert done.stderr.count("out-of-memory killer") == 1, done.stderr


def test_a_sharer_the_oom_killer_hit_hears_why_it_went_first(box, background, sharing):
    tmp_path, _, _ = box
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("oom_kill 0\n")
    holder = job_report(tmp_path, "holder")

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background(
        "--share", "--", *failing(vmstat, "exit 1"), HEAVY_VMSTAT=str(vmstat)
    )
    status, _, err = finish(s)
    holder[2].touch()
    finish(h)

    assert status == 1
    assert "A job sharing a lane" in err, err


def key_of(slot):
    """The command key heavy wrote into a slot."""
    lines = slot.read_text().splitlines()
    return next(line.removeprefix("key=") for line in lines if line.startswith("key="))


@pytest.fixture
def stuck(box):
    """Add a queue entry for a waiter that asked to share and runs the command known
    by a key, held by a live process that never takes its place: a stopped heavy,
    without the chance that one stops while it holds the queue's lock. It returns the
    process: killing it clears the entry. Its processes end with the test."""
    tmp_path, _, _ = box
    procs = []

    def add(key):
        proc = subprocess.Popen(["sleep", "120"])
        procs.append(proc)
        since = datetime.now().astimezone().isoformat(timespec="seconds")
        pid = str(proc.pid)
        holder = fifo.Holder(
            "s-stuck", since, socket.gethostname(), pid, fifo.started(pid)
        )
        about = f"cwd={tmp_path}\ncmd=stuck\nkey={key}\nshare=1\n"
        fifo.join(lane(tmp_path), holder, about)
        return proc

    yield add
    for proc in procs:
        proc.kill()
        proc.wait()


def test_a_stuck_waiter_ahead_holds_up_sharing_only_a_while(
    box, background, sharing, stuck
):
    tmp_path, _, env = box
    command = same_job(tmp_path)
    (tmp_path / "learn.release").touch()
    learned = heavy(env, "--", *command, JOB="learn", HEAVY_CORES_USED="1")
    assert learned.returncode == 0, learned.stderr
    history_only = {"HEAVY_SHARE_SECONDS": "600"}
    holder = background("--", *command, JOB="holder")
    wait_until((tmp_path / "holder.report").exists, "the holder to start")
    sharer = background("--share", "--", *command, JOB="sharer", **history_only)
    wait_until((tmp_path / "sharer.report").exists, "a job to share")
    stuck(key_of(lane(tmp_path) / "slot"))  # it fits, but never takes the place
    behind = background("--share", "--", *command, JOB="behind", **history_only)
    wait_until(lambda: len(entries(tmp_path)) == 2, "a waiter behind it to queue")

    (tmp_path / "sharer.release").touch()
    freed = time.monotonic()
    wait_until((tmp_path / "behind.report").exists, "the waiter behind to share")
    waited = time.monotonic() - freed
    for name in ("holder", "behind"):
        (tmp_path / f"{name}.release").touch()
    for proc in (holder, sharer, behind):
        status, _, err = finish(proc)
        assert status == 0, err

    assert waited >= 4, waited  # it stepped aside for the stuck one a while


def test_a_job_shares_only_when_it_asks_to(box, background, sharing):
    tmp_path, _, _ = box
    holder = job_report(tmp_path, "holder")
    plain, asking = job_report(tmp_path, "plain"), job_report(tmp_path, "asking")

    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    p = background("--", *plain[0])
    wait_until(lambda: len(entries(tmp_path)) == 1, "the plain job to queue")
    a = background("--share", "--", *asking[0])
    wait_until(asking[1].exists, "the job that asks to share to share")  # window past
    assert not plain[1].exists()
    holder[2].touch()
    asking[2].touch()
    wait_until(plain[1].exists, "the plain job to start once the holder ends")
    plain[2].touch()
    for proc in (h, p, a):
        status, _, err = finish(proc)
        assert status == 0, err

    assert report(plain[1])["nice"] == BASE_NICE


def test_share_and_alone_dont_go_together(box):
    _, _, env = box

    done = heavy(env, "--share", "--alone", "--", "true")

    assert done.returncode == 2
    assert "not allowed with" in done.stderr, done.stderr


def learned_same_job(tmp_path, env):
    """same_job's command, with a history of 1 core."""
    command = same_job(tmp_path)
    (tmp_path / "learn.release").touch()
    learned = heavy(env, "--", *command, JOB="learn", HEAVY_CORES_USED="1")
    assert learned.returncode == 0, learned.stderr
    return command


def test_a_waiter_that_never_shares_holds_up_no_sharer(box, background, sharing):
    tmp_path, _, env = box
    command = learned_same_job(tmp_path, env)
    history_only = {"HEAVY_SHARE_SECONDS": "600"}
    holder = background("--", *command, JOB="holder")
    wait_until((tmp_path / "holder.report").exists, "the holder to start")
    plain = background("--", *command, JOB="plain", **history_only)
    wait_until(lambda: len(entries(tmp_path)) == 1, "a plain waiter to queue")
    joined = time.monotonic()
    sharer = background("--share", "--", *command, JOB="sharer", **history_only)
    wait_until((tmp_path / "sharer.report").exists, "the waiter behind to share")
    waited = time.monotonic() - joined
    for name in ("holder", "plain", "sharer"):
        (tmp_path / f"{name}.release").touch()
    for proc in (holder, plain, sharer):
        status, _, err = finish(proc)
        assert status == 0, err

    assert waited < 4, waited  # no stepping aside for one that can't share


def test_behind_a_stuck_waiter_the_next_in_line_still_goes_first(
    box, background, sharing, stuck
):
    tmp_path, _, env = box
    command = learned_same_job(tmp_path, env)
    history_only = {"HEAVY_SHARE_SECONDS": "600"}
    holder = background("--", *command, JOB="holder")
    wait_until((tmp_path / "holder.report").exists, "the holder to start")
    sharer = background("--share", "--", *command, JOB="sharer", **history_only)
    wait_until((tmp_path / "sharer.report").exists, "a job to share")
    stuck_one = stuck(key_of(lane(tmp_path) / "slot"))
    # The second's window is longer, so the third sees the stuck one fit first, and
    # would pass it, and the second, before the second can take the place.
    second = background(
        "--share",
        "--",
        *command,
        JOB="second",
        HEAVY_QUIET_SECONDS="1",
        **history_only,
    )
    wait_until(lambda: len(entries(tmp_path)) == 2, "a second waiter to queue")
    third = background("--share", "--", *command, JOB="third", **history_only)
    wait_until(lambda: len(entries(tmp_path)) == 3, "a third waiter to queue")

    (tmp_path / "sharer.release").touch()
    reports = [tmp_path / f"{name}.report" for name in ("second", "third")]
    wait_until(lambda: any(r.exists() for r in reports), "a waiter behind to share")
    went = "second" if reports[0].exists() else "third"
    stuck_one.kill()  # or, heading the line once the holder ends, it holds up the rest
    for name in ("holder", "second", "third"):
        (tmp_path / f"{name}.release").touch()
    for proc in (holder, sharer, second, third):
        status, _, err = finish(proc)
        assert status == 0, err

    assert went == "second"


def test_a_waiter_whose_fit_flickers_still_passes_a_stuck_one(
    box, background, sharing, stuck
):
    tmp_path, busy, env = box
    command = learned_same_job(tmp_path, env)
    history_only = {"HEAVY_SHARE_SECONDS": "600"}
    holder = background("--", *command, JOB="holder")
    wait_until((tmp_path / "holder.report").exists, "the holder to start")
    sharer = background("--share", "--", *command, JOB="sharer", **history_only)
    wait_until((tmp_path / "sharer.report").exists, "a job to share")
    stuck(key_of(lane(tmp_path) / "slot"))
    # A one-sample window, so the waiter fits at a quiet sample and not at a busy one.
    behind = background(
        "--share",
        "--",
        *command,
        JOB="behind",
        HEAVY_QUIET_SECONDS="0.05",
        **history_only,
    )
    wait_until(lambda: len(entries(tmp_path)) == 2, "a waiter behind it to queue")

    (tmp_path / "sharer.release").touch()
    report = tmp_path / "behind.report"
    deadline = time.monotonic() + 15
    while not report.exists() and time.monotonic() < deadline:
        for share in ("0.0", "0.95"):
            set_busy(busy, share)
            time.sleep(0.06)
    set_busy(busy, "0.0")
    shared = report.exists()
    for name in ("holder", "behind"):
        (tmp_path / f"{name}.release").touch()
    for proc in (holder, sharer, behind):
        finish(proc)

    assert shared, "the waiter behind never passed the stuck one"


def env_report(tmp_path, name):
    """A command that writes its HEAVY_SLOT (or `unset`), CARGO_BUILD_JOBS, cores and
    oom_score_adj to a file, then exits."""
    out = tmp_path / f"{name}.report"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, os, pathlib\n"
        "report = {\n"
        "    'slot': os.environ.get('HEAVY_SLOT', 'unset'),\n"
        "    'jobs': os.environ.get('CARGO_BUILD_JOBS'),\n"
        "    'cores': sorted(os.sched_getaffinity(0)),\n"
        "    'oom': int(pathlib.Path('/proc/self/oom_score_adj').read_text()),\n"
        "}\n"
        f"pathlib.Path({str(out)!r}).write_text(json.dumps(report))\n"
    )
    return [sys.executable, str(script)], out


@pytest.fixture
def memory(box):
    """Memory enough for a --quick job to run outside the queue: 16 GiB free."""
    tmp_path, _, _ = box
    meminfo(tmp_path / "meminfo", 16)


@pytest.mark.parametrize(
    ("share", "extra"), [("0.2", {}), ("0.4", {"HEAVY_QUICK_BUSY": "0.5"})]
)
def test_a_quick_job_on_quiet_cores_runs_at_once_beside_the_holder(
    box, background, memory, share, extra
):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    holder = (lane(tmp_path) / "slot").read_text()
    set_busy(busy, share)
    quick, out = env_report(tmp_path, "quick")

    q = background("--quick", "--", *quick, **extra)
    ran = wait_or_warn(lambda: q.poll() is not None, "the quick job beside the holder")
    still = a.poll() is None and (lane(tmp_path) / "slot").read_text() == holder
    release.touch()
    finish(a)
    if not ran:
        return
    status, _, err = finish(q)

    assert status == 0 and err == "", err
    assert still, "the holder kept its slot"
    assert entries(tmp_path) == []
    ran = report(out)
    assert (ran["jobs"], ran["cores"], ran["oom"]) == (str(len(CPUS)), CPUS, 1000)


def test_a_heavy_inside_a_quick_run_outside_the_queue_runs_at_once(
    box, background, memory
):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.1")
    inner = tmp_path / "inner"

    q = background("--quick", "--", str(SCRIPT), "--", "touch", str(inner))
    ran = wait_or_warn(lambda: q.poll() is not None, "the inner heavy to run")
    release.touch()
    finish(a)
    if not ran:
        return
    status, _, err = finish(q)

    assert status == 0, err
    assert inner.exists()


def test_with_too_little_free_memory_a_quick_job_waits_at_the_front(box, background):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.1")
    log = tmp_path / "err"

    q = background("--quick", "--", "true", stderr=log)
    wait_until(lambda: len(entries(tmp_path)) == 1, "the quick job to queue")
    meminfo(tmp_path / "meminfo", 16)
    ran = wait_or_warn(lambda: q.poll() is not None, "the quick job to run")
    holding = a.poll() is None
    release.touch()
    finish(a)
    if not ran:
        return
    status, _, _ = finish(q)

    assert status == 0, log.read_text()
    assert "memory" in log.read_text() and holding


def test_a_quick_job_on_busier_cores_goes_ahead_of_every_ordinary_waiter(
    box, background, memory
):
    tmp_path, busy, _ = box
    set_busy(busy, "0.4")  # quiet enough for a head to start, too busy for --quick
    log = tmp_path / "log"
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    b = background("--", "sh", "-c", f'echo b >> "{log}"', session="s-b")
    wait_until(lambda: len(entries(tmp_path)) == 1, "b to queue")
    q = background("--quick", "--", "sh", "-c", f'echo q >> "{log}"', session="s-q")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the quick job to queue")
    first = entries(tmp_path)[0].read_text()
    release.touch()

    for proc in (a, b, q):
        status, _, err = finish(proc)
        assert status == 0, err
    assert first.startswith("s-q "), first
    assert log.read_text().split() == ["start", "a", "end", "a", "q", "b"]


def test_a_quick_job_at_the_head_starts_at_the_first_quiet_sample(
    box, background, memory
):
    # A quick job that needed the 600 s window can't end in time, however slowly a
    # busy machine runs heavy, so no time bound has to allow for load.
    tmp_path, busy, _ = box
    set_busy(busy, "0.4")
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    quick, out = env_report(tmp_path, "quick")
    q = background("--quick", "--", *quick, HEAVY_QUIET_SECONDS="600")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the quick job to queue")
    release.touch()

    ran = wait_or_warn(lambda: q.poll() is not None, "the quick job to start", 60)
    finish(a)
    if not ran:
        return
    status, _, err = finish(q)

    assert status == 0, err
    assert report(out)["slot"].split()[0] == "s-test", "it took the slot"


def test_a_waiting_quick_job_runs_once_the_cores_go_quiet(box, background, memory):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.9")
    ran = tmp_path / "ran"
    q = background("--quick", "--", "touch", str(ran))
    wait_until(lambda: len(entries(tmp_path)) == 1, "the quick job to queue")
    set_busy(busy, "0.1")

    went = wait_or_warn(lambda: q.poll() is not None, "the quick job to run")
    holding = a.poll() is None and held(tmp_path)
    release.touch()
    finish(a)
    if not went:
        return
    status, _, err = finish(q)

    assert status == 0, err
    assert ran.exists() and holding
    assert entries(tmp_path) == []


def test_a_quick_job_watches_the_cores_for_its_window_first(box, memory):
    _, _, env = box
    begun = time.monotonic()

    done = heavy(env, "--quick", "--", "true", HEAVY_QUICK_SECONDS="0.6")

    assert done.returncode == 0 and done.stderr == "", done.stderr
    assert time.monotonic() - begun >= 0.6


def test_a_quick_job_that_runs_long_hears_once_at_its_end_to_drop_quick(
    box, background, memory
):
    tmp_path, _, _ = box
    started, release = tmp_path / "started", tmp_path / "release"
    command = (
        f'touch "{started}"; while [ ! -e "{release}" ]; do sleep 0.05; done;'
        " echo done >&2"
    )

    q = background("--quick", "--", "sh", "-c", command, HEAVY_QUICK_MAX="0.2")
    wait_until(started.exists, "the job to start")
    # fixed sleep: the job must outlast HEAVY_QUICK_MAX, which heavy doesn't say
    time.sleep(0.4)
    release.touch()
    status, _, err = finish(q)

    assert status == 0, err
    lines = err.splitlines()
    assert len(lines) == 2 and lines[0] == "done", err
    assert "past 0.2 s" in lines[1] and "--quick" in lines[1], err


@pytest.mark.parametrize("other", ["--long", "--alone", "--share"])
def test_quick_goes_with_no_other_option(box, other):
    _, _, env = box

    done = heavy(env, "--quick", other, "--", "true")

    assert done.returncode == 2
    assert f"--quick can't go with {other}" in done.stderr, done.stderr


def test_status_shows_a_quick_waiter_at_the_front(box, background):
    tmp_path, busy, env = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command, session="s-holder")
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.9")
    b = background("--", "make", "check", session="s-plain")
    wait_until(lambda: len(entries(tmp_path)) == 1, "b to queue")
    q = background("--quick", "--", "make", "one", session="s-quick")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the quick job to queue")

    shown = heavy(env, "status").stdout
    set_busy(busy, "0.1")
    release.touch()
    for proc in (a, b, q):
        finish(proc)

    waiting = [ln for ln in shown.splitlines() if "waiting for" in ln]
    assert len(waiting) == 2, shown
    assert waiting[0].lstrip().startswith("1.") and "s-quick" in waiting[0], shown
    assert "at the front" in waiting[0] and "--quick" in waiting[0], shown
    assert "at the front" not in waiting[1] and "s-plain" in waiting[1], shown


def test_a_quick_job_never_runs_beside_an_alone_holder(box, background, memory):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--alone", "--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.1")
    ran = tmp_path / "ran"

    q = background("--quick", "--", "touch", str(ran))
    wait_until(lambda: len(entries(tmp_path)) == 1, "the quick job to queue")
    early = ran.exists()
    release.touch()
    for proc in (a, q):
        status, _, err = finish(proc)
        assert status == 0, err

    assert not early and ran.exists()


def test_a_quick_job_starting_on_a_free_lane_says_nothing(box):
    # Short of memory, it can't run outside the queue, but nothing holds it up.
    _, _, env = box

    done = heavy(env, "--quick", "--", "true")

    assert done.returncode == 0 and done.stderr == "", done.stderr


def test_a_quick_job_never_hears_the_pr_hint(box, background, memory):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")
    log = tmp_path / "err"

    q = background("--quick", "--", "true", HEAVY_STALL_SECONDS="0.2", stderr=log)
    wait_until(has_text(log, "waiting for quiet cores"), "the busy reading")
    # fixed sleep: past HEAVY_STALL_SECONDS, when a plain head hears the hint
    time.sleep(0.5)
    set_busy(busy, "0.1")
    status, _, _ = finish(q)

    assert status == 0, log.read_text()
    assert "PR" not in log.read_text()


def test_a_quick_heavy_inside_a_job_runs_at_once(box, background):
    tmp_path, busy, _ = box
    set_busy(busy, "0.4")  # quiet enough for the job, too busy for --quick
    inner = tmp_path / "inner"

    job = background("--", str(SCRIPT), "--quick", "--", "touch", str(inner))
    if not wait_or_warn(lambda: job.poll() is not None, "the inner heavy to run"):
        return
    status, _, err = finish(job)

    assert status == 0, err
    assert inner.exists()
