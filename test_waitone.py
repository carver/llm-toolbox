"""waitone waits for exactly one matching process and never for its own caller."""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "waitone"


def command(*args: str, shell: bool = False) -> list[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is
        # what makes `while pgrep -f ...` loops wait forever. bash runs a lone
        # command in its own place, so a second one keeps the shell there.
        return ["bash", "-c", " ".join([str(SCRIPT), *args]) + "; exit $?"]
    return [str(SCRIPT), *args]


def waitone(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command(*args, shell=shell),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


class Waiting:
    """waitone running in the background, writing the PID it picks to a file. It runs
    in a process group of its own, since uv starts its Python as a child."""

    def __init__(self, picked: Path, *args: str, shell: bool) -> None:
        self.file = picked
        self.proc = subprocess.Popen(
            command(*args, shell=shell),
            env=os.environ | {"WAITONE_PICKED": str(picked)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def picked(self) -> int:
        """The PID waitone picked, once it has written it. Fails with waitone's own
        output when it exits first, or after 30 s."""
        deadline = time.monotonic() + 30
        while not (self.file.is_file() and self.file.read_text().endswith("\n")):
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                pytest.fail(f"waitone exited {self.proc.returncode} first: {out}{err}")
            assert time.monotonic() < deadline, "waitone never picked a process"
            time.sleep(0.05)
        return int(self.file.read_text())

    def finish(self) -> tuple[int, str, str]:
        out, err = self.proc.communicate(timeout=30)
        return self.proc.returncode, out, err


@pytest.fixture
def waiting(tmp_path: Path) -> Iterator[Callable[..., Waiting]]:
    """Start waitone in the background; every run's process group is killed and
    reaped at the end."""
    runs: list[Waiting] = []

    def start_waitone(*args: str, shell: bool = False) -> Waiting:
        run = Waiting(tmp_path / f"picked{len(runs)}", *args, shell=shell)
        runs.append(run)
        return run

    yield start_waitone
    for run in runs:
        with suppress(ProcessLookupError):
            os.killpg(run.proc.pid, signal.SIGKILL)
        run.proc.communicate()


def end(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    proc.wait()


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        end(proc)


def start(
    sleepers: list[subprocess.Popen[bytes]], marker: str, seconds: float
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})", marker]
    )
    sleepers.append(proc)
    time.sleep(0.05)
    return proc


# The match exits only once waitone has picked it: one that ended on a timer of its own
# was gone before a waitone slowed by a busy machine first looked.
def test_returns_once_the_match_exits(
    sleepers: list[subprocess.Popen[bytes]], waiting: Callable[..., Waiting]
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker, 300)
    run = waiting("-f", marker, "--timeout", "10")
    assert run.picked() == proc.pid
    end(proc)

    status, out, err = run.finish()

    assert status == 0, err
    assert f"{proc.pid} exited" in out


def test_the_calling_shell_is_never_a_match(
    sleepers: list[subprocess.Popen[bytes]], waiting: Callable[..., Waiting]
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker, 300)
    run = waiting("-f", f"'{marker}'", "--timeout", "10", shell=True)
    assert run.picked() == proc.pid
    end(proc)

    status, out, err = run.finish()

    assert status == 0, err
    assert f"{proc.pid} exited" in out


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


def test_a_long_command_line_is_listed_short_with_the_match_in_view(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    # A shell running a whole sourced env file matched once and listed 7k characters.
    marker = uuid.uuid4().hex
    padding = "x" * 5000
    first, second = start(sleepers, padding + marker, 300), start(sleepers, marker, 300)

    result = waitone("-f", marker, "--timeout", "5")

    assert result.returncode == 2
    listed = [line for line in result.stderr.splitlines() if str(first.pid) in line]
    assert len(listed) == 1 and len(listed[0]) < 300
    assert marker in listed[0] and "chars" in listed[0]
    assert str(second.pid) in result.stderr


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


def test_a_shell_and_its_child_are_told_apart_in_the_refusal(shell_and_child) -> None:
    marker = uuid.uuid4().hex
    shell, child = shell_and_child(marker)
    result = waitone("-f", marker, "--timeout", "1")
    assert result.returncode == 2
    child_line = next(
        line
        for line in result.stderr.splitlines()
        if line.strip().startswith(str(child))
    )
    assert f"(started by {shell})" in child_line
    assert "^" in result.stderr and "bgrun" in result.stderr
