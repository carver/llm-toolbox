"""Performance promises: a repo's perf-promises.toml promises how long a command run
through heavy takes. The tests run heavy as a process over fake commands in a
throwaway repo, and `perf-promises status` and `--session-start` over logs written
here, with the logs under PERF_PROMISES_DIR (conftest gives each test its own) and
llm-toolbox's own promises replaced by PERF_PROMISES_GLOBAL."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import without_repo_vars

ROOT = Path(__file__).resolve().parent
HEAVY = ROOT / "bin" / "heavy"
TOOL = ROOT / "bin" / "perf-promises"
sys.path.insert(0, str(ROOT / "lib"))
import machinespeed


@pytest.fixture(autouse=True)
def quiet_cores(tmp_path, monkeypatch):
    """A /proc/stat and sccache that never move, so no run is left out for the load
    the suite's own workers put on the cores; a test that wants load names its own."""
    stat, sccache = tmp_path / "quiet-stat", tmp_path / "quiet-sccache"
    stat.write_text(proc_stat(0))
    sccache.write_text("0\n")
    monkeypatch.setenv("HEAVY_STAT", str(stat))
    monkeypatch.setenv("HEAVY_SCCACHE_CPU", str(sccache))


def promise(name, command, seconds, measured_seconds=None, counts=None, **extra):
    """One [[promise]] table."""
    lines = [
        "[[promise]]",
        f'name = "{name}"',
        f"command = '{command}'",
        f"seconds = {seconds}",
        'measured = "by hand"',
        f"measured_seconds = {measured_seconds or seconds}",
        'source = "test"',
    ]
    if counts is not None:
        lines.append(f"counts = {counts}")
    lines += [f"{key} = {value}" for key, value in extra.items()]
    return "\n".join(lines) + "\n"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    ).stdout


def make_repo(path, *tables):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    if tables:
        (path / "perf-promises.toml").write_text("".join(tables))
    return path


def env(**extra):
    found = without_repo_vars(os.environ) | {
        "HEAVY_QUIET_SECONDS": "0",
        "HEAVY_POLL": "0.01",
        "CLAUDE_CODE_SESSION_ID": "s-test",
    }
    found.pop("HEAVY_SLOT", None)
    return found | extra


def heavy(folder, *command, **extra):
    return subprocess.run(
        [str(HEAVY), "--", *command],
        cwd=folder,
        env=env(**extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def tool(*args, stdin=None, cwd=None, **extra):
    return subprocess.run(
        [str(TOOL), *args],
        cwd=cwd,
        env=env(**extra),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def log_of(repo_name, name):
    return Path(os.environ["PERF_PROMISES_DIR"]) / repo_name / f"{name}.tsv"


def notes(stderr):
    return [line for line in stderr.splitlines() if "promise" in line]


# heavy, at a job's end


# A promise no run can keep, so an instant command gets its note without a sleep.
TINY = 0.0001


def test_a_run_of_twice_its_promise_or_more_gets_one_note(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^true", TINY))

    done = heavy(repo, "true")

    assert done.returncode == 0, done.stderr
    (line,) = notes(done.stderr)
    assert re.fullmatch(
        r"heavy: nap ran \d+\.\d s, \d+\.\dx its promised 0\.0 s"
        r" \(proj/perf-promises\.toml\): perf-promises status",
        line,
    )


def test_each_run_goes_on_the_promises_log(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^sleep", 30))

    done = heavy(repo, "sleep", "0.1")

    assert notes(done.stderr) == []
    (line,) = log_of("proj", "nap").read_text().splitlines()
    fields = line.split("\t")
    end, seconds, status, load, promised, digest, session, folder, speed = fields
    assert abs(datetime.fromisoformat(end) - datetime.now(UTC)) < timedelta(minutes=1)
    assert 0.1 <= float(seconds) < 5
    assert (status, load, promised, session, folder, speed) == (
        "0",
        "-",
        "30",
        "s-test",
        str(repo),
        "-",  # no usual speed yet
    )
    assert len(digest) == 8


# The machine's speed (lib/machinespeed.py): heavy judges a job by the newest probe of
# the last 5 minutes, and probes again after it exits, in a child of its own, which
# the suite turns off (conftest) except where a test turns it on (after_exit).


def speed_file():
    return machinespeed.speed_file()


def speed_lines():
    path = speed_file()
    if not path.exists():
        return []
    return [line.split("\t") for line in path.read_text().splitlines()]


def heavy_span(cpus):
    if len(cpus) > 1 and cpus == list(range(cpus[0], cpus[-1] + 1)):
        return f"{cpus[0]}-{cpus[-1]}"
    return ",".join(map(str, cpus))


def probe_line(seconds=0.01, factor="-", age=0, vcpu=None, load="0.00", mark="probe"):
    """A speed file line: a probe that took seconds, age seconds ago, on vcpu (the
    job's first core, where a faked probe runs) under load, judged at factor."""
    when = (datetime.now(UTC) - timedelta(seconds=age)).isoformat(timespec="seconds")
    cpus = sorted(os.sched_getaffinity(0))
    vcpu = cpus[0] if vcpu is None else vcpu
    fields = [when, seconds, seconds, "-", heavy_span(cpus), vcpu, load, mark, factor]
    return "\t".join(map(str, fields)) + "\n"


def seed(*lines):
    path = speed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))


def usual_speed(seconds=0.01, count=20, **kw):
    """count probes that took seconds: the machine's usual speed, by default on the
    job's first core under no load (kw: probe_line's age, vcpu and load)."""
    seed(probe_line(seconds, **kw) * count)


@pytest.fixture
def probe(tmp_path):
    """The file whose numbers stand in for the probe's seconds and the load before
    it; probe(s, load) sets them."""
    path = tmp_path / "probe"

    def at(seconds, load=0.0):
        path.write_text(f"{seconds} {load}\n")
        return {"HEAVY_SPEED_PROBE": str(path)}

    return at


def probe_done(path=None):
    """Whether no after-exit probe holds its lock beside the speed file: once heavy
    has exited, whether the probe it started has ended."""
    lock = machinespeed.probe_lock(path or speed_file())
    if not lock.exists():
        return True
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def wait_until(condition, what, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def probed(repo, *command, **extra):
    """Run heavy with its after-exit probe on and wait for that probe to end: heavy's
    result and the speed file's newest line."""
    done = heavy(repo, *(command or ("true",)), HEAVY_SPEED_AFTER_EXIT="1", **extra)
    wait_until(probe_done, "the after-exit probe to end")
    return done, speed_lines()[-1]


def logged_speed(repo_name, name):
    (line,) = log_of(repo_name, name).read_text().splitlines()
    return line.split("\t")[8]


def test_every_job_is_probed_after_heavy_exits(tmp_path):
    repo = make_repo(tmp_path / "proj")

    done, line = probed(repo)

    assert done.returncode == 0, done.stderr
    (when, wall, process, steal, cores, vcpu, load, mark, factor) = line
    assert abs(datetime.fromisoformat(when) - datetime.now(UTC)) < timedelta(minutes=1)
    # The loop takes about 8 ms; a busy machine may take ten times that.
    assert 0.0005 < float(process) < 2
    assert 0.0005 < float(wall) < 10
    assert float(steal) == 0  # the quiet /proc/stat
    assert cores == heavy_span(sorted(os.sched_getaffinity(0)))
    assert int(vcpu) in os.sched_getaffinity(0)
    assert load == "-"  # the quiet /proc/stat never moves, so no time passed on it
    assert (mark, factor) == ("probe", "-")


def test_heavy_exits_and_frees_its_slot_before_its_probe_runs(tmp_path):
    # The fake probe is a pipe, so the probe waits until the test writes to it.
    pipe = tmp_path / "probe"
    os.mkfifo(pipe)
    repo = make_repo(tmp_path / "proj")

    done = heavy(repo, "true", HEAVY_SPEED_AFTER_EXIT="1", HEAVY_SPEED_PROBE=str(pipe))

    assert done.returncode == 0, done.stderr
    assert not (Path(os.environ["HEAVY_DIR"]) / "normal" / "slot").exists()
    assert speed_lines() == []
    assert not probe_done()

    def opened():
        try:
            return os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # ENXIO: the probe hasn't opened it yet
            return None

    fd = None

    def reader_came():
        nonlocal fd
        fd = opened()
        return fd is not None

    wait_until(reader_came, "the probe to read its pipe")
    os.write(fd, b"0.02 0\n")
    os.close(fd)
    wait_until(probe_done, "the after-exit probe to end")
    ((_, _, process, *_, mark, _),) = speed_lines()
    assert (float(process), mark) == (0.02, "fake")


def test_a_job_ending_while_a_probe_runs_starts_none(tmp_path, probe):
    repo = make_repo(tmp_path / "proj")
    seed()
    lock = os.open(machinespeed.probe_lock(speed_file()), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = heavy(repo, "true", HEAVY_SPEED_AFTER_EXIT="1", **probe(0.01))
    finally:
        os.close(lock)

    assert done.returncode == 0, done.stderr
    wait_until(probe_done, "any after-exit probe to end")
    assert speed_lines() == []


def test_a_probe_names_the_load_in_the_moment_before_it(tmp_path, monkeypatch):
    monkeypatch.delenv("HEAVY_STAT")  # the real /proc/stat, which moves
    repo = make_repo(tmp_path / "proj")

    _, (*_, vcpu, load, _, _) = probed(repo)

    assert int(vcpu) in os.sched_getaffinity(0)
    assert 0 <= float(load) <= os.cpu_count()


def test_a_probe_takes_the_load_of_the_busier_moment_before_or_during_it(
    tmp_path, monkeypatch
):
    # The moment before the probe is quiet, as when a job has just freed its slot,
    # and every core is busy while it runs, as when the next job has ramped up.
    # By import: the real loop gives way to one that moves HEAVY_STAT as it runs.
    cpus = sorted(os.sched_getaffinity(0))  # before the probe pins this process

    def stat(user, idle):
        return "".join(f"cpu{cpu} {user} 0 0 {idle} 0 0 0 0\n" for cpu in cpus)

    path = tmp_path / "stat"
    path.write_text(stat(0, 2000))
    monkeypatch.setenv("HEAVY_STAT", str(path))
    monkeypatch.setenv("HEAVY_POLL", "1")
    quiet = machinespeed.cores.Sample(
        str(path), stat(0, 1000), stat(0, 2000), 1.0, 0.0, time.monotonic()
    )
    monkeypatch.setattr(machinespeed.cores, "newest", quiet)

    def busy_loop():
        path.write_text(stat(100_000, 2000))
        return 0.01, 0.01

    monkeypatch.setattr(machinespeed, "probe", busy_loop)

    machinespeed.measure(cpus)

    (line,) = speed_lines()
    assert abs(float(line[6]) - len(cpus)) < 0.05, line


def test_a_faked_probe_is_marked(tmp_path, probe):
    repo = make_repo(tmp_path / "proj")

    _, (_, wall, process, _, _, vcpu, load, mark, _) = probed(repo, **probe(0.25, 2.5))

    assert (float(wall), float(process), mark) == (0.25, 0.25, "fake")
    assert (int(vcpu), load) == (min(os.sched_getaffinity(0)), "2.50")


def test_a_probe_is_judged_against_the_usual_speed(tmp_path, probe):
    usual_speed(0.01)
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo, **probe(0.025))

    assert line[8] == "0.40"


def test_with_fewer_than_twenty_probes_the_speed_is_unknown(tmp_path, probe):
    usual_speed(0.01, count=19)
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo, **probe(0.025))

    assert line[8] == "-"


@pytest.mark.parametrize(
    "seeded",
    [
        {"age": 4 * 86400},  # past the 3 days that count
        {"vcpu": "999"},  # another vCPU's
        {"load": "1.50"},  # under another load
    ],
)
def test_probes_that_dont_count_toward_the_usual_speed(tmp_path, probe, seeded):
    usual_speed(0.01, **seeded)
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo, **probe(0.025))

    assert line[8] == "-"


def test_fakes_dont_count_for_a_probe_that_fakes_none(tmp_path, monkeypatch):
    # Fakes for every vCPU under every load, so the real probe, on whichever vCPU and
    # under whatever load, would find a usual speed if they counted.
    seed(
        *(
            probe_line(0.01, vcpu=vcpu, load=load, mark="fake") * 20
            for vcpu in os.sched_getaffinity(0)
            for load in (0.5, 2, 4, 8)
        )
    )
    monkeypatch.delenv("HEAVY_STAT")  # a /proc/stat that moves, so the load is known
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo)

    assert (line[7], line[8]) == ("probe", "-")


@pytest.mark.parametrize(
    ("seeded", "now", "same"),
    [
        (0.0, 0.99, True),
        (1.0, 2.99, True),
        (3.0, 5.99, True),
        (6.0, 15.0, True),  # past 6 vCPUs the power budget is spent: one bucket
        (0.99, 1.0, False),
        (2.99, 3.0, False),
        (5.99, 6.0, False),
    ],
)
def test_the_usual_speed_is_of_probes_under_the_same_load(
    tmp_path, probe, seeded, now, same
):
    usual_speed(0.01, load=f"{seeded:.2f}")
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo, **probe(0.02, now))

    assert line[8] == ("0.50" if same else "-")


def test_the_usual_speed_is_the_fast_tenth_of_the_probes(tmp_path, probe):
    # 2 of 20 at 10 ms, the rest at 40: the 10th percentile is 10 ms, the median 40.
    seed(probe_line(0.01) * 2, probe_line(0.04) * 18)
    repo = make_repo(tmp_path / "proj")

    _, line = probed(repo, **probe(0.02))

    assert line[8] == "0.50"


def test_the_speed_file_is_trimmed_to_its_newest_two_thousand(tmp_path, probe):
    usual_speed(count=4001)
    repo = make_repo(tmp_path / "proj")

    probed(repo, **probe(0.5))

    lines = speed_lines()
    assert len(lines) == 2000
    assert lines[-1][2] == "0.500000"


def test_a_run_counts_at_the_speed_of_the_newest_probe(tmp_path):
    seed(probe_line(factor="0.90", age=240), probe_line(factor="0.40", age=120))
    repo = make_repo(tmp_path / "proj", promise("nap", "^true", TINY))

    done = heavy(repo, "true")

    (line,) = notes(done.stderr)
    assert re.fullmatch(
        r"heavy: nap ran \d+\.\d s \(\d+\.\d s at the machine's 0\.40x speed,"
        r" measured 2 min ago\), \d+\.\dx its promised 0\.0 s"
        r" \(proj/perf-promises\.toml\): perf-promises status",
        line,
    ), line
    assert logged_speed("proj", "nap") == "0.40"
    assert len(speed_lines()) == 2  # the job's end took no probe of its own


def test_a_newer_probe_of_unknown_speed_gives_way_to_a_known_one(tmp_path):
    seed(probe_line(factor="0.40", age=60), probe_line(factor="-", age=30))
    repo = make_repo(tmp_path / "proj", promise("nap", "^true", TINY))

    done = heavy(repo, "true")

    (line,) = notes(done.stderr)
    assert "at the machine's 0.40x speed, measured 1 min ago" in line, line
    assert logged_speed("proj", "nap") == "0.40"


def test_a_run_its_slow_machine_explains_doesnt_count(tmp_path):
    # 0.1 s is 2.5x a 0.04 s promise; at 0.05x speed it's under 0.01 s.
    seed(probe_line(factor="0.05"))
    repo = make_repo(tmp_path / "proj", promise("nap", "^sleep", 0.04))

    done = heavy(repo, "sleep", "0.1")

    (line,) = notes(done.stderr)
    assert re.search(r"at the machine's 0\.05x speed, measured \d+ s ago\), 0\.", line)
    assert line.endswith(", so it doesn't count: perf-promises status"), line


@pytest.mark.parametrize(
    "newest",
    [
        {"factor": "0.40", "age": 330},  # past the 5 minutes
        {"factor": "-", "age": 60},  # newer than the 0.40 one, of unknown speed
        {"factor": "0.40", "age": 60, "mark": "fake"},  # a test's, and none faked
    ],
)
def test_with_no_known_speed_from_the_last_five_minutes_a_run_counts_raw(
    tmp_path, newest
):
    seed(probe_line(factor="0.40", age=400), probe_line(**newest))
    repo = make_repo(tmp_path / "proj", promise("nap", "^true", TINY))

    done = heavy(repo, "true")

    (line,) = notes(done.stderr)
    assert "machine's" not in line
    assert logged_speed("proj", "nap") == "-"


def test_a_speed_file_it_cant_read_is_one_note_and_the_job_keeps_its_status(tmp_path):
    speed_file().mkdir(parents=True)
    repo = make_repo(tmp_path / "proj")

    done = heavy(repo, "sh", "-c", "exit 3")

    assert done.returncode == 3
    (line,) = [ln for ln in done.stderr.splitlines() if "speed" in ln]
    assert line.startswith("heavy: can't measure the machine's speed (")


def test_heavy_speed_prints_the_usual_time_by_vcpu_and_load():
    now = datetime.now(UTC)
    when = now.isoformat(timespec="seconds")
    old = (now - timedelta(days=4)).isoformat(timespec="seconds")

    def lines(count, seconds, vcpu, load, mark="probe", at=when):
        return f"{at}\t{seconds}\t{seconds}\t-\t0-15\t{vcpu}\t{load}\t{mark}\n" * count

    seed(
        lines(20, 0.008, 0, 0.5)
        + lines(25, 0.016, 0, 4)
        + lines(3, 0.02, 9, 7)
        + lines(5, 0.001, 9, 7, mark="fake")  # a test's, which doesn't count
        + lines(5, 0.001, 9, 7, at=old)  # past the 3 days
        + lines(5, 0.001, 9, "-")  # no load known
    )

    done = subprocess.run(
        [str(HEAVY), "speed"],
        env=env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    rows = [re.split(r"\s{2,}", line.strip()) for line in done.stdout.splitlines()]
    assert ["vCPU", "under 1", "1-3", "3-6", "6+"] in rows
    assert ["0", "8.0 ms (20)", "-", "16.0 ms (25)", "-"] in rows
    assert ["9", "-", "-", "-", "- (3)"] in rows
    assert len(rows) == 4, done.stdout  # a header line, the columns and 2 vCPUs


def test_heavy_speed_with_no_probes_says_so(tmp_path):
    done = subprocess.run(
        [str(HEAVY), "speed"],
        env=env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("heavy speed: no probes in the last 3 days")


def test_the_test_knobs_need_perf_promises_dir(tmp_path):
    # A copy of heavy in a repo of its own, so its speed file lands in that repo's
    # git dir, as the real one lands in llm-toolbox's, and not in this checkout's.
    toolbox = make_repo(tmp_path / "toolbox")
    (toolbox / "bin").mkdir()
    shutil.copy2(HEAVY, toolbox / "bin" / "heavy")
    shutil.copytree(ROOT / "lib", toolbox / "lib", symlinks=True)
    probe_file = tmp_path / "probe"
    probe_file.write_text("5\n")
    repo = make_repo(tmp_path / "proj")
    run_env = env(HEAVY_SPEED_PROBE=str(probe_file), HEAVY_SPEED_AFTER_EXIT="0")
    run_env.pop("PERF_PROMISES_DIR")

    done = subprocess.run(
        [str(toolbox / "bin" / "heavy"), "--", "true"],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    folder = toolbox / ".git" / "perf-promises"

    def probed_there():
        found = list(folder.glob("speed-*.tsv"))
        return len(found) == 1 and probe_done(found[0])

    wait_until(probed_there, "the after-exit probe to end")
    (path,) = folder.glob("speed-*.tsv")
    ((_, _, process, *_, mark, _),) = [
        ln.split("\t") for ln in path.read_text().splitlines()
    ]
    assert mark == "probe"
    assert float(process) < 2


def test_the_queue_wait_isnt_counted(tmp_path):
    # Quiet takes 1 s of samples before the job starts; the run itself is instant.
    repo = make_repo(tmp_path / "proj", promise("fast", "^true", 0.4))

    done = heavy(repo, "true", HEAVY_QUIET_SECONDS="1")

    assert notes(done.stderr) == []
    (line,) = log_of("proj", "fast").read_text().splitlines()
    assert float(line.split("\t")[1]) < 0.4


def test_a_note_heavy_cant_write_leaves_the_commands_status(tmp_path):
    # As in `heavy -- x 2>&1 | head`, once head has gone.
    repo = make_repo(tmp_path / "proj", promise("nap", "^sh", 0.001, counts="[3]"))
    reader, writer = os.pipe()
    os.close(reader)
    try:
        done = subprocess.run(
            [str(HEAVY), "--", "sh", "-c", "exit 3"],
            cwd=repo,
            env=env(),
            stdout=subprocess.DEVNULL,
            stderr=writer,
            timeout=60,
            check=False,
        )
    finally:
        os.close(writer)

    assert done.returncode == 3
    assert log_of("proj", "nap").exists()


def test_a_stop_while_git_runs_leaves_the_commands_status(tmp_path):
    # The command has ended by then; a stop must not turn its 0 into heavy's 143.
    repo = make_repo(tmp_path / "proj", promise("nap", "^sh", 30))
    armed, in_git = tmp_path / "armed", tmp_path / "in-git"
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "git").write_text(
        "#!/bin/sh\n"
        f'if [ -e "{armed}" ]; then touch "{in_git}"; exec sleep 3; fi\n'
        f'exec {shutil.which("git")} "$@"\n'
    )
    (fakes / "git").chmod(0o755)
    run_env = env(PATH=f"{fakes}{os.pathsep}{os.environ['PATH']}")

    with subprocess.Popen(
        [str(HEAVY), "--", "sh", "-c", 'touch "$0"', str(armed)],
        cwd=repo,
        env=run_env,
        stderr=subprocess.PIPE,
        text=True,
    ) as job:
        deadline = time.monotonic() + 30
        while not in_git.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        job.send_signal(signal.SIGTERM)
        _, stderr = job.communicate(timeout=60)

    assert in_git.exists()
    assert job.returncode == 0, stderr


def test_a_failing_run_is_logged_but_gets_no_note(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^sh", TINY))

    done = heavy(repo, "sh", "-c", "exit 1")

    assert done.returncode == 1
    assert notes(done.stderr) == []
    (line,) = log_of("proj", "nap").read_text().splitlines()
    assert line.split("\t")[2] == "1"


def test_a_status_the_promise_counts_gets_a_note(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^sh", TINY, counts="[0, 1]"))

    done = heavy(repo, "sh", "-c", "exit 1")

    assert done.returncode == 1
    assert len(notes(done.stderr)) == 1


def test_the_first_word_is_matched_without_its_folder(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^nap 0", TINY))
    script = repo / "bin" / "nap"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    done = heavy(repo, "bin/nap", "0")

    assert len(notes(done.stderr)) == 1, done.stderr


def test_a_file_that_cant_be_read_is_one_note_and_the_job_keeps_its_status(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "perf-promises.toml").write_text("[[promise]\n")

    done = heavy(repo, "sh", "-c", "exit 3")

    assert done.returncode == 3
    (line,) = notes(done.stderr)
    assert str(repo / "perf-promises.toml") in line


def test_an_entry_missing_a_key_is_named_and_the_rest_still_work(tmp_path):
    broken = promise("half", "^sleep", 0.1).replace('source = "test"\n', "")
    repo = make_repo(tmp_path / "proj", broken, promise("nap", "^sleep", 30))

    done = heavy(repo, "sleep", "0")

    assert done.returncode == 0
    assert log_of("proj", "nap").exists()
    assert not log_of("proj", "half").exists()
    listed = tool("status", "-C", str(repo))
    assert "'half' lacks source" in listed.stdout


def test_llm_toolboxs_promises_cover_a_repo_with_none_of_its_own(tmp_path):
    toolbox = make_repo(tmp_path / "llm-toolbox", promise("nap", "^true", TINY))
    other = make_repo(tmp_path / "other")

    done = heavy(
        other, "true", PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml")
    )

    (line,) = notes(done.stderr)
    assert "(llm-toolbox/perf-promises.toml)" in line
    assert log_of("llm-toolbox", "nap").exists()


def test_a_repos_own_promise_and_llm_toolboxs_both_log_a_job(tmp_path):
    toolbox = make_repo(tmp_path / "llm-toolbox", promise("any-nap", "^sleep", 30))
    repo = make_repo(tmp_path / "proj", promise("nap", "^sleep", 30))

    heavy(repo, "sleep", "0", PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml"))

    assert log_of("proj", "nap").exists()
    assert log_of("llm-toolbox", "any-nap").exists()


def test_an_own_repo_only_promise_skips_another_repos_job(tmp_path):
    # wt-ship's checks has that name in every repo, so llm-toolbox's landing promise
    # mustn't judge filedrawer's landings.
    toolbox = make_repo(
        tmp_path / "llm-toolbox",
        promise("own-nap", "^sleep", 30, own_repo_only="true"),
        promise("any-nap", "^sleep", 30),
    )
    other = make_repo(tmp_path / "other")

    heavy(other, "sleep", "0", PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml"))

    assert not log_of("llm-toolbox", "own-nap").exists()
    # The next promise in the file still takes the job.
    assert log_of("llm-toolbox", "any-nap").exists()


def test_an_own_repo_only_promise_logs_its_own_repos_job(tmp_path):
    toolbox = make_repo(
        tmp_path / "llm-toolbox",
        promise("own-nap", "^sleep", 30, own_repo_only="true"),
    )
    git(toolbox, "add", "perf-promises.toml")
    git(toolbox, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    tree = tmp_path / "llm-toolbox-wt-x"
    git(toolbox, "worktree", "add", "-q", "--detach", str(tree))

    heavy(tree, "sleep", "0", PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml"))

    assert log_of("llm-toolbox", "own-nap").exists()


def test_own_repo_only_must_be_true_or_false(tmp_path):
    repo = make_repo(
        tmp_path / "proj", promise("own", "^sleep", 30, own_repo_only='"yes"')
    )

    listed = tool("status", "-C", str(repo))

    assert "'own' has own_repo_only that isn't true or false" in listed.stdout


def test_in_a_worktree_of_llm_toolbox_its_own_copy_stands(tmp_path):
    toolbox = make_repo(tmp_path / "llm-toolbox", promise("old", "^sleep", 30))
    git(toolbox, "add", "perf-promises.toml")
    git(toolbox, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    tree = tmp_path / "llm-toolbox-wt-x"
    git(toolbox, "worktree", "add", "-q", "--detach", str(tree))
    (tree / "perf-promises.toml").write_text(promise("new", "^sleep", 30))

    heavy(tree, "sleep", "0", PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml"))

    # Logged by the main tree's name, shared by its worktrees.
    assert log_of("llm-toolbox", "new").exists()
    assert not log_of("llm-toolbox", "old").exists()


def proc_stat(ticks):
    """A /proc/stat with `ticks` user ticks on every core of this process's."""
    lines = ["cpu  0 0 0 0 0 0 0 0"]
    lines += [f"cpu{cpu} {ticks} 0 0 1000 0 0 0 0" for cpu in os.sched_getaffinity(0)]
    return "\n".join(lines) + "\n"


def test_a_run_beside_heavy_other_work_is_logged_but_not_counted(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("nap", "^sh", TINY))
    stat, after, sccache = tmp_path / "stat", tmp_path / "after", tmp_path / "sccache"
    stat.write_text(proc_stat(0))
    after.write_text(proc_stat(10_000))  # 100 s a core at 100 ticks a second
    sccache.write_text("0\n")

    done = heavy(
        repo,
        "sh",
        "-c",
        'cp "$0" "$HEAVY_STAT"',
        str(after),
        HEAVY_STAT=str(stat),
        HEAVY_SCCACHE_CPU=str(sccache),
        HEAVY_CORES_USED="0",
    )

    (line,) = notes(done.stderr)
    assert (
        "but other work used " in line and "of its cores, so it doesn't count" in line
    )
    (logged,) = log_of("proj", "nap").read_text().splitlines()
    assert logged.split("\t")[3].startswith("load=")


# perf-promises status, over a prepared log


def write_log(repo_name, name, rows):
    """rows: (seconds, exit, load, promised, digest, age in days, speed factor or
    None for a line from before heavy logged one)."""
    path = log_of(repo_name, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    lines = []
    for seconds, status, load, promised, digest, days, speed in rows:
        when = (now - timedelta(days=days)).isoformat(timespec="seconds")
        line = f"{when}\t{seconds}\t{status}\t{load}\t{promised}\t{digest}\ts\t/x"
        lines.append(line if speed is None else f"{line}\t{speed}")
    path.write_text("\n".join(lines) + "\n")


def digest_of(command):
    return hashlib.sha256(command.encode()).hexdigest()[:8]


def runs(*seconds, status="0", load="-", promised=10, command="^x", days=0, speed=None):
    return [
        (s, status, load, promised, digest_of(command), days, speed) for s in seconds
    ]


def status_line(repo, name):
    done = tool("status", "-C", str(repo))
    (line,) = [
        ln for ln in done.stdout.splitlines() if ln.strip().startswith(name + ":")
    ]
    return done, line


def test_a_median_of_twice_the_promise_over_the_last_five_runs_is_broken(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(1, 1, 1, 25, 20, 30, 19, 40))

    done, line = status_line(repo, "p")

    assert done.returncode == 1
    assert "promised 10 s, 8 runs logged, last-5 median 25 s (2.5x)" in line
    assert line.endswith("BROKEN")


def test_one_slow_run_is_not_broken(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(9, 9, 9, 9, 100))

    done, line = status_line(repo, "p")

    assert done.returncode == 0
    assert "BROKEN" not in line
    assert "last-5 median 9.0 s" in line


def test_fewer_than_five_counted_runs_are_not_judged(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(50, 50, 50) + runs(50, 50, status="1"))

    _, line = status_line(repo, "p")

    assert "5 runs logged, n=3, not judged" in line


@pytest.mark.parametrize(
    "left_out",
    [
        runs(50, 50, load="share"),
        runs(50, 50, load="load=0.80"),
        runs(50, 50, days=15),
        runs(50, 50, promised=5),  # another definition's seconds
        runs(50, 50, command="^y"),  # another definition's command
        runs(50, 50, speed="0.10"),  # 5 s at its usual speed: the slowness explains it
    ],
)
def test_runs_another_definition_logged_or_left_out_dont_count(tmp_path, left_out):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(50, 50, 50) + left_out)

    _, line = status_line(repo, "p")

    assert "n=3, not judged" in line


def test_the_median_is_of_the_runs_at_the_machines_usual_speed(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(100, 100, 100, speed="0.30") + runs(30, 30, speed="-"))

    done, line = status_line(repo, "p")

    assert done.returncode == 1
    assert "last-5 median 30 s (3.0x)" in line
    assert "BROKEN" in line


def test_a_promise_whose_runs_are_mostly_left_out_is_flagged(tmp_path):
    # 6 of the last 10: 4 for the machine's speed, 2 for other work's load.
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    rows = runs(9, 9, 9, 9) + runs(50, 50, 50, 50, speed="0.10")
    write_log("proj", "p", rows + runs(9, 9, load="load=0.90"))

    _, line = status_line(repo, "p")
    started = session_start(repo)

    flag = "6 of its last 10 runs were left out, for the machine's speed or load"
    assert flag in line
    assert f"p {flag}" in json.loads(started.stdout)["systemMessage"]


def test_a_promise_with_fewer_than_ten_runs_all_left_out_is_flagged(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(50, 50, 50, 50, 50, speed="0.10"))

    _, line = status_line(repo, "p")

    assert "5 of its last 5 runs were left out" in line


def test_half_of_its_runs_left_out_is_judged_on_the_rest_unflagged(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(9, 9, 9, 9, 9) + runs(50, 50, 50, 50, 50, speed="0.1"))

    _, line = status_line(repo, "p")

    assert "last-5 median 9.0 s" in line
    assert "left out" not in line


def test_counts_and_signals_decide_which_statuses_count(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10, counts="[0, 1]"))
    write_log(
        "proj", "p", runs(50, 50) + runs(50, status="1") + runs(50, 50, status="sig15")
    )

    _, line = status_line(repo, "p")

    assert line.endswith("BROKEN")


def test_a_promise_above_three_times_its_measurement_is_flagged(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 100, measured_seconds=20))
    write_log("proj", "p", runs(1, promised=100))

    _, line = status_line(repo, "p")

    assert "over 3x the 20 s measured" in line


def test_a_promise_unmatched_for_seven_days_is_flagged(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(1, days=8))

    _, line = status_line(repo, "p")

    assert "matched no job in 7 days" in line


def test_a_promise_that_never_matched_shows_only_in_status(tmp_path):
    # A sentinel such as mutate-check-diff is meant never to match.
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    week_ago = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    os.utime(repo / "perf-promises.toml", (week_ago, week_ago))

    _, line = status_line(repo, "p")
    started = session_start(repo)

    assert line.endswith("; never matched")
    assert (started.returncode, started.stdout) == (0, "")


def test_a_promise_unmatched_for_seven_days_is_flagged_at_session_start(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(1, days=8))

    done = session_start(repo)

    assert "p matched no job in 7 days" in json.loads(done.stdout)["systemMessage"]


def test_a_log_line_that_cant_be_read_is_skipped(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(50, 50, 50, 50, 50))
    with log_of("proj", "p").open("a") as log:
        log.write("garbage\nnot-a-time\t1\t0\t-\t10\tabc\n")

    _, line = status_line(repo, "p")

    assert line.endswith("BROKEN")


def test_status_lists_llm_toolboxs_promises_too(tmp_path):
    toolbox = make_repo(tmp_path / "llm-toolbox", promise("shared", "^x", 10))
    repo = make_repo(tmp_path / "proj", promise("own", "^x", 10))

    done = tool(
        "status",
        "-C",
        str(repo),
        PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml"),
    )

    assert "  own: promised 10 s" in done.stdout
    assert "  shared: promised 10 s" in done.stdout


def test_by_default_llm_toolboxs_promises_are_the_ones_beside_the_tool(tmp_path):
    # This checkout's own file: its seeded mutate-check --diff promise.
    other = make_repo(tmp_path / "other")
    run_env = {k: v for k, v in env().items() if k != "PERF_PROMISES_GLOBAL"}

    done = subprocess.run(
        [str(TOOL), "status", "-C", str(other)],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "  mutate-check-diff: promised" in done.stdout


# perf-promises --session-start


def session_start(cwd, **extra):
    return tool("--session-start", stdin=json.dumps({"cwd": str(cwd)}), **extra)


def test_a_broken_promise_reaches_the_owner_and_the_agent_at_session_start(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(30, 30, 30, 30, 30))

    done = session_start(repo)

    assert done.returncode == 0
    out = json.loads(done.stdout)
    line = (
        "perf promise broken in proj: p median 30 s over its last 5 runs, 3.0x the"
        " 10 s promised (test); perf-promises status"
    )
    assert out["systemMessage"] == line
    assert out["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": line,
    }


def test_session_start_is_silent_when_every_promise_holds(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(10, 10, 10, 10, 10))

    done = session_start(repo)

    assert (done.returncode, done.stdout) == (0, "")


def test_session_start_names_a_file_that_cant_be_read(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "perf-promises.toml").write_text("not toml [\n")

    done = session_start(repo)

    assert str(repo / "perf-promises.toml") in json.loads(done.stdout)["systemMessage"]


def test_session_start_checks_llm_toolboxs_promises_from_any_folder(tmp_path):
    toolbox = make_repo(tmp_path / "llm-toolbox", promise("p", "^x", 10))
    write_log("llm-toolbox", "p", runs(30, 30, 30, 30, 30))
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()

    done = session_start(
        elsewhere, PERF_PROMISES_GLOBAL=str(toolbox / "perf-promises.toml")
    )

    assert (
        "perf promise broken in llm-toolbox: p"
        in json.loads(done.stdout)["systemMessage"]
    )


@pytest.mark.parametrize("stdin", ["", "not json", "{}"])
def test_session_start_never_fails(stdin):
    done = tool("--session-start", stdin=stdin)
    assert (done.returncode, done.stdout) == (0, "")


def test_status_with_no_promises_says_so(tmp_path):
    repo = make_repo(tmp_path / "proj")

    done = tool("status", "-C", str(repo))

    assert done.returncode == 0
    assert "no perf-promises.toml" in done.stdout


def test_old_lines_are_trimmed_to_the_newest_two_hundred(tmp_path):
    repo = make_repo(tmp_path / "proj", promise("p", "^true", 30))
    write_log("proj", "p", runs(*range(400)))

    heavy(repo, "true")

    lines = log_of("proj", "p").read_text().splitlines()
    assert len(lines) == 200
    assert lines[0].split("\t")[1] == "201"
    assert lines[-1].split("\t")[2] == "0"
