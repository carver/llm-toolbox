"""Performance promises: a repo's perf-promises.toml promises how long a command run
through heavy takes. The tests run heavy as a process over fake commands in a
throwaway repo, and `perf-promises status` and `--session-start` over logs written
here, with the logs under PERF_PROMISES_DIR (conftest gives each test its own) and
llm-toolbox's own promises replaced by PERF_PROMISES_GLOBAL."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import without_repo_vars

ROOT = Path(__file__).resolve().parent
HEAVY = ROOT / "bin" / "heavy"
TOOL = ROOT / "bin" / "perf-promises"


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
    end, seconds, status, load, promised, digest, session, folder = line.split("\t")
    assert abs(datetime.fromisoformat(end) - datetime.now(UTC)) < timedelta(minutes=1)
    assert 0.1 <= float(seconds) < 5
    assert (status, load, promised, session, folder) == (
        "0",
        "-",
        "30",
        "s-test",
        str(repo),
    )
    assert len(digest) == 8


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
    """rows: (seconds, exit, load, promised, digest, age in days)."""
    path = log_of(repo_name, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    lines = []
    for seconds, status, load, promised, digest, days in rows:
        when = (now - timedelta(days=days)).isoformat(timespec="seconds")
        lines.append(
            f"{when}\t{seconds}\t{status}\t{load}\t{promised}\t{digest}\ts\t/x"
        )
    path.write_text("\n".join(lines) + "\n")


def digest_of(command):
    return hashlib.sha256(command.encode()).hexdigest()[:8]


def runs(*seconds, status="0", load="-", promised=10, command="^x", days=0):
    return [(s, status, load, promised, digest_of(command), days) for s in seconds]


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
    ],
)
def test_runs_another_definition_logged_or_left_out_dont_count(tmp_path, left_out):
    repo = make_repo(tmp_path / "proj", promise("p", "^x", 10))
    write_log("proj", "p", runs(50, 50, 50) + left_out)

    _, line = status_line(repo, "p")

    assert "n=3, not judged" in line


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
