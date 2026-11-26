"""heavy-bench runs each variant's command as its own `heavy --alone` job, round by
round, and sums the runs up, flagging and rerunning one taken under outside load.

Each test runs heavy-bench against a queue of its own (HEAVY_DIR) whose cores read
quiet (HEAVY_BUSY), as test_heavy.py does; outside load is staged the same way, by a
command that rewrites the HEAVY_STAT file heavy reads in place of /proc/stat.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "heavy-bench"
FIELDS = [
    "variant",
    "round",
    "attempt",
    "wall",
    "user",
    "sys",
    "outside",
    "exit",
    "flagged",
    "start_speed",
    "end_speed",
    "parts",
]


def proc_stat(busy_on_first):
    cpus = sorted(os.sched_getaffinity(0))
    lines = ["cpu  0 0 0 0 0 0 0 0"]
    for cpu in cpus:
        user = busy_on_first if cpu == cpus[0] else 0
        lines.append(f"cpu{cpu} {user} 0 0 1000 0 0 0 0")
    return "\n".join(lines) + "\n"


@pytest.fixture
def bench(tmp_path):
    busy = tmp_path / "busy"
    busy.write_text("0.0")
    (tmp_path / "meminfo").write_text(
        "MemTotal: 16777216 kB\nMemAvailable: 1048576 kB\n"
    )
    stat = tmp_path / "stat"
    stat.write_text(proc_stat(0))
    (tmp_path / "stat.quiet").write_text(proc_stat(0))
    (tmp_path / "stat.busy").write_text(proc_stat(10**6))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HEAVY_SLOT", "HEAVY_ALONE_REPORT")
    }
    env |= {
        "HEAVY_DIR": str(tmp_path / "heavy"),
        "HEAVY_BUSY": str(busy),
        "HEAVY_MEMINFO": str(tmp_path / "meminfo"),
        "HEAVY_STAT": str(stat),
        "HEAVY_CORES_USED": "0",
        "HEAVY_QUIET_SECONDS": "0.1",
        "HEAVY_POLL": "0.02",
        "CLAUDE_CODE_SESSION_ID": "s-test",
    }
    out = tmp_path / "out"

    def runner(env):
        def run(*args):
            return subprocess.run(
                [str(SCRIPT), "--out", str(out), *args],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

        return run

    run = runner(env)
    run.with_env = lambda **extra: runner(env | extra)
    run.env = env

    def rows():
        lines = (out / "results.tsv").read_text().splitlines()
        assert lines[0].split("\t") == FIELDS
        return [dict(zip(FIELDS, line.split("\t"), strict=True)) for line in lines[1:]]

    run.rows, run.out, run.tmp = rows, out, tmp_path
    run.busy_cmd = f'cp {tmp_path / "stat.busy"} "$HEAVY_STAT"; echo loud'
    run.quiet_cmd = f'cp {tmp_path / "stat.quiet"} "$HEAVY_STAT"; echo quiet'
    run.reset = f'cp {tmp_path / "stat.quiet"} "$HEAVY_STAT"'
    return run


def test_variants_run_interleaved_round_by_round_one_heavy_job_each(bench):
    done = bench("--rounds", "2", "a=echo A", "b=echo B")
    assert done.returncode == 0, done.stderr
    assert [(r["variant"], r["round"]) for r in bench.rows()] == [
        ("a", "1"),
        ("b", "1"),
        ("a", "2"),
        ("b", "2"),
    ]
    assert all(r["exit"] == "0" and r["flagged"] == "0" for r in bench.rows())
    assert (bench.out / "a-1.log").read_text().startswith("A\n")
    assert "while this --alone job ran" in (bench.out / "b-2.log").read_text()


def table_line(done, name):
    """The summary table's line for variant `name`, split into words."""
    (line,) = [line for line in done.stdout.splitlines() if line.split()[:1] == [name]]
    return line.split()


def test_a_warmup_runs_each_variant_once_and_counts_for_nothing(bench):
    done = bench("--rounds", "1", "--warmup", "a=echo A", "b=echo B")
    assert done.returncode == 0, done.stderr
    assert [(r["variant"], r["round"]) for r in bench.rows()] == [
        ("a", "warmup"),
        ("b", "warmup"),
        ("a", "1"),
        ("b", "1"),
    ]
    assert table_line(done, "a")[1] == "1"  # runs counted


def test_a_warmup_under_outside_load_is_not_run_again(bench):
    bench("--rounds", "1", "--warmup", "--prepare", bench.reset, f"a={bench.busy_cmd}")
    assert [(r["round"], r["attempt"]) for r in bench.rows()][:2] == [
        ("warmup", "1"),
        ("1", "1"),
    ]


def test_prepare_runs_before_each_run_with_the_variants_name(bench):
    order = bench.tmp / "order"
    done = bench(
        "--rounds",
        "2",
        "--prepare",
        f'echo "prep $BENCH_VARIANT" >> {order}',
        f"a=echo run-a >> {order}",
        f"b=echo run-b >> {order}",
    )
    assert done.returncode == 0, done.stderr
    assert order.read_text().split("\n")[:4] == ["prep a", "run-a", "prep b", "run-b"]


def test_a_failed_prepare_stops_the_bench(bench):
    done = bench("--prepare", "exit 3", "a=echo A")
    assert done.returncode == 1
    assert "heavy-bench: --prepare exited 3 before a round 1; stopping." in done.stderr
    assert not (bench.out / "a-1.log").exists()
    assert "results:" in done.stdout  # what it measured is still summed up


def test_a_run_under_outside_load_is_flagged_and_run_again(bench):
    # The prepare step makes each attempt start quiet, so the rerun sees the load too.
    done = bench("--rounds", "1", "--prepare", bench.reset, f"a={bench.busy_cmd}")
    rows = bench.rows()
    assert [(r["round"], r["attempt"], r["flagged"]) for r in rows] == [
        ("1", "1", "1"),
        ("1", "2", "1"),
    ]
    assert float(rows[0]["outside"]) > 0
    assert "flagged" in done.stdout


def test_no_rerun_keeps_a_flagged_run_as_it_is(bench):
    bench("--rounds", "1", "--no-rerun", f"a={bench.busy_cmd}")
    assert [(r["attempt"], r["flagged"]) for r in bench.rows()] == [("1", "1")]


def test_the_table_sums_up_only_clean_runs(bench):
    # The first run sees outside load, its rerun none: the rerun alone is counted.
    flip = bench.tmp / "flip"
    command = (
        f"if [ -e {flip} ]; then {bench.quiet_cmd}; "
        f"else touch {flip}; {bench.busy_cmd}; fi"
    )
    done = bench("--rounds", "1", f"a={command}")
    assert done.returncode == 0, done.stderr
    assert table_line(done, "a")[1] == "1"
    assert [r["flagged"] for r in bench.rows()] == ["1", "0"]


def test_a_failing_run_is_kept_counted_and_fails_the_bench(bench):
    done = bench("--rounds", "1", "a=exit 4", "b=echo B")
    assert done.returncode == 1
    assert [(r["variant"], r["exit"]) for r in bench.rows()] == [("a", "4"), ("b", "0")]
    assert table_line(done, "a")[1:3] == ["0", "-"]  # no clean run, no mean
    assert table_line(done, "a")[-1] == "1"  # failed


def test_a_failed_warmup_alone_doesnt_fail_the_bench(bench):
    flip = bench.tmp / "flip"
    done = bench(
        "--rounds", "1", "--warmup", f"a=[ -e {flip} ] || {{ touch {flip}; exit 5; }}"
    )
    assert done.returncode == 0, done.stderr
    assert [r["exit"] for r in bench.rows()] == ["5", "0"]


def test_out_may_be_nested_but_not_hold_results_already(bench):
    nested = bench.tmp / "deep" / "er"
    assert bench("--out", str(nested), "--rounds", "1", "a=echo A").returncode == 0
    again = bench("--out", str(nested), "--rounds", "1", "a=echo A")
    assert again.returncode == 2
    assert "already holds results" in again.stderr
    assert len((nested / "results.tsv").read_text().splitlines()) == 2  # kept


@pytest.mark.parametrize("rounds", ["0", "-1"])
def test_rounds_below_one_is_a_usage_error(bench, rounds):
    assert bench("--rounds", rounds, "a=echo A").returncode == 2


def test_inside_a_heavy_job_it_refuses_since_heavy_would_measure_nothing(bench):
    done = bench.with_env(HEAVY_SLOT="some job")("a=echo A")
    assert done.returncode == 2
    assert "inside a heavy job" in done.stderr


def test_stopping_the_bench_stops_its_heavy_job_and_sums_up_what_it_measured(bench):
    started = bench.tmp / "started"
    proc = subprocess.Popen(
        [
            str(SCRIPT),
            "--out",
            str(bench.out),
            "--rounds",
            "2",
            f"a=touch {started}; sleep 30",
        ],
        env=bench.env,
        cwd=bench.tmp,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "the run never started"
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM
    assert "results:" in out
    rows = bench.rows()
    assert [(r["round"], r["exit"]) for r in rows] == [("1", str(128 + signal.SIGTERM))]
    status = subprocess.run(
        [str(SCRIPT.parent / "heavy"), "status"],
        env=bench.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "running" not in status, status  # its heavy job is gone


@pytest.mark.parametrize("variants", [[], ["noequals"], ["=x"], ["a="], ["a=x", "a=y"]])
def test_variants_must_be_distinct_name_equals_command(bench, variants):
    assert bench(*variants).returncode == 2


def test_a_threshold_that_isnt_a_number_is_a_usage_error(bench):
    done = bench.with_env(HEAVY_ALONE_OUTSIDE="lots")("a=echo A")
    assert done.returncode == 2
    assert "HEAVY_ALONE_OUTSIDE" in done.stderr


def test_a_run_heavy_could_not_measure_is_kept_but_counts_for_nothing(bench):
    # With its /proc/stat stand-in gone, heavy writes no report.
    done = bench("--rounds", "1", "--no-rerun", 'a=rm "$HEAVY_STAT"')
    assert done.returncode == 0, done.stderr
    [row] = bench.rows()
    assert (row["wall"], row["flagged"]) == ("-", "0")
    assert "heavy gave no measure for a round 1" in done.stderr
    assert table_line(done, "a")[1:3] == ["0", "-"]


def test_a_run_quicker_than_the_tables_places_still_counts(tmp_path):
    # A copy of heavy-bench beside a stand-in heavy that reports a 4 ms run: as a
    # bare echo can take on GitHub's runner (#2), under 0.00 to 2 places.
    tool = tmp_path / "bin"
    tool.mkdir()
    (tool / "heavy-bench").write_text(SCRIPT.read_text())
    (tool / "heavy-bench").chmod(0o755)
    (tool / "heavy").write_text(
        '#!/bin/sh\nshift 2\n"$@"\necho "0.004 0.001 0.000" > "$HEAVY_ALONE_REPORT"\n'
    )
    (tool / "heavy").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "HEAVY_SLOT"}
    done = subprocess.run(
        [str(tool / "heavy-bench"), "--out", str(tmp_path / "out"), "a=echo A"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "gave no measure" not in done.stderr
    assert (tmp_path / "out" / "a-1.log").read_text() == "A\n"
    assert table_line(done, "a")[1] == "3"  # runs counted


def test_each_row_keeps_the_machines_speed_at_the_runs_start_and_end(tmp_path):
    # A stand-in heavy whose report adds the speed factors, as heavy's does once it
    # knows the machine's usual speed, or `-` for each it doesn't.
    tool = tmp_path / "bin"
    tool.mkdir()
    (tool / "heavy-bench").write_text(SCRIPT.read_text())
    (tool / "heavy-bench").chmod(0o755)
    (tool / "heavy").write_text(
        '#!/bin/sh\nshift 2\n"$@"\n'
        'echo "1.500 0.001 0.000 0.40 -" > "$HEAVY_ALONE_REPORT"\n'
    )
    (tool / "heavy").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "HEAVY_SLOT"}
    out = tmp_path / "out"

    done = subprocess.run(
        [str(tool / "heavy-bench"), "--out", str(out), "--rounds", "1", "a=true"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    header, row = (out / "results.tsv").read_text().splitlines()
    found = dict(zip(header.split("\t"), row.split("\t"), strict=True))
    assert (found["wall"], found["start_speed"], found["end_speed"]) == (
        "1.500",
        "0.40",
        "-",
    )


# A run's parts: lines `NAME SECONDS` it writes to $BENCH_TIMINGS.


def test_a_run_writes_its_parts_and_the_table_gives_each_parts_mean(bench):
    parts = 'printf "build 2.0\\ntest 1.0\\nnot a timing\\n" >> "$BENCH_TIMINGS"'
    other = 'printf "build 4.0\\ntest 3.0\\n" >> "$BENCH_TIMINGS"'
    done = bench("--rounds", "2", f"a={parts}", f"b={other}")

    assert done.returncode == 0, done.stderr
    assert [r["parts"] for r in bench.rows()] == [
        "build=2.0,test=1.0",
        "build=4.0,test=3.0",
    ] * 2
    assert "a parts: build 2.0, test 1.0" in done.stdout
    assert "b parts: build 4.0, test 3.0" in done.stdout


def test_a_run_that_writes_no_parts_leaves_the_column_empty(bench):
    done = bench("--rounds", "1", "a=true")

    assert done.returncode == 0, done.stderr
    assert bench.rows()[0]["parts"] == ""
    assert "parts:" not in done.stdout


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(bench):
    """bench's folder as a git repo with two commits of the file v: one, then two."""
    git(bench.tmp, "init", "-q", "-b", "main")
    (bench.tmp / ".gitignore").write_text("out/\nbusy\nmeminfo\nstat*\nheavy/\n")
    (bench.tmp / "v").write_text("one\n")
    git(bench.tmp, "add", "-A")
    git(bench.tmp, "commit", "-qm", "one")
    (bench.tmp / "v").write_text("two\n")
    git(bench.tmp, "commit", "-qam", "two")
    return bench.tmp


def bench_tree(repo, name):
    return repo.parent / f"{repo.name}-wt-bench-{name}"


def test_a_variant_at_a_commit_runs_in_a_worktree_there_removed_after(bench, repo):
    order = repo / "order"
    done = bench(
        "--rounds",
        "1",
        "--prepare",
        f'pwd >> "{order}"',
        "old@HEAD~1=cat v",
        "new@HEAD=cat v",
        "here=cat v",
    )

    assert done.returncode == 0, done.stderr
    assert (bench.out / "old-1.log").read_text().startswith("one\n")
    assert (bench.out / "new-1.log").read_text().startswith("two\n")
    assert (bench.out / "here-1.log").read_text().startswith("two\n")
    assert order.read_text().splitlines() == [
        str(bench_tree(repo, "old")),
        str(bench_tree(repo, "new")),
        str(repo),
    ]
    assert [r["variant"] for r in bench.rows()] == ["old", "new", "here"]
    assert not bench_tree(repo, "old").exists()
    assert not bench_tree(repo, "new").exists()
    assert git(repo, "worktree", "list").count("\n") == 0


def test_an_unknown_commit_is_a_usage_error_before_any_run(bench, repo):
    done = bench("--rounds", "1", "a=true", "b@nosuchrev=true")

    assert done.returncode == 2
    assert "nosuchrev" in done.stderr
    assert not (bench.out / "results.tsv").exists()


def test_a_bench_worktree_path_already_taken_is_refused(bench, repo):
    bench_tree(repo, "old").mkdir()

    done = bench("--rounds", "1", "old@HEAD~1=true")

    assert done.returncode == 2
    assert str(bench_tree(repo, "old")) in done.stderr


def test_a_variant_at_a_commit_outside_a_repo_is_a_usage_error(bench):
    done = bench("--rounds", "1", "old@HEAD=true")

    assert done.returncode == 2
    assert "repo" in done.stderr


def test_a_warmups_parts_count_for_nothing(bench):
    # Each run writes its own number as the part, so the warmup's is 1.
    counter = bench.tmp / "n"
    count = (
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); '
        f'echo "$n" > "{counter}"; echo "p $n" >> "$BENCH_TIMINGS"'
    )
    done = bench("--rounds", "2", "--warmup", f"a={count}")

    assert done.returncode == 0, done.stderr
    assert "a parts: p 2.5" in done.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only folder")
def test_a_worktree_git_cant_add_stops_the_bench_saying_why(bench, repo):
    # git can't record the worktree in a read-only .git/worktrees, so it fails.
    records = repo / ".git" / "worktrees"
    records.mkdir()
    records.chmod(0o555)
    try:
        done = bench("--rounds", "1", "old@HEAD~1=true")
    finally:
        records.chmod(0o755)

    assert done.returncode == 1, done.stderr
    assert "couldn't add" in done.stderr
    assert "ermission denied" in done.stderr  # git's own reason
    assert bench.rows() == []


def test_a_failing_run_still_removes_the_worktrees(bench, repo):
    done = bench("--rounds", "1", "old@HEAD~1=false")

    assert done.returncode == 1
    assert not bench_tree(repo, "old").exists()


def test_a_stopped_bench_removes_its_worktrees(bench, repo):
    started = repo / "started"
    proc = subprocess.Popen(
        [
            str(SCRIPT),
            "--out",
            str(bench.out),
            "--rounds",
            "2",
            f"old@HEAD~1=touch {started}; sleep 30",
        ],
        env=bench.env,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "the run never started"
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=60)

    assert proc.returncode == 128 + signal.SIGTERM, err
    assert "Traceback" not in err
    assert not bench_tree(repo, "old").exists()


def test_a_part_name_holding_the_columns_separators_is_left_out(bench):
    parts = 'printf "x=y 1.0\\na,b 2.0\\nok 3.0\\n" >> "$BENCH_TIMINGS"'
    done = bench("--rounds", "1", f"a={parts}")

    assert done.returncode == 0, done.stderr
    assert bench.rows()[0]["parts"] == "ok=3.0"
    assert "a parts: ok 3.0" in done.stdout


def test_from_a_folder_of_the_repo_a_variant_at_a_commit_runs_in_that_folder(
    bench, repo, monkeypatch
):
    (repo / "sub").mkdir()
    (repo / "sub/f").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "sub")
    done = subprocess.run(
        [
            str(SCRIPT),
            "--out",
            str(bench.out),
            "--rounds",
            "1",
            "old@HEAD=basename $PWD",
            "here=basename $PWD",
        ],
        env=bench.env,
        cwd=repo / "sub",
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (bench.out / "old-1.log").read_text().startswith("sub\n")
    assert (bench.out / "here-1.log").read_text().startswith("sub\n")


def test_help_says_to_change_a_files_bytes_not_touch_it():
    done = subprocess.run(
        [str(SCRIPT), "-h"], capture_output=True, text=True, check=False
    )

    assert "touch" in done.stdout and "sccache" in done.stdout
