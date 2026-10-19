"""waitone waits for exactly one matching process and never for its own caller."""

import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "waitone"


def waitone(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is
        # what makes `while pgrep -f ...` loops wait forever.
        command = " ".join([str(SCRIPT), *args])
        return subprocess.run(
            ["bash", "-c", command],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    return subprocess.run(
        [str(SCRIPT), *args], text=True, capture_output=True, timeout=30, check=False
    )


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


def start(
    sleepers: list[subprocess.Popen[bytes]], marker: str, seconds: float
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})", marker]
    )
    sleepers.append(proc)
    time.sleep(0.05)
    return proc


def test_returns_once_the_match_exits(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker, 1)
    result = waitone("-f", marker, "--timeout", "10")
    assert result.returncode == 0, result.stderr
    assert f"{proc.pid} exited" in result.stdout


def test_the_calling_shell_is_never_a_match(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    start(sleepers, marker, 1)
    result = waitone("-f", f"'{marker}'", "--timeout", "10", shell=True)
    assert result.returncode == 0, result.stderr


def test_times_out_while_the_match_runs(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker, 300)
    result = waitone("-f", marker, "--timeout", "0.5")
    assert result.returncode == 4
    assert proc.poll() is None


def test_several_matches_wait_for_none(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    first, second = start(sleepers, marker, 300), start(sleepers, marker, 300)
    result = waitone("-f", marker, "--timeout", "5")
    assert result.returncode == 2
    assert str(first.pid) in result.stderr and str(second.pid) in result.stderr


def test_no_match() -> None:
    assert waitone("-f", uuid.uuid4().hex).returncode == 1


def test_a_command_line_pattern_without_f_is_pointed_at_f(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker, 300)

    result = waitone(marker, "--timeout", "1")

    assert result.returncode == 1
    assert "-f" in result.stderr and f"{proc.pid}" in result.stderr


def test_waits_for_a_pid_that_is_not_our_child(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    proc = start(sleepers, uuid.uuid4().hex, 1)
    result = waitone("--pid", str(proc.pid), "--timeout", "10")
    assert result.returncode == 0, result.stderr


def test_a_pid_that_already_exited_returns_at_once(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    proc = start(sleepers, uuid.uuid4().hex, 0)
    proc.wait()
    result = waitone("--pid", str(proc.pid), "--timeout", "1")
    assert result.returncode == 0


def test_needs_a_pattern_or_a_pid() -> None:
    assert waitone().returncode == 2
    assert waitone("x", "--pid", "1").returncode == 2
