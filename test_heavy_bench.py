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
    assert (row["wall"], row["flagged"]) == ("0.00", "0")
    assert "heavy gave no measure for a round 1" in done.stderr
    assert table_line(done, "a")[1:3] == ["0", "-"]
