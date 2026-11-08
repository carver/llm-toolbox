"""heavy runs a CPU-heavy command once it's its turn in its lane and its cores are
quiet. The tests run it as a process with its queue in tmp_path, a file standing in
for how busy the cores are (HEAVY_BUSY), and short waits."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


@pytest.fixture
def box(tmp_path):
    """The queue folder, the busy file (quiet to start) and an env to run heavy in."""
    busy = tmp_path / "busy"
    set_busy(busy, "0.0")
    env = os.environ | {
        "HEAVY_DIR": str(tmp_path / "heavy"),
        "HEAVY_BUSY": str(busy),
        "HEAVY_QUIET_SECONDS": "0.2",
        "HEAVY_POLL": "0.05",
        "HEAVY_STALL_SECONDS": "600",
        "CLAUDE_CODE_SESSION_ID": "s-test",
    }
    env.pop("HEAVY_SLOT", None)
    return tmp_path, busy, env


@pytest.fixture
def background(box):
    """Start heavy in the background; every run is killed and reaped at the end."""
    procs = []
    _, _, env = box

    def start(*args, session="s-test", stderr=None, **extra):
        """stderr: a file to write heavy's stderr to, to read while it runs."""
        proc = subprocess.Popen(
            [str(SCRIPT), *args],
            env=env | {"CLAUDE_CODE_SESSION_ID": session, **extra},
            stdout=subprocess.PIPE,
            stderr=stderr.open("w") if stderr else subprocess.PIPE,
            text=True,
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


def heavy(env, *args, **extra):
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env | extra,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
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


def test_a_job_that_arrives_to_a_free_quiet_lane_starts_at_once(box):
    _, _, env = box

    started = time.monotonic()
    done = heavy(env, "--", "true", HEAVY_QUIET_SECONDS="5", HEAVY_POLL="0.2")

    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < 3  # one sample, not the 5 s window


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
