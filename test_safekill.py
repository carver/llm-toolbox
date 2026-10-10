"""safekill signals one (--one) or every (--all) matching process, never its own caller."""

import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "safekill"


def safekill(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is what trips pkill -f.
        command = " ".join([str(SCRIPT), *args])
        return subprocess.run(
            ["bash", "-c", command + "; echo shell-survived"],
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


def start(
    sleepers: list[subprocess.Popen[bytes]], marker: str
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", marker]
    )
    sleepers.append(proc)
    time.sleep(0.05)
    return proc


def test_a_mode_is_required() -> None:
    result = safekill("-f", uuid.uuid4().hex)
    assert result.returncode == 2
    assert "--one" in result.stderr and "--all" in result.stderr


def test_one_and_all_are_exclusive() -> None:
    assert safekill("--one", "--all", "-f", uuid.uuid4().hex).returncode == 2


def test_one_signals_the_single_match(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = safekill("--one", "-f", f"time.sleep.300. {marker}", "--wait", "5")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -15


def test_one_with_several_matches_signals_none(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    first, second = start(sleepers, marker), start(sleepers, marker)
    result = safekill("--one", "-f", marker)
    assert result.returncode == 2
    assert str(first.pid) in result.stderr and str(second.pid) in result.stderr
    assert first.poll() is None and second.poll() is None


def test_all_signals_every_match(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    procs = [start(sleepers, marker) for _ in range(3)]
    result = safekill("--all", "-f", marker, "--wait", "5")
    assert result.returncode == 0, result.stderr
    assert [p.wait(timeout=5) for p in procs] == [-15, -15, -15]
    assert all(str(p.pid) in result.stdout for p in procs)


def test_all_with_one_match_signals_it(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    assert safekill("--all", "-f", marker, "--wait", "5").returncode == 0
    assert proc.wait(timeout=5) == -15


@pytest.mark.parametrize("mode", ["--one", "--all"])
def test_no_match(mode: str) -> None:
    result = safekill(mode, "-f", uuid.uuid4().hex)
    assert result.returncode == 1
    assert "no process matches" in result.stderr


@pytest.mark.parametrize("mode", ["--one", "--all"])
def test_the_calling_shell_is_never_a_match(
    sleepers: list[subprocess.Popen[bytes]], mode: str
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = safekill(mode, "-f", f"'{marker}'", "--wait", "5", shell=True)
    assert "shell-survived" in result.stdout, result.stderr
    assert proc.wait(timeout=5) == -15


def test_exact_name_without_f() -> None:
    assert safekill("--one", "no-such-process-name-xyz").returncode == 1


@pytest.mark.parametrize("mode", ["--one", "--all"])
def test_dry_run_signals_nothing(
    sleepers: list[subprocess.Popen[bytes]], mode: str
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = safekill(mode, "-f", marker, "--dry-run")
    assert result.returncode == 0
    assert f"would send SIGTERM to {proc.pid}" in result.stdout
    assert proc.poll() is None


def test_signal_by_name(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    assert safekill("--one", "-f", marker, "-s", "KILL", "--wait", "5").returncode == 0
    assert proc.wait(timeout=5) == -9


def test_all_reports_a_process_that_outlives_the_wait(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
            marker,
        ]
    )
    sleepers.append(proc)
    time.sleep(0.2)
    result = safekill("--all", "-f", marker, "--wait", "0.5")
    assert result.returncode == 3
    assert f"{proc.pid} still running" in result.stderr
