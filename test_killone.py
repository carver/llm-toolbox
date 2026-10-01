"""killone signals exactly one matching process and never its own caller."""
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "killone"


def killone(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is what trips pkill -f.
        command = " ".join([str(SCRIPT), *args])
        return subprocess.run(["bash", "-c", command + "; echo shell-survived"], text=True, capture_output=True, check=False)
    return subprocess.run([str(SCRIPT), *args], text=True, capture_output=True, check=False)


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


def start(sleepers: list[subprocess.Popen[bytes]], marker: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", marker])
    sleepers.append(proc)
    time.sleep(0.05)
    return proc


def test_one_match_is_signalled(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = killone("-f", f"time.sleep.300. {marker}", "--wait", "5")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -15


def test_several_matches_signal_none(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    first, second = start(sleepers, marker), start(sleepers, marker)
    result = killone("-f", marker)
    assert result.returncode == 2
    assert str(first.pid) in result.stderr and str(second.pid) in result.stderr
    assert first.poll() is None and second.poll() is None


def test_no_match(sleepers: list[subprocess.Popen[bytes]]) -> None:
    assert killone("-f", uuid.uuid4().hex).returncode == 1


def test_the_calling_shell_is_never_a_match(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = killone("-f", f"'{marker}'", "--wait", "5", shell=True)
    assert "shell-survived" in result.stdout, result.stderr
    assert proc.wait(timeout=5) == -15


def test_exact_name_without_f(sleepers: list[subprocess.Popen[bytes]]) -> None:
    result = killone("-n", "no-such-process-name-xyz")
    assert result.returncode == 1


def test_dry_run_signals_nothing(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    result = killone("-f", marker, "--dry-run")
    assert result.returncode == 0 and f"would send SIGTERM to {proc.pid}" in result.stdout
    assert proc.poll() is None


def test_signal_by_name(sleepers: list[subprocess.Popen[bytes]]) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)
    assert killone("-f", marker, "-s", "KILL", "--wait", "5").returncode == 0
    assert proc.wait(timeout=5) == -9
