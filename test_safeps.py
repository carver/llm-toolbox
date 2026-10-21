"""safeps lists the processes matching a pattern, never its own caller."""

import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "safeps"
LINE = re.compile(r"^\s*(\d+)\s+(\d+:)?\d\d:\d\d\s+(.*)$")


def safeps(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is what trips pgrep -f.
        command = " ".join([str(SCRIPT), *args])
        return subprocess.run(
            ["bash", "-c", command + "; true"],
            text=True,
            capture_output=True,
            check=False,
        )
    return subprocess.run(
        [str(SCRIPT), *args], text=True, capture_output=True, check=False
    )


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


def sleeper(
    started: list[subprocess.Popen[bytes]], tag: str, *extra: str
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", tag, *extra]
    )
    started.append(proc)
    return proc


def listed_pids(out: str) -> list[int]:
    pids = []
    for line in out.splitlines():
        found = LINE.match(line)
        assert found, f"unexpected line: {line!r}"
        pids.append(int(found.group(1)))
    return pids


def test_lists_every_match_with_pid_elapsed_time_and_command(sleepers):
    tag = f"safeps-{uuid.uuid4().hex}"
    first, second = sleeper(sleepers, tag), sleeper(sleepers, tag)

    result = safeps("-f", tag)

    assert result.returncode == 0, result.stderr
    assert sorted(listed_pids(result.stdout)) == sorted([first.pid, second.pid])
    assert all(tag in line for line in result.stdout.splitlines())


def test_never_lists_the_shell_that_runs_it(sleepers):
    tag = f"safeps-{uuid.uuid4().hex}"
    target = sleeper(sleepers, tag)

    result = safeps("-f", tag, shell=True)

    assert listed_pids(result.stdout) == [target.pid]


def test_a_long_command_line_is_listed_short(sleepers):
    tag = f"safeps-{uuid.uuid4().hex}"
    sleeper(sleepers, "x" * 5000, tag)

    result = safeps("-f", tag)

    (line,) = result.stdout.splitlines()
    assert len(line) < 400
    assert tag in line and "chars]" in line


def test_nothing_matching_exits_1():
    result = safeps("-f", f"no-such-process-{uuid.uuid4().hex}")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no process matches" in result.stderr


def test_a_command_line_without_f_points_at_f(sleepers):
    tag = f"safeps-{uuid.uuid4().hex}"
    target = sleeper(sleepers, tag)

    result = safeps(tag)

    assert result.returncode == 1
    assert "with -f it would match" in result.stderr
    assert str(target.pid) in result.stderr


def test_a_bad_regex_is_a_usage_error():
    result = safeps("-f", "(")

    assert result.returncode == 2
    assert "pattern" in result.stderr


def test_a_command_line_with_newlines_stays_on_one_line(sleepers):
    tag = f"safeps-{uuid.uuid4().hex}"
    target = sleeper(sleepers, f"first line\nsecond line {tag}")

    result = safeps("-f", tag)

    assert listed_pids(result.stdout) == [target.pid]
    assert "first line second line" in result.stdout


def test_a_child_of_another_match_is_marked(shell_and_child):
    marker = uuid.uuid4().hex
    shell, child = shell_and_child(marker)
    result = safeps("-f", marker)
    assert result.returncode == 0
    lines = {
        pid: line
        for pid, line in zip(listed_pids(result.stdout), result.stdout.splitlines())
    }
    assert set(lines) == {shell, child}
    assert lines[child].endswith(f"(started by {shell})")
    assert "started by" not in lines[shell]
