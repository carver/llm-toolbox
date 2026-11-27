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
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import wait_or_warn

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "bin" / "heavy"
sys.path.insert(0, str(ROOT / "lib"))
import cores
import fifo
import heavyjobs
import heavyqueue
import machinespeed


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
    """Room to share: 16 GiB free, and a holder idle for 0.3 s lets a waiter in.
    Below 4 cores no sharer fits by history (a holder's core and a sharer's are more
    than SHARE_FIT of 2), and the tests' own workers keep the cores too busy to fit
    by idleness, as on a 2-core CI runner."""
    if len(os.sched_getaffinity(0)) < 4:
        pytest.skip("needs at least 4 CPUs to share")
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


@pytest.mark.parametrize(
    ("cpus", "text"),
    [
        ([0, 1, 2, 3], "0-3"),
        ([0, 1], "0-1"),
        ([0, 2, 3], "0,2,3"),
        ([1, 5], "1,5"),
        ([5], "5"),
    ],
)
def test_span_writes_cores_that_unspan_reads_back(cpus, text):
    # A holder's cores= field goes through both; a set that isn't one run must
    # survive too, or a holder on it would look like it holds no cores.
    assert cores.span(cpus) == text
    assert heavyqueue.unspan(cores.span(cpus)) == cpus


def test_load_is_busy_cores_less_the_hosts_steal_and_the_measurer():
    before = "cpu  1 1 1 1\ncpu0 0 0 0 0 0 0 0 0\ncpu1 0 0 0 0 0 0 0 0\n"
    before += "cpu2 0 0 0 0 0 0 0 0\n"
    # 100 ticks a core: cpu0 all busy, 20 of them stolen; cpu1 half busy, 10 of it
    # iowait, which is idle; cpu2 idle.
    after = "cpu  1 1 1 1\ncpu0 60 0 20 0 0 0 0 20\ncpu1 40 0 0 50 10 0 0 0\n"
    after += "cpu2 0 0 0 100 0 0 0 0\n"

    busy_cores, shares = cores.load(before, after, own_ticks=10)

    # 80 + 40 busy ticks less 10 of the measurer's, over 100 ticks a core.
    assert busy_cores == pytest.approx(1.1)
    assert shares == pytest.approx({0: 0.8, 1: 0.4, 2: 0.0})


def test_load_with_no_time_passed_is_none():
    text = "cpu  1 1 1 1\ncpu0 10 0 0 90 0 0 0 0\n"
    assert cores.load(text, text) is None


def test_a_sample_is_reused_only_while_it_covers_the_last_moment(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  1 1 1 1\ncpu0 0 0 0 0 0 0 0 0\n")

    taken = cores.sample(str(stat), 0.05)

    assert (taken.before, taken.seconds >= 0.05) == (stat.read_text(), True)
    assert cores.recent(str(stat), 0.05) is taken
    assert cores.recent(str(tmp_path / "other"), 0.05) is None  # another file's
    assert cores.recent(str(stat), 1) is None  # too short for a second's window
    wait_until(lambda: cores.recent(str(stat), 0.05) is None, "the sample to age")


def test_a_probe_takes_the_quietest_vcpu_from_heavys_own_sample(monkeypatch):
    # Two seconds at 100 ticks a core: cpu0 fully busy, cpu1 half, cpu2 a quarter;
    # cpu3, outside the job's cores, idle.
    before = "cpu  1 1 1 1\n" + "".join(f"cpu{n} 0 0 0 0 0 0 0 0\n" for n in range(4))
    after = "cpu  1 1 1 1\ncpu0 200 0 0 0 0 0 0 0\ncpu1 100 0 0 100 0 0 0 0\n"
    after += "cpu2 50 0 0 150 0 0 0 0\ncpu3 0 0 0 200 0 0 0 0\n"
    monkeypatch.setenv("HEAVY_STAT", "/heavy/stat")  # never read: the sample is reused
    monkeypatch.setenv("HEAVY_POLL", "2")
    monkeypatch.setattr(
        cores,
        "newest",
        cores.Sample("/heavy/stat", before, after, 2.0, 0.0, time.monotonic()),
    )

    assert machinespeed.moment([0, 1, 2]) == (2, pytest.approx(1.75))


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
    if shutil.which("script") is None:
        pytest.skip("needs script(1) for a terminal")
    if os.environ.get("OWN_PIDS") and not script_feeds_its_command():
        pytest.skip(
            "inside own-pids here, script(1) doesn't hand its input to the command"
            " (sudo's use_pty), so no test can read from a terminal"
        )
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


def script_feeds_its_command():
    """Whether script(1) hands its input to the command it runs. Inside own-pids, on a
    machine whose sudo has `Defaults use_pty`, the command reads an empty line. A
    probe past 30 s counts as no."""
    try:
        done = subprocess.run(
            ["script", "-qec", "sh -c 'read line; echo \"got:$line\"'", "/dev/null"],
            input="hello\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return "got:hello" in done.stdout


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
    """A command that writes its HEAVY_SLOT (or `unset`), CARGO_BUILD_JOBS, cores,
    oom_score_adj and nice to a file, then exits."""
    out = tmp_path / f"{name}.report"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, os, pathlib\n"
        "report = {\n"
        "    'slot': os.environ.get('HEAVY_SLOT', 'unset'),\n"
        "    'jobs': os.environ.get('CARGO_BUILD_JOBS'),\n"
        "    'cores': sorted(os.sched_getaffinity(0)),\n"
        "    'oom': int(pathlib.Path('/proc/self/oom_score_adj').read_text()),\n"
        "    'nice': os.nice(0),\n"
        "}\n"
        f"pathlib.Path({str(out)!r}).write_text(json.dumps(report))\n"
    )
    return [sys.executable, str(script)], out


@pytest.fixture
def memory(box):
    """Memory enough for a --few-seconds job to run outside the queue: 16 GiB free."""
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

    q = background("--few-seconds", "--", *quick, **extra)
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
    # Niced as a sharer is, so the holder keeps the cores both want.
    assert ran["nice"] == max(BASE_NICE, 10)


def test_a_quick_job_outside_the_queue_on_a_free_lane_is_niced_too(box, memory):
    # A holder may take the lane while it runs, and only root can undo a nice.
    tmp_path, busy, env = box
    set_busy(busy, "0.1")
    quick, out = env_report(tmp_path, "quick")

    done = heavy(env, "--few-seconds", "--", *quick)

    assert done.returncode == 0, done.stderr
    ran = report(out)
    assert ran["slot"].startswith("outside "), "it ran outside the queue"
    assert ran["nice"] == max(BASE_NICE, 10)


def test_a_heavy_inside_a_quick_run_outside_the_queue_runs_at_once(
    box, background, memory
):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.1")
    inner = tmp_path / "inner"

    q = background("--few-seconds", "--", str(SCRIPT), "--", "touch", str(inner))
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

    q = background("--few-seconds", "--", "true", stderr=log)
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
    # Quiet enough for a head to start, too busy for --few-seconds.
    set_busy(busy, "0.4")
    log = tmp_path / "log"
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    b = background("--", "sh", "-c", f'echo b >> "{log}"', session="s-b")
    wait_until(lambda: len(entries(tmp_path)) == 1, "b to queue")
    q = background(
        "--few-seconds", "--", "sh", "-c", f'echo q >> "{log}"', session="s-q"
    )
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
    q = background("--few-seconds", "--", *quick, HEAVY_QUIET_SECONDS="600")
    wait_until(lambda: len(entries(tmp_path)) == 1, "the quick job to queue")
    release.touch()

    ran = wait_or_warn(lambda: q.poll() is not None, "the quick job to start", 60)
    finish(a)
    if not ran:
        return
    status, _, err = finish(q)

    assert status == 0, err
    ran = report(out)
    assert ran["slot"].split()[0] == "s-test", "it took the slot"
    assert ran["nice"] == BASE_NICE, "a holder runs at full priority"


def test_a_waiting_quick_job_runs_once_the_cores_go_quiet(box, background, memory):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.9")
    ran = tmp_path / "ran"
    q = background("--few-seconds", "--", "touch", str(ran))
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

    done = heavy(env, "--few-seconds", "--", "true", HEAVY_QUICK_SECONDS="0.6")

    assert done.returncode == 0 and done.stderr == "", done.stderr
    assert time.monotonic() - begun >= 0.6


@pytest.mark.parametrize("flag", ["--quick", "--few"])
def test_the_old_name_and_a_prefix_still_run_a_few_seconds_job(box, memory, flag):
    _, _, env = box
    begun = time.monotonic()

    done = heavy(env, flag, "--", "true", HEAVY_QUICK_SECONDS="0.6")

    assert done.returncode == 0 and done.stderr == "", done.stderr
    assert time.monotonic() - begun >= 0.6


def test_help_names_few_seconds_and_not_the_old_quick(box):
    # The name says how long such a job may run, as the agent types it.
    _, _, env = box

    done = heavy(env, "-h")

    assert done.returncode == 0
    assert "--few-seconds" in done.stdout and "--quick" not in done.stdout


def alive(pid):
    """Whether pid runs: a zombie, ended but not yet reaped, doesn't."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


@pytest.mark.parametrize("ignores_term", [False, True], ids=["term", "kill"])
def test_a_few_seconds_job_past_its_limit_is_stopped_with_its_group(
    box, memory, ignores_term
):
    """2026-11-26: agents ran 61 to 214 s jobs as --few-seconds, and heavy only warned
    as each ended. Past HEAVY_QUICK_MAX, CMD's whole group gets SIGTERM, then SIGKILL
    after HEAVY_GRACE."""
    tmp_path, _, env = box
    pid_file = tmp_path / "grandchild"
    trap = "trap '' TERM; " if ignores_term else ""
    command = f'{trap}sleep 600 & echo $! > "{pid_file}"; wait; wait'
    begun = time.monotonic()

    done = heavy(
        env,
        "--few-seconds",
        "--",
        "sh",
        "-c",
        command,
        HEAVY_QUICK_MAX="0.5",
        HEAVY_GRACE="0.3",
    )

    took = time.monotonic() - begun
    assert done.returncode == 124, done.stderr
    assert took < 30, took
    grandchild = int(pid_file.read_text())
    wait_until(lambda: not alive(grandchild), "the job's grandchild to be stopped", 10)
    (line,) = [ln for ln in done.stderr.splitlines() if ln.startswith("heavy:")]
    assert "stopped this --few-seconds job at its 0.5 s limit" in line, line
    assert "bgrun -o LOG -- heavy -- CMD" in line, line
    (job,) = ended(tmp_path)
    assert job["exit"] == 124 and job["stopped"] == "limit", job
    shown = heavy(env, "history").stdout
    assert "stopped at the --few-seconds limit" in shown, shown


def test_a_few_seconds_job_within_its_limit_runs_to_its_end(box, memory):
    tmp_path, _, env = box

    done = heavy(env, "--few-seconds", "--", "sh", "-c", "sleep 0.2; exit 124")

    assert done.returncode == 124, done.stderr
    assert "limit" not in done.stderr, done.stderr
    (job,) = ended(tmp_path)
    assert job["exit"] == 124 and "stopped" not in job, job


def test_a_few_seconds_heavy_inside_a_job_has_no_limit(box):
    """Inside a job, such as wt-ship's landing check, a --few-seconds heavy runs as
    part of it: stopping it would cut the landing's check short."""
    _, _, env = box
    inner = [str(SCRIPT), "--few-seconds", "--", "sh", "-c", "sleep 0.5; exit 3"]

    done = heavy(env, "--", *inner, HEAVY_QUICK_MAX="0.1", HEAVY_GRACE="0.1")

    assert done.returncode == 3, done.stderr
    assert "limit" not in done.stderr, done.stderr


@pytest.mark.parametrize("setting", ["61", "0", "-5", "soon", "nan"])
def test_a_few_seconds_limit_over_60_s_or_unreadable_is_a_usage_error(box, setting):
    _, _, env = box

    done = heavy(env, "--few-seconds", "--", "true", HEAVY_QUICK_MAX=setting)

    assert done.returncode == 2, done.stderr
    assert "HEAVY_QUICK_MAX" in done.stderr, done.stderr


def test_the_limit_is_only_for_few_seconds_jobs(box):
    _, _, env = box

    done = heavy(env, "--", "sh", "-c", "sleep 0.3", HEAVY_QUICK_MAX="0.1")

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("other", ["--long", "--alone", "--share"])
def test_quick_goes_with_no_other_option(box, other):
    _, _, env = box

    done = heavy(env, "--few-seconds", other, "--", "true")

    assert done.returncode == 2
    assert f"--few-seconds can't go with {other}" in done.stderr, done.stderr


def test_status_shows_a_quick_waiter_at_the_front(box, background):
    tmp_path, busy, env = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command, session="s-holder")
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.9")
    b = background("--", "make", "check", session="s-plain")
    wait_until(lambda: len(entries(tmp_path)) == 1, "b to queue")
    q = background("--few-seconds", "--", "make", "one", session="s-quick")
    wait_until(lambda: len(entries(tmp_path)) == 2, "the quick job to queue")

    shown = heavy(env, "status").stdout
    set_busy(busy, "0.1")
    release.touch()
    for proc in (a, b, q):
        finish(proc)

    waiting = [ln for ln in shown.splitlines() if "waiting for" in ln]
    assert len(waiting) == 2, shown
    assert waiting[0].lstrip().startswith("1.") and "s-quick" in waiting[0], shown
    assert "at the front" in waiting[0] and "--few-seconds" in waiting[0], shown
    assert "at the front" not in waiting[1] and "s-plain" in waiting[1], shown


def test_a_quick_job_never_runs_beside_an_alone_holder(box, background, memory):
    tmp_path, busy, _ = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--alone", "--", *command)
    wait_until(started.exists, "a to start")
    set_busy(busy, "0.1")
    ran = tmp_path / "ran"

    q = background("--few-seconds", "--", "touch", str(ran))
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

    done = heavy(env, "--few-seconds", "--", "true")

    assert done.returncode == 0 and done.stderr == "", done.stderr


def test_a_quick_job_never_hears_the_pr_hint(box, background, memory):
    tmp_path, busy, _ = box
    set_busy(busy, "0.9")
    log = tmp_path / "err"

    q = background("--few-seconds", "--", "true", HEAVY_STALL_SECONDS="0.2", stderr=log)
    wait_until(has_text(log, "waiting for quiet cores"), "the busy reading")
    # fixed sleep: past HEAVY_STALL_SECONDS, when a plain head hears the hint
    time.sleep(0.5)
    set_busy(busy, "0.1")
    status, _, _ = finish(q)

    assert status == 0, log.read_text()
    assert "PR" not in log.read_text()


def test_a_quick_heavy_inside_a_job_runs_at_once(box, background):
    tmp_path, busy, _ = box
    set_busy(busy, "0.4")  # quiet enough for the job, too busy for --few-seconds
    inner = tmp_path / "inner"

    job = background("--", str(SCRIPT), "--few-seconds", "--", "touch", str(inner))
    if not wait_or_warn(lambda: job.poll() is not None, "the inner heavy to run"):
        return
    status, _, err = finish(job)

    assert status == 0, err
    assert inner.exists()


def test_an_empty_entry_a_crash_left_holds_no_job_up(box):
    """After a VM crash the queue held a waiter's entry of 0 bytes, and every job
    waited behind it with the cores idle."""
    tmp_path, _, env = box
    folder = lane(tmp_path)
    folder.mkdir(parents=True)
    leftover = folder / f"{1:020d}-1795595"
    leftover.write_text("")

    done = heavy(env, "--", "true")

    assert done.returncode == 0, done.stderr
    assert not leftover.exists()


def proc_stat(busy_on_first):
    """A /proc/stat whose first core of this process's has `busy_on_first` user ticks
    and every core 1000 idle ones."""
    cpus = sorted(os.sched_getaffinity(0))
    lines = ["cpu  0 0 0 0 0 0 0 0"]
    for cpu in cpus:
        user = busy_on_first if cpu == cpus[0] else 0
        lines.append(f"cpu{cpu} {user} 0 0 1000 0 0 0 0")
    return "\n".join(lines) + "\n"


@pytest.fixture
def outside(box):
    """An --alone run whose CMD moves HEAVY_STAT from a quiet snapshot to `after`, so
    heavy sees that much busy time on the job's cores while it ran, none of it the
    job's own (HEAVY_CORES_USED=0)."""
    tmp_path, _, env = box
    stat, after, report = (
        tmp_path / "stat",
        tmp_path / "stat.after",
        tmp_path / "report",
    )
    sccache = tmp_path / "sccache"

    def run(busy_ticks, *flags, **extra):
        """extra may hold HEAVY_SCCACHE_AFTER, the sccache server's CPU-seconds once
        CMD has run (0 before), HEAVY_ALONE_SLEEP, seconds CMD sleeps, and
        HEAVY_ALONE_EXIT, its exit status: all three are read by CMD, not heavy."""
        stat.write_text(proc_stat(0))
        after.write_text(proc_stat(busy_ticks))
        sccache.write_text("0\n")
        script = (
            'cp "$1" "$HEAVY_STAT"; echo "${HEAVY_SCCACHE_AFTER:-0}" > "$HEAVY_SCCACHE_CPU";'
            ' sleep "${HEAVY_ALONE_SLEEP:-0}"; exit "${HEAVY_ALONE_EXIT:-0}"'
        )
        command = ["sh", "-c", script, "sh", str(after)]
        return heavy(
            env,
            *flags,
            "--",
            *command,
            HEAVY_STAT=str(stat),
            HEAVY_SCCACHE_CPU=str(sccache),
            **{"HEAVY_ALONE_REPORT": str(report), "HEAVY_CORES_USED": "0"} | extra,
        )

    run.report = report
    return run


def test_an_alone_job_says_how_much_outside_work_shared_its_cores(outside):
    done = outside(4200, "--alone")
    assert done.returncode == 0, done.stderr
    assert "other work used 42.0 CPU-seconds of its cores" in done.stderr
    assert "the timing may be off; run it again." in done.stderr
    seconds, own, other, *_ = outside.report.read_text().split()
    assert (float(own), float(other)) == (0.0, 42.0)
    assert float(seconds) > 0


def test_an_alone_job_on_quiet_cores_hears_no_warning(outside):
    done = outside(0, "--alone")
    assert "other work used 0.0 CPU-seconds of its cores" in done.stderr
    assert "may be off" not in done.stderr


def test_the_warning_waits_for_the_threshold(outside):
    done = outside(4200, "--alone", HEAVY_ALONE_OUTSIDE="1e9")
    assert "other work used 42.0 CPU-seconds" in done.stderr
    assert "may be off" not in done.stderr


def test_a_job_not_run_alone_says_nothing_of_outside_work(outside):
    done = outside(4200)
    assert done.returncode == 0, done.stderr
    assert "other work used" not in done.stderr
    assert not outside.report.exists()


def test_a_report_file_it_cannot_write_is_named_and_the_job_still_passes(
    outside, tmp_path
):
    done = outside(0, "--alone", HEAVY_ALONE_REPORT=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert f"can't write HEAVY_ALONE_REPORT {tmp_path}" in done.stderr


def test_the_jobs_own_cpu_is_not_counted_as_other_work(outside):
    # 1000 cores for a run of at least 0.2 s is 200 CPU-seconds, past the 42 busy.
    done = outside(4200, "--alone", HEAVY_CORES_USED="1000", HEAVY_ALONE_SLEEP="0.2")
    assert "other work used 0.0 CPU-seconds of its cores" in done.stderr


def test_a_failed_alone_job_still_reports_and_keeps_its_exit_status(outside):
    done = outside(4200, "--alone", HEAVY_ALONE_EXIT="3")
    assert done.returncode == 3
    assert "other work used 42.0 CPU-seconds" in done.stderr


def test_the_sccache_servers_cpu_counts_as_the_jobs_own(outside):
    done = outside(4200, "--alone", HEAVY_SCCACHE_AFTER="40")
    assert "other work used 2.0 CPU-seconds of its cores" in done.stderr
    assert "40.0 more went to the sccache server" in done.stderr


def test_a_measure_it_cannot_take_is_a_note_not_a_failure(outside):
    done = outside(4200, "--alone", HEAVY_ALONE_OUTSIDE="lots")
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr
    assert "can't tell what other work" in done.stderr


@pytest.fixture
def usual_speed(box):
    """An env whose probe (HEAVY_SPEED_PROBE) takes the file's 10 ms, and a speed file
    of 20 such probes on this job's cores: the machine's usual speed."""
    tmp_path, _, env = box
    probe = tmp_path / "probe"
    probe.write_text("0.01\n")
    env = env | {"HEAVY_SPEED_PROBE": str(probe)}
    cpus = sorted(os.sched_getaffinity(0))
    when = datetime.now(UTC).isoformat(timespec="seconds")
    line = f"{when}\t0.01\t0.01\t-\t{cores.span(cpus)}\t{cpus[0]}\t0.00\tfake\t-\n"
    path = machinespeed.speed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line * 20)
    return env, probe


def test_an_alone_job_names_the_machines_speed_at_its_start_and_end(
    usual_speed, tmp_path
):
    env, probe = usual_speed
    report = tmp_path / "report"

    done = heavy(
        env,
        "--alone",
        "--",
        "sh",
        "-c",
        'echo 0.02 > "$0"',
        str(probe),
        HEAVY_ALONE_REPORT=str(report),
    )

    assert done.returncode == 0, done.stderr
    assert (
        "The machine ran at 1.00x its usual speed at the start and 0.50x at the end."
        in done.stderr
    )
    assert "trust" not in done.stderr
    assert report.read_text().split()[3:] == ["1.00", "0.50"]


def test_an_alone_job_slow_at_its_start_and_end_is_no_timing(usual_speed):
    env, probe = usual_speed
    probe.write_text("0.02\n")

    done = heavy(env, "--alone", "--", "true")

    assert "0.50x its usual speed at the start and 0.50x at the end" in done.stderr
    assert (
        "Below 0.8x at both, so this timing isn't one to trust; run it again later."
        in done.stderr
    )


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_during_an_alone_jobs_start_probe_stops_heavy(box, background, sig):
    # The fake probe is a pipe the test holds open, so heavy is inside its start probe,
    # reading it, when the signal comes.
    tmp_path, _, _ = box
    pipe, ran = tmp_path / "probe", tmp_path / "ran"
    os.mkfifo(pipe)
    h = background("--alone", "--", "touch", str(ran), HEAVY_SPEED_PROBE=str(pipe))
    fd = None

    def probe_reads():
        nonlocal fd
        try:
            fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # ENXIO: heavy hasn't opened it yet
            return False
        return True

    wait_until(probe_reads, "heavy to read its start probe")
    try:
        h.send_signal(sig)
        h.wait(timeout=20)
    finally:
        os.close(fd)

    assert h.returncode == 128 + sig
    assert not ran.exists()
    assert not held(tmp_path)


def test_an_alone_job_with_no_usual_speed_yet_says_none(outside):
    done = outside(0, "--alone")

    assert "usual speed" not in done.stderr
    assert outside.report.read_text().split()[3:] == ["-", "-"]


def test_an_alone_job_names_the_time_the_host_stole(box):
    tmp_path, _, env = box
    stat, after = tmp_path / "stat", tmp_path / "stat.after"
    stat.write_text(proc_stat(0))
    first = min(os.sched_getaffinity(0))
    after.write_text(
        proc_stat(0).replace(
            f"cpu{first} 0 0 0 1000 0 0 0 0", f"cpu{first} 0 0 0 1000 0 0 0 500"
        )
    )
    sccache = tmp_path / "sccache"
    sccache.write_text("0\n")

    done = heavy(
        env,
        "--alone",
        "--",
        "sh",
        "-c",
        'cp "$0" "$HEAVY_STAT"',
        str(after),
        HEAVY_STAT=str(stat),
        HEAVY_SCCACHE_CPU=str(sccache),
        HEAVY_CORES_USED="0",
    )

    assert "the host stole 5.0 s of its cores' time." in done.stderr


# The record of the jobs heavy ran, and `heavy history`.


def history(env, *args, **extra):
    done = heavy(env, "history", *args, **extra)
    assert done.returncode == 0, done.stderr
    return done.stdout


def record_lines(tmp_path):
    path = tmp_path / "heavy" / "jobs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def ended(tmp_path):
    """Each job's whole record, start and end merged, by its id, from the file."""
    jobs = {}
    for line in record_lines(tmp_path):
        jobs.setdefault(line["id"], {}).update(line)
    return list(jobs.values())


def line_of(text, needle):
    found = [ln for ln in text.splitlines() if needle in ln]
    assert len(found) == 1, text
    return found[0]


def test_history_lists_each_job_with_its_status_folder_and_command(box):
    tmp_path, _, env = box
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    heavy(env, "--", "true", "first")
    heavy(env, "--", "sh", "-c", "exit 3", cwd=elsewhere)
    shown = history(env)

    first, failed = line_of(shown, "true first"), line_of(shown, "sh -c")
    assert "exit 0" in first and str(tmp_path) in first, shown
    assert "exit 3" in failed and str(elsewhere) in failed, shown
    assert "s-test" in first and "normal" in first, shown
    assert shown.index("true first") < shown.index("sh -c"), shown


@pytest.mark.parametrize(
    "argv, shown",
    [
        (["true", "run", "--token", "s3cret", "more"], "true run --token"),
        (["true", "run", "--token=s3cret", "more"], "true run"),
        (["env", "TOKEN=s3cret", "true"], "env"),
        (["true", "clone", "https://me:s3cret@example.com/x"], "true clone"),
        (["mysql", "-ps3cret", "db"], "mysql"),
        (["curl", "-HAuthorization: Bearer s3cret", "x"], "curl"),
        (["true", "-v", "--dry-run", "-ks3cret"], "true -v --dry-run"),
        (["true", "--to.ken", "s3cret"], "true"),
    ],
)
def test_a_command_is_recorded_only_up_to_its_first_value(box, argv, shown):
    tmp_path, _, env = box

    heavy(env, "--", *argv)

    text = (tmp_path / "heavy" / "jobs.jsonl").read_text()
    assert "s3cret" not in text and "more" not in text, text
    (job,) = ended(tmp_path)
    assert job["cmd"] == shown, job
    assert "s3cret" not in history(env)


def test_a_command_with_an_unbalanced_quote_is_recorded_as_typed():
    assert heavyjobs.shown("wt-ship's checks") == ("wt-ship's checks", False)


def write_record(queue, *lines):
    queue.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps({"v": 1} | line) + "\n" for line in lines)
    (queue / heavyjobs.NAME).write_text(text)


def test_beside_finds_only_the_pids_job_that_started_since(tmp_path):
    write_record(
        tmp_path,
        {"id": "old", "pid": 42, "at_start": 100.0, "at_end": 200.0, "cmd": "old"},
        {"id": "x", "pid": 7, "at_start": 150.0, "at_end": 160.0, "cmd": "x"},
    )

    assert heavyjobs.beside(tmp_path, 42, since=1000.0, now=2000.0) == []
    assert len(heavyjobs.beside(tmp_path, 42, since=100.0, now=2000.0)) == 2


def test_beside_sums_up_and_lists_only_the_heaviest_jobs(tmp_path):
    mine = {"id": "me", "pid": 42, "at_start": 100.0, "at_end": 200.0}
    others = [
        {"id": f"j{n}", "pid": n, "at_start": 150.0, "at_end": 160.0}
        | {"cmd": f"job{n}", "cpu": float(n), "nice": 10 if n % 2 else 0}
        for n in range(1, 6)
    ]
    write_record(tmp_path, mine | {"other_cpu": 300.0}, *others)

    lines = heavyjobs.beside(tmp_path, 42, since=100.0, now=2000.0)

    assert lines[0].startswith("heavy: 5 jobs overlapped it (2 at full priority)")
    assert "other work used 300s on its cores (3.0 cores)" in lines[0]
    assert [line.rsplit(" ", 1)[-1] for line in lines[1:]] == ["job5", "job4", "job3"]


def test_a_long_command_is_cut_to_its_first_words(box):
    tmp_path, _, env = box

    heavy(env, "--", "true", *(["word"] * 100))

    (job,) = ended(tmp_path)
    assert job["cmd"].startswith("true word") and len(job["cmd"]) <= heavyjobs.CHARS
    assert job["cut"] is True, job


def test_a_record_says_when_the_job_joined_started_and_ended(box, background):
    tmp_path, _, _ = box
    first, second = blocker(tmp_path, "a"), blocker(tmp_path, "b")
    a = background("--", *first[0])
    wait_until(first[1].exists, "a to start")
    b = background("--", *second[0], session="s-waiter")
    wait_until(lambda: entries(tmp_path), "b to queue")
    first[2].touch()
    wait_until(second[1].exists, "b to start")
    second[2].touch()
    for proc in (a, b):
        finish(proc)

    one, two = sorted(ended(tmp_path), key=lambda job: job["joined"])
    assert one["joined"] <= one["at_start"] <= one["at_end"] <= two["at_start"]
    assert two["joined"] < one["at_end"], (one, two)
    assert (two["session"], two["pid"], two["exit"]) == ("s-waiter", b.pid, 0)
    assert two["how"] == "slot" and two["lane"] == "normal", two


def test_a_running_job_shows_in_history_as_running(box, background):
    tmp_path, _, env = box
    command, started, release = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")

    shown = history(env)
    release.touch()
    finish(a)

    assert "running" in line_of(shown, f"pid {a.pid}"), shown
    assert "exit 0" in line_of(history(env), f"pid {a.pid}")


def test_a_stopped_job_is_recorded_as_stopped(box, background):
    tmp_path, _, env = box
    command, started, _ = blocker(tmp_path, "a")
    a = background("--", *command)
    wait_until(started.exists, "a to start")

    a.send_signal(signal.SIGTERM)
    finish(a)

    (job,) = ended(tmp_path)
    assert job["signal"] == signal.SIGTERM and job["exit"] is None, job
    assert "stopped by signal 15" in line_of(history(env), f"pid {a.pid}")


def test_a_command_that_cant_be_found_is_recorded_with_its_status(box):
    tmp_path, _, _ = box

    heavy(box[2], "--", "no-such-command-anywhere")

    (job,) = ended(tmp_path)
    assert job["exit"] == 127, job


def test_a_heavy_inside_a_job_adds_no_record_of_its_own(box):
    tmp_path, _, env = box

    done = heavy(env, "--", str(SCRIPT), "--", "true")

    assert done.returncode == 0, done.stderr
    assert len(ended(tmp_path)) == 1


def test_a_few_seconds_job_outside_the_queue_is_recorded_so(box):
    tmp_path, _, env = box
    meminfo(tmp_path / "meminfo", 16)  # room to run outside the queue

    heavy(env, "--few-seconds", "--", "true")

    (job,) = ended(tmp_path)
    assert (job["how"], job["quick"], job["nice"]) == ("outside", True, 10), job
    assert "outside the queue" in line_of(history(env), "true")


def test_a_sharers_record_names_the_holder_it_started_beside(box, background, sharing):
    tmp_path, _, env = box
    holder, sharer = job_report(tmp_path, "holder"), job_report(tmp_path, "sharer")
    h = background("--", *holder[0])
    wait_until(holder[1].exists, "the holder to start")
    s = background("--share", "--", *sharer[0])
    wait_until(sharer[1].exists, "the waiter to share")
    holder[2].touch()
    sharer[2].touch()
    for proc in (h, s):
        finish(proc)

    job = next(job for job in ended(tmp_path) if job["pid"] == s.pid)
    assert job["how"] == "share" and job["beside"]["pid"] == h.pid, job
    assert f"beside pid {h.pid}" in line_of(history(env), f"pid {s.pid}")


def test_a_record_counts_the_cpu_other_work_used_on_the_jobs_cores(outside, box):
    tmp_path, _, _ = box

    outside(4200)  # not --alone: every job's record measures it

    (job,) = ended(tmp_path)
    assert (job["cpu"], job["other_cpu"]) == (0.0, 42.0), job


def test_overlapping_a_pid_lists_the_jobs_that_ran_beside_it(box, background):
    tmp_path, _, env = box
    meminfo(tmp_path / "meminfo", 16)  # room for the --few-seconds job beside a
    command, started, release = blocker(tmp_path, "a")
    before = heavy(env, "--", "true", "before")
    a = background("--", *command)
    wait_until(started.exists, "a to start")
    beside = heavy(env, "--few-seconds", "--", "true", "beside")
    release.touch()
    finish(a)
    after = heavy(env, "--", "true", "after")
    assert {before.returncode, beside.returncode, after.returncode} == {0}

    shown = history(env, "--overlapping", str(a.pid))

    assert "true beside" in shown, shown
    assert "true before" not in shown and "true after" not in shown, shown
    assert shown.splitlines()[0].startswith("heavy: 1 job overlapped"), shown


# Whole hours a little more than 6 hours ago: a test's times come after it, all of them
# past, so a time of day given as HH:MM, which names its latest past moment, finds them.
BASE = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
BASE -= timedelta(hours=6)


def at(hour, minute=0):
    """An epoch time hour:minute after BASE."""
    return (BASE + timedelta(hours=hour, minutes=minute)).timestamp()


def clock(moment):
    """An epoch time as HH:MM, local."""
    return datetime.fromtimestamp(moment).astimezone().strftime("%H:%M")


def write_jobs(tmp_path, jobs):
    """Write a record of whole jobs, each (name, start, end): end None for one whose
    end isn't recorded, by a process that has ended."""
    folder = tmp_path / "heavy"
    folder.mkdir(exist_ok=True)
    lines = []
    for number, (name, start, end) in enumerate(jobs):
        ident = f"{number}"
        lines.append(
            {"v": 1, "id": ident, "at_start": start, "joined": start, "pid": 999999}
            | {"cmd": name, "cwd": "/w", "lane": "normal", "how": "slot"}
            | {"session": "s-x", "proc_start": "1", "host": socket.gethostname()}
        )
        if end is not None:
            lines.append({"v": 1, "id": ident, "at_end": end, "exit": 0})
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (folder / "jobs.jsonl").write_text(text)


def test_overlapping_a_time_range_lists_the_jobs_that_ran_in_it(box):
    tmp_path, _, env = box
    write_jobs(
        tmp_path,
        [
            ("early", at(1), at(1, 30)),
            ("across", at(1, 50), at(2, 20)),
            ("inside", at(2, 5), at(2, 10)),
            ("late", at(3), at(3, 5)),
            ("lost", at(2, 40), None),
        ],
    )

    shown = history(env, "--overlapping", f"{clock(at(2))}..{clock(at(2, 30))}")
    lost = history(env, "--overlapping", clock(at(2, 45)))

    assert "across" in shown and "inside" in shown, shown
    assert "early" not in shown and "late" not in shown, shown
    assert "lost" in lost and "no end recorded" in lost, lost


def test_since_leaves_out_the_jobs_that_started_before(box):
    tmp_path, _, env = box
    write_jobs(
        tmp_path, [("stale", at(0, 5), at(0, 6)), ("fresh", at(0, 20), at(0, 21))]
    )
    moment = datetime.fromtimestamp(at(0, 10)).astimezone().isoformat()

    shown = history(env, "--since", moment)

    assert "fresh" in shown and "stale" not in shown, shown


def test_history_by_default_shows_the_last_hour(box):
    tmp_path, _, env = box
    now = time.time()
    write_jobs(
        tmp_path, [("stale", now - 7200, now - 7100), ("fresh", now - 60, now - 50)]
    )

    shown = history(env)

    assert "fresh" in shown and "stale" not in shown, shown
    assert "1 older" in shown, shown


def test_history_with_no_record_says_so(box):
    _, _, env = box
    assert "no jobs recorded" in history(env)


@pytest.mark.parametrize("args", [["--since", "soon"], ["--overlapping", "x..y"]])
def test_history_given_a_time_it_cant_read_is_a_usage_error(box, args):
    _, _, env = box
    done = heavy(env, "history", *args)
    assert done.returncode == 2, done.stderr


def test_overlapping_a_pid_finds_the_job_by_its_commands_pid_too(box):
    tmp_path, _, env = box
    pidfile = tmp_path / "cmdpid"

    heavy(env, "--", "sh", "-c", f'echo $$ > "{pidfile}"')

    cmd_pid = int(pidfile.read_text())
    (job,) = ended(tmp_path)
    assert job["cmd_pid"] == cmd_pid, job
    shown = history(env, "--overlapping", str(cmd_pid))
    assert shown.startswith(f"heavy: 0 jobs overlapped pid {cmd_pid}'s"), shown


# `heavy waits`: how long jobs waited for their turn, and who waits now.


def write_waits(tmp_path, recorded, now):
    """A record of jobs, each (seconds before now it started, seconds it waited) and
    optionally a dict of fields to set, such as its lane or how it got in (by default
    the normal lane's slot), from several sessions, each job's end recorded."""
    lines = []
    for number, (ago, waited, *fields) in enumerate(recorded):
        start = now - ago
        lines.append(
            {"v": 1, "id": f"{number}", "at_start": start, "joined": start - waited}
            | {"pid": 999999, "session": f"s-{number % 3}", "cmd": "make"}
            | {"lane": "normal", "how": "slot"}
            | (fields[0] if fields else {})
        )
        lines.append({"v": 1, "id": f"{number}", "at_end": start + 1, "exit": 0})
    folder = tmp_path / "heavy"
    folder.mkdir(exist_ok=True)
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (folder / "jobs.jsonl").write_text(text)


def add_waiter(tmp_path, lane_name, waited, pid=None):
    """A queue entry in lane_name for a live process (this test's, by default) that
    joined `waited` seconds ago."""
    pid = os.getpid() if pid is None else pid
    since = (datetime.now().astimezone() - timedelta(seconds=waited)).isoformat(
        timespec="seconds"
    )
    folder = lane(tmp_path, lane_name)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns() - int(waited * 1e9):020d}-{pid}"
    (folder / name).write_text(
        f"s-waiting {since} host={socket.gethostname()} pid={pid}"
        f" start={fifo.started(str(pid))}\ncwd=/w\ncmd=make\n"
    )


def waits(env, *args):
    done = heavy(env, "waits", *args)
    assert done.returncode == 0, done.stderr
    return done.stdout


# Waits of ten jobs in the last half hour, 1 to 10 minutes, shuffled: nearest rank
# puts p50 at the 5th smallest and p90 at the 9th.
TEN_WAITS = [(60 * n, 60 * w) for n, w in enumerate([3, 9, 1, 10, 5, 2, 8, 4, 7, 6], 1)]


def test_waits_gives_the_percentiles_of_the_windows_jobs_and_who_waits_now(box):
    tmp_path, _, env = box
    now = time.time()
    write_waits(tmp_path, [*TEN_WAITS, (3 * 3600, 3600)], now)
    add_waiter(tmp_path, "normal", 125)
    add_waiter(tmp_path, "long", 250)

    found = json.loads(waits(env, "--json"))

    assert found["started"] == 10, found
    # The two waiters, at 2m05s and 4m10s, count in p90 and max, not in p50: 12
    # waits, so p90 is the 11th smallest.
    spread = (found["p50"], found["p90"], found["max"])
    assert spread == pytest.approx((300, 540, 600)), found
    assert found["waiting"] == 2, found
    assert found["waiting_by_lane"] == {"normal": 1, "long": 1}, found
    assert 249 <= found["oldest_waiting"] <= 260, found
    assert abs(found["since"] - (now - 1800)) < 30, found


def test_waits_says_it_all_in_one_line(box):
    tmp_path, _, env = box
    write_waits(tmp_path, TEN_WAITS, time.time())
    add_waiter(tmp_path, "normal", 125)

    shown = waits(env)

    assert len(shown.splitlines()) == 1, shown
    assert "10 jobs started" in shown, shown
    assert "p50 5m00s, p90 9m00s, max 10m00s" in shown, shown
    assert "1 waiting now, the oldest for 2m" in shown, shown


def test_waits_with_too_few_jobs_gives_no_percentiles(box):
    """13:05 on 2026-11-26: 5 jobs started in the window, so one 12m49s wait from a
    throttled spell was p90 and max both, and a gate read held with 0 waiting."""
    tmp_path, _, env = box
    write_waits(
        tmp_path, [(60, 30), (120, 40), (180, 20), (240, 769), (300, 10)], time.time()
    )

    found = json.loads(waits(env, "--json"))
    shown = waits(env)
    gate = until_open(env, "--max-wait", "0")
    enough = json.loads(waits(env, "--json", "--min-jobs", "5"))

    assert found["started"] == 5 and found["min_jobs"] == 10, found
    assert found["p50"] is None and found["p90"] is None, found
    assert found["max"] == pytest.approx(769), found  # not a percentile: still shown
    assert "too few jobs for percentiles (under 10)" in shown, shown
    assert "p90" not in shown and "max 12m49s" in shown, shown
    assert gate.returncode == 0 and gate.stdout.startswith("open: "), gate.stdout
    assert enough["p90"] == pytest.approx(769), enough


def test_waits_refuses_a_negative_min_jobs(box):
    _, _, env = box
    done = heavy(env, "waits", "--min-jobs", "-1")
    assert done.returncode == 2 and "--min-jobs" in done.stderr, done.stderr


def test_waits_since_sets_the_window(box):
    tmp_path, _, env = box
    write_waits(tmp_path, TEN_WAITS, time.time())

    found = json.loads(waits(env, "--since", "270s", "--json"))

    assert found["started"] == 4, found  # started 1, 2, 3 and 4 minutes ago
    assert found["max"] == pytest.approx(600), found


def test_waits_with_no_job_and_no_waiter_says_so(box):
    _, _, env = box

    shown = waits(env)
    found = json.loads(waits(env, "--json"))

    assert "0 jobs started" in shown and "0 waiting now" in shown, shown
    assert found["started"] == 0 and found["waiting"] == 0, found
    assert None is found["p50"] is found["p90"] is found["max"], found
    assert found["oldest_waiting"] is None, found


def test_waits_leaves_out_waiters_that_ended_and_jobs_with_no_join_time(box):
    tmp_path, _, env = box
    now = time.time()
    write_waits(tmp_path, [(60, 30)], now)
    with (tmp_path / "heavy" / "jobs.jsonl").open("a") as out:
        out.write(json.dumps({"v": 1, "id": "x", "at_start": now - 10}) + "\n")
    gone = subprocess.Popen(["true"])
    gone.wait()
    add_waiter(tmp_path, "normal", 600, pid=gone.pid)

    found = json.loads(waits(env, "--json"))

    assert found["started"] == 1 and found["max"] == pytest.approx(30), found
    assert found["waiting"] == 0, found


def test_a_waiter_whose_join_time_cant_be_read_counts_but_has_no_age(box):
    tmp_path, _, env = box
    add_waiter(tmp_path, "normal", 60)
    (entry,) = lane(tmp_path).iterdir()
    first, rest = entry.read_text().split("\n", 1)
    words = first.split()
    entry.write_text(" ".join([words[0], "not-a-time", *words[2:]]) + "\n" + rest)

    found = json.loads(waits(env, "--json"))

    assert found["waiting"] == 1, found
    assert found["oldest_waiting"] is None, found


def test_few_seconds_jobs_run_beside_a_slot_are_left_out_and_counted(box):
    tmp_path, _, env = box
    beside = {"how": "outside"}
    write_waits(tmp_path, [*TEN_WAITS, *[(30, 0, beside)] * 20], time.time())

    found = json.loads(waits(env, "--json"))

    assert found["started"] == 10, found
    assert found["outside_left_out"] == 20, found
    assert found["p90"] == pytest.approx(540), found


def test_a_job_still_waiting_counts_in_p90_and_max_before_any_job_starts(box):
    tmp_path, _, env = box
    add_waiter(tmp_path, "normal", 400)

    found = json.loads(waits(env, "--json", "--min-jobs", "0"))
    shown = waits(env, "--min-jobs", "0")

    assert found["started"] == 0 and found["p50"] is None, found
    assert 399 <= found["p90"] <= 410, found
    assert found["max"] == found["p90"], found
    assert "p90 6m" in shown and "max 6m" in shown, shown
    assert "1 waiting now" in shown, shown


def test_a_long_wait_still_under_way_raises_p90_and_max(box):
    tmp_path, _, env = box
    write_waits(tmp_path, TEN_WAITS, time.time())
    add_waiter(tmp_path, "normal", 3600)

    found = json.loads(waits(env, "--json"))

    assert found["p50"] == pytest.approx(300), found
    assert found["p90"] == pytest.approx(600), found  # 11 waits: the 10th smallest
    assert 3599 <= found["max"] <= 3610, found


def test_lane_counts_only_that_lanes_jobs_and_waiters(box):
    tmp_path, _, env = box
    long_lane = {"lane": "long"}
    write_waits(tmp_path, [*TEN_WAITS, (60, 1800, long_lane)], time.time())
    add_waiter(tmp_path, "long", 2400)
    add_waiter(tmp_path, "normal", 125)

    normal = json.loads(waits(env, "--lane", "normal", "--json"))
    long = json.loads(waits(env, "--lane", "long", "--json"))
    shown = waits(env, "--lane", "normal")

    assert normal["lane"] == "normal" and normal["started"] == 10, normal
    assert normal["max"] == pytest.approx(600), normal
    assert normal["waiting"] == 1, normal
    assert normal["waiting_by_lane"] == {"normal": 1, "long": 1}, normal
    assert long["started"] == 1 and long["waiting"] == 1, long
    assert 2399 <= long["max"] <= 2410, long
    assert "normal lane" in shown, shown


def test_lane_must_be_normal_or_long(box):
    _, _, env = box
    assert heavy(env, "waits", "--lane", "fast").returncode == 2


def test_waits_given_a_window_it_cant_read_is_a_usage_error(box):
    _, _, env = box
    done = heavy(env, "waits", "--since", "soon")
    assert done.returncode == 2, done.stderr


def test_history_reads_a_record_whose_fields_have_the_wrong_shape(box):
    tmp_path, _, env = box
    write_jobs(tmp_path, [("odd", time.time() - 60, None)])
    path = tmp_path / "heavy" / "jobs.jsonl"
    odd = {"v": 1, "id": "0", "beside": "x", "parents": [5, "y", [1]], "how": "share"}
    odd |= {"session": None, "pid": "x", "signal": "x", "lane": 3, "cpu": "x"}
    odd |= {"at_end": time.time(), "exit": "z", "other_cpu": [], "cwd": 7}
    with path.open("a") as out:
        out.write(json.dumps(odd) + "\n" + json.dumps({"id": "1", "at_start": "x"}))
        out.write("\n[1]\nnot json\n")

    shown = history(env)
    assert "odd" in shown, shown
    assert "odd" in history(env, "--overlapping", "5m..0s")
    assert "no job recorded" in history(env, "--overlapping", "12345")


def test_a_few_seconds_job_from_a_folder_since_removed_still_runs(box):
    tmp_path, _, env = box
    meminfo(tmp_path / "meminfo", 16)  # room to run outside the queue
    script = f'mkdir gone && cd gone && rmdir ../gone && exec "{SCRIPT}" --few-seconds -- sh -c "exit 4"'

    done = subprocess.run(
        ["sh", "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 4, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    (job,) = ended(tmp_path)
    assert job["exit"] == 4 and job["cwd"] is None, job


def test_a_record_with_bytes_that_arent_utf8_is_still_cut(box):
    tmp_path, _, env = box
    old = json.dumps({"v": 1, "id": "old", "at_start": 1.0, "cmd": "x" * 100}) + "\n"
    count = heavyjobs.TRIM_BYTES // len(old) + 1
    (tmp_path / "heavy").mkdir()
    (tmp_path / "heavy" / "jobs.jsonl").write_bytes(
        b"\xff\xfe\n" + old.encode() * count
    )

    done = heavy(env, "--", "true", "newest")

    assert "can't record" not in done.stderr, done.stderr
    assert len(record_lines(tmp_path)) == heavyjobs.KEEP_LINES + 1


def test_a_record_that_cant_be_written_never_fails_the_job(box):
    tmp_path, _, env = box
    (tmp_path / "heavy").mkdir()
    (tmp_path / "heavy" / "jobs.jsonl").mkdir()  # so no line can be appended

    done = heavy(env, "--", "sh", "-c", "echo out; exit 3")

    assert (done.returncode, done.stdout) == (3, "out\n"), done.stderr
    assert "Traceback" not in done.stderr, done.stderr


def test_a_record_past_its_size_keeps_only_its_newest_lines(box):
    tmp_path, _, env = box
    old = json.dumps({"v": 1, "id": "old", "at_start": 1.0, "cmd": "x" * 100}) + "\n"
    count = heavyjobs.TRIM_BYTES // len(old) + 1
    (tmp_path / "heavy").mkdir()
    (tmp_path / "heavy" / "jobs.jsonl").write_text(old * count)

    heavy(env, "--", "true", "newest")

    lines = record_lines(tmp_path)
    # Cut at the start line's write, then the end line comes.
    assert len(lines) == heavyjobs.KEEP_LINES + 1, len(lines)
    assert lines[-1]["id"] == lines[-2]["id"] and lines[-2]["cmd"] == "true newest"


# `heavy waits --until-open`: wait for a quiet queue, as the overnight gate does.


def until_open(env, *args):
    """Run `heavy waits --until-open` with its readings 0.05 s apart."""
    return heavy(env, "waits", "--until-open", *args, HEAVY_WAITS_POLL="0.05")


def test_until_open_on_a_quiet_queue_exits_0_with_one_reading(box):
    _, _, env = box

    done = until_open(env)

    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line.startswith("open: heavy, both lanes:"), line


def test_until_open_holds_at_p90_over_the_threshold_until_max_wait(box):
    tmp_path, _, env = box
    write_waits(tmp_path, TEN_WAITS, time.time())  # p90 9m

    done = until_open(env, "--waiting", "5", "--max-wait", "0.3")
    loose = until_open(env, "--p90", "600", "--max-wait", "0")

    assert done.returncode == 3, done.stderr
    # Two lines, or one when the first reading took past --max-wait on a slow machine.
    lines = done.stdout.splitlines()
    assert 1 <= len(lines) <= 2, done.stdout
    assert all(line.startswith("held: ") for line in lines), done.stdout
    assert "p90 9m00s" in lines[-1], lines
    assert loose.returncode == 0, loose.stdout
    assert loose.stdout.startswith("open: "), loose.stdout


def test_until_open_counts_waiters_in_the_lane_it_names(box):
    tmp_path, _, env = box
    add_waiter(tmp_path, "long", 1)
    add_waiter(tmp_path, "normal", 1)

    both = until_open(env, "--waiting", "1", "--max-wait", "0")
    normal = until_open(env, "--waiting", "1", "--max-wait", "0", "--lane", "normal")
    none = until_open(env, "--waiting", "0", "--max-wait", "0", "--lane", "normal")

    assert both.returncode == 3, both.stdout
    assert "2 waiting now" in both.stdout, both.stdout
    assert normal.returncode == 0, normal.stdout
    assert none.returncode == 3, none.stdout


def test_until_open_prints_its_first_and_last_reading_when_the_queue_clears(
    box, background
):
    tmp_path, _, _ = box
    sleeper = subprocess.Popen(["sleep", "600"])
    try:
        add_waiter(tmp_path, "normal", 400, pid=sleeper.pid)  # p90 at least 6m40s
        proc = background(
            "waits", "--until-open", "--min-jobs", "0", HEAVY_WAITS_POLL="0.05"
        )
        first = proc.stdout.readline()
        assert first.startswith("held: "), first
    finally:
        sleeper.kill()
        sleeper.wait()

    out, err = proc.communicate(timeout=60)

    assert proc.returncode == 0, err
    (last,) = out.splitlines()  # the readings between print nothing
    assert last.startswith("open: ") and "0 waiting now" in last, last


def test_until_open_refuses_a_poll_interval_it_cant_read(box):
    _, _, env = box

    done = heavy(env, "waits", "--until-open", HEAVY_WAITS_POLL="soon")

    assert done.returncode == 2, done.stdout
    assert "HEAVY_WAITS_POLL" in done.stderr, done.stderr


def test_until_open_options_need_until_open(box):
    _, _, env = box

    done = heavy(env, "waits", "--p90", "10")

    assert done.returncode == 2, done.stdout
    assert "--until-open" in done.stderr, done.stderr


# `heavy waits --until-quiet`: wait for an empty queue on a quiet machine, as the
# end of a night's timing runs does.


def until_quiet(box, *args, load="0.50"):
    """Run `heavy waits --until-quiet` with its readings 0.05 s apart and the 1-minute
    load average `load`."""
    tmp_path, _, env = box
    loadavg = tmp_path / "loadavg"
    loadavg.write_text(f"{load} 3.00 2.00 1/300 1234\n")
    return heavy(
        env,
        "waits",
        "--until-quiet",
        *args,
        HEAVY_WAITS_POLL="0.05",
        HEAVY_LOADAVG=str(loadavg),
    )


def test_until_quiet_on_an_empty_queue_and_a_quiet_machine_exits_0(box):
    done = until_quiet(box)

    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line == "quiet: 0 running, 0 waiting, load 0.50 (under 2)", line


def test_until_quiet_holds_for_a_waiter_in_either_lane(box):
    tmp_path, _, _ = box
    add_waiter(tmp_path, "long", 1)

    done = until_quiet(box, "--max-wait", "0")

    assert done.returncode == 3, done.stderr
    (line,) = done.stdout.splitlines()
    assert line == "busy: 0 running, 1 waiting, load 0.50 (under 2)", line


def test_until_quiet_holds_for_a_high_load_average_unless_load_allows_it(box):
    busy = until_quiet(box, "--max-wait", "0.2", load="4.40")
    allowed = until_quiet(box, "--load", "5", load="4.40")

    assert busy.returncode == 3, busy.stderr
    assert all(line.startswith("busy: ") for line in busy.stdout.splitlines())
    assert "load 4.40 (2 or more)" in busy.stdout, busy.stdout
    assert allowed.returncode == 0, allowed.stderr
    assert "load 4.40 (under 5)" in allowed.stdout, allowed.stdout


def test_until_quiet_counts_a_few_seconds_job_running_outside_the_queue(box):
    tmp_path, _, _ = box
    folder = tmp_path / "heavy"
    folder.mkdir()
    pid = os.getpid()  # alive while the test runs
    job = {"v": 1, "id": "q", "at_start": time.time(), "pid": pid, "how": "outside"}
    job |= {"proc_start": fifo.started(str(pid)), "host": socket.gethostname()}
    gone = job | {"id": "g", "pid": 999999}  # its heavy ended with no end line
    (folder / "jobs.jsonl").write_text(json.dumps(job) + "\n" + json.dumps(gone) + "\n")

    done = until_quiet(box, "--max-wait", "0", "--json")

    assert done.returncode == 3, done.stderr
    found = json.loads(done.stdout)
    assert found == {
        "quiet": False,
        "running": 1,
        "waiting": 0,
        "load": 0.5,
        "load_under": 2.0,
    }, found


def test_until_quiet_waits_for_the_running_job_to_end(box, background):
    tmp_path, _, _ = box
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("0.10 0.10 0.10 1/300 1234\n")
    command, started, release = blocker(tmp_path, "a")
    holder = background("--", *command)
    wait_until(started.exists, "a to start")
    waiter = background(
        "waits",
        "--until-quiet",
        HEAVY_WAITS_POLL="0.05",
        HEAVY_LOADAVG=str(loadavg),
    )
    first = waiter.stdout.readline()
    release.touch()
    finish(holder)

    code, out, err = finish(waiter)

    assert first == "busy: 1 running, 0 waiting, load 0.10 (under 2)\n", first
    assert code == 0, err
    assert out == "quiet: 0 running, 0 waiting, load 0.10 (under 2)\n", out


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--until-quiet", "--until-open"], "--until-open"),
        (["--until-quiet", "--p90", "10"], "--p90"),
        (["--until-quiet", "--lane", "long"], "--lane"),
        (["--until-quiet", "--since", "1h"], "--since"),
        (["--until-quiet", "--min-jobs", "3"], "--min-jobs"),
        (["--load", "3"], "--until-quiet"),
        (["--max-wait", "10"], "--until-quiet"),
    ],
)
def test_until_quiet_refuses_options_that_dont_go_with_it(box, args, named):
    _, _, env = box

    done = heavy(env, "waits", *args)

    assert done.returncode == 2, done.stdout
    assert named in done.stderr.splitlines()[-1], done.stderr  # not just the usage


@pytest.mark.parametrize(
    "queue", ["unset", "the shared path", "a link to it", "a shared path via a link"]
)
def test_in_an_own_pids_namespace_it_refuses_the_shared_queue(box, queue):
    # HEAVY_SHARED_DIR stands in for /tmp/heavy, so a heavy that fails to refuse runs
    # on a queue of the test's, never the live one other sessions share.
    tmp_path, _, env = box
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    shared = tmp_path / "real" / "heavy"
    env = {k: v for k, v in env.items() if k != "HEAVY_DIR"} | {
        "OWN_PIDS": "1",
        "HEAVY_SHARED_DIR": str(shared),
    }
    if queue == "the shared path":
        env["HEAVY_DIR"] = str(shared)
    elif queue == "a link to it":
        env["HEAVY_DIR"] = str(tmp_path / "link" / "heavy")
    elif queue == "a shared path via a link":
        # As on a machine whose /tmp is a link.
        env["HEAVY_SHARED_DIR"] = str(tmp_path / "link" / "heavy")
        env["HEAVY_DIR"] = str(shared)

    done = subprocess.run(
        [str(SCRIPT), "--few-seconds", "--", "touch", "ran"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )

    assert done.returncode == 125, done.stderr
    assert "heavy -- mutate-check" in done.stderr
    assert not (tmp_path / "ran").exists()
    assert not shared.exists()


def test_the_live_queue_is_shared_whatever_its_stand_in(tmp_path, monkeypatch):
    # By import: run as a tool, a heavy that failed this would use the live queue.
    monkeypatch.setenv("HEAVY_SHARED_DIR", str(tmp_path / "heavy"))

    assert heavyqueue.is_shared(Path("/tmp/heavy"))
    assert heavyqueue.is_shared(tmp_path / "heavy")
    assert not heavyqueue.is_shared(tmp_path / "other")


def test_in_an_own_pids_namespace_a_queue_of_its_own_still_works(box):
    tmp_path, _, env = box

    done = heavy(env, "--", "touch", "ran", OWN_PIDS="1")

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "ran").exists()
