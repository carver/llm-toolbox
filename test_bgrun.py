"""bgrun starts a command detached, with its output in a log, and prints the command's own PID."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "bgrun"


def bgrun(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args], text=True, capture_output=True, check=False, cwd=cwd
    )


@pytest.fixture
def started() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_for(path: Path, text: str, seconds: float = 10) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and text in (content := path.read_text()):
            return content
        time.sleep(0.05)
    raise AssertionError(
        f"{text!r} never appeared in {path}: {path.read_text() if path.exists() else '(missing)'}"
    )


def cmdline(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")


def test_prints_the_commands_own_pid_and_logs_its_output(tmp_path, started):
    log = tmp_path / "out.log"
    script = "import sys, time; print('hello'); print('oops', file=sys.stderr); sys.stdout.flush(); time.sleep(300)"

    result = bgrun("-o", str(log), "--", sys.executable, "-c", script)

    assert result.returncode == 0, result.stderr
    pid = int(result.stdout)
    started.append(pid)
    assert cmdline(pid) == [sys.executable, "-c", script]
    content = wait_for(log, "oops")
    assert "hello" in content


def test_runs_in_its_own_session_and_outlives_the_calling_shell(tmp_path, started):
    log = tmp_path / "out.log"
    shell = subprocess.run(
        [
            "bash",
            "-c",
            f"{SCRIPT} -o {log} -- {sys.executable} -c 'import time; time.sleep(300)'",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    pid = int(shell.stdout)
    started.append(pid)

    assert Path(f"/proc/{pid}").exists()
    assert os.getsid(pid) == pid


def test_stdin_is_empty(tmp_path, started):
    log = tmp_path / "out.log"

    result = bgrun(
        "-o",
        str(log),
        "--",
        sys.executable,
        "-c",
        "import sys; print('read', repr(sys.stdin.read()))",
    )

    started.append(int(result.stdout))
    assert "read ''" in wait_for(log, "read")


def test_truncates_the_log_unless_append(tmp_path, started):
    log = tmp_path / "out.log"
    log.write_text("old line\n")
    printer = [sys.executable, "-c", "print('new line')"]

    started.append(int(bgrun("-o", str(log), "--", *printer).stdout))
    assert wait_for(log, "new line") == "new line\n"

    started.append(int(bgrun("--append", "-o", str(log), "--", *printer).stdout))
    assert wait_for(log, "new line\nnew line") == "new line\nnew line\n"


def test_runs_in_the_directory_given_by_c_with_the_log_relative_to_the_caller(
    tmp_path, started
):
    work = tmp_path / "work"
    work.mkdir()

    result = bgrun(
        "-C",
        str(work),
        "-o",
        "out.log",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.getcwd())",
        cwd=tmp_path,
    )

    started.append(int(result.stdout))
    assert wait_for(tmp_path / "out.log", str(work)).strip() == str(work)


def test_a_missing_command_exits_1_and_prints_no_pid(tmp_path):
    result = bgrun("-o", str(tmp_path / "out.log"), "--", "no-such-command-xyz")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no-such-command-xyz" in result.stderr


def test_a_missing_directory_exits_1(tmp_path):
    result = bgrun(
        "-C", str(tmp_path / "nope"), "-o", str(tmp_path / "out.log"), "--", "true"
    )

    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.parametrize(
    "args", [["--", "true"], ["-o", "x.log"], ["-o", "x.log", "--"]]
)
def test_a_log_and_a_command_are_required(tmp_path, args):
    result = bgrun(*args, cwd=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    "bad", [["--", "no-such-command-xyz"], ["-C", "/no/such/dir", "--", "true"]]
)
def test_a_failed_start_leaves_the_old_log_alone(tmp_path, bad):
    log = tmp_path / "out.log"
    log.write_text("previous run\n")

    result = bgrun("-o", str(log), *bad)

    assert result.returncode == 1
    assert log.read_text() == "previous run\n"


def test_a_missing_directory_is_named_as_the_problem(tmp_path):
    result = bgrun("-C", "/no/such/dir", "-o", str(tmp_path / "out.log"), "--", "true")

    assert "/no/such/dir" in result.stderr
    assert "'true'" not in result.stderr
