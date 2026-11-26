"""own-pids runs a command in a PID namespace of its own, as the user who ran it, so
a command that signals every process it can see reaches only its own."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "own-pids"
NO_NAMESPACE = 125


def own_pids(
    *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
        timeout=60,
    )


def sudo_works() -> bool:
    try:
        done = subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False)
    except OSError:
        return False
    return done.returncode == 0


needs_sudo = pytest.mark.skipif(not sudo_works(), reason="needs passwordless sudo")


def python(code: str) -> tuple[str, ...]:
    return ("--", sys.executable, "-I", "-c", code)


@needs_sudo
def test_the_command_sees_only_itself_and_the_init() -> None:
    result = own_pids(
        *python(
            "import os; print(sorted(p for p in os.listdir('/proc') if p.isdigit()))"
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['1', '2']"


@needs_sudo
def test_check_passes_where_a_namespace_can_be_made() -> None:
    result = own_pids("--check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


# Sends SIGKILL to every process it can see, itself last. It refuses to unless it
# sees fewer than 10, all with pids under 100: outside a fresh PID namespace it
# would kill every process the user owns.
KILL_ALL = """\
import os, signal, sys
pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
if not (len(pids) < 10 and max(pids) < 100):
    sys.exit(f"refusing: {len(pids)} processes visible")
for pid in sorted(pids, key=lambda p: p == os.getpid()):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
"""


@needs_sudo
def test_a_command_that_kills_every_process_it_sees_spares_the_others() -> None:
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        result = own_pids(*python(KILL_ALL))

        assert bystander.poll() is None, "it killed a process outside"
    finally:
        bystander.kill()
        bystander.wait()
    assert result.returncode == 137, result.stdout + result.stderr


@needs_sudo
def test_the_command_keeps_the_users_ids_folder_environment_and_umask(
    tmp_path: Path,
) -> None:
    show = (
        "import os, json; mask = os.umask(0); print(json.dumps([os.getuid(),"
        " os.getgid(), sorted(os.getgroups()), os.getcwd(), mask,"
        " os.environ.get('PYTHONDONTWRITEBYTECODE'), os.environ.get('OWN_PIDS_T')]))"
    )
    env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "OWN_PIDS_T": "a b'c"}
    old = os.umask(0o027)
    try:
        result = own_pids(*python(show), env=env, cwd=tmp_path)
    finally:
        os.umask(old)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        os.getuid(),
        os.getgid(),
        sorted(os.getgroups()),
        str(tmp_path),
        0o027,
        "1",
        "a b'c",
    ]


@needs_sudo
def test_the_commands_exit_status_is_its_own() -> None:
    assert own_pids("sh", "-c", "exit 7").returncode == 7
    assert own_pids("--", "sh", "-c", "kill -TERM $$").returncode == 143


@needs_sudo
def test_what_the_command_leaves_running_ends_with_it() -> None:
    started = time.monotonic()

    # The sleep holds the output pipe: run() returns only once the sleep is gone.
    result = own_pids("sh", "-c", "sleep 600 & echo left")

    assert result.stdout == "left\n"
    assert time.monotonic() - started < 30


def started_own_pids(script: str, ready: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [str(SCRIPT), "--", "bash", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ | {"OWN_PIDS_GRACE": "0.5"},
    )
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "the command never started"
        time.sleep(0.02)
    return proc


@needs_sudo
def test_a_stop_signal_goes_on_to_the_command(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    proc = started_own_pids(
        f"trap 'echo got TERM; exit 3' TERM; touch {ready}; sleep 600 & wait", ready
    )

    proc.terminate()
    out, _ = proc.communicate(timeout=20)

    assert (proc.returncode, out) == (3, "got TERM\n")


@needs_sudo
def test_a_command_that_ignores_the_signal_is_killed_after_the_grace(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    proc = started_own_pids(f"trap '' TERM; touch {ready}; sleep 600 & wait", ready)

    proc.terminate()
    proc.communicate(timeout=20)

    assert proc.returncode == 137


def read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def descendants(pid: int) -> list[int]:
    found, level = [], [pid]
    while level:
        level = [
            int(child)
            for parent in level
            for task in Path(f"/proc/{parent}/task").glob("*")
            for child in read(f"{task}/children").split()
        ]
        found += level
    return found


def reading_the_environment(pid: int) -> bool:
    """Whether `pid` is the namespace's first process before it becomes bash."""
    argv = read(f"/proc/{pid}/cmdline").split("\0")
    return Path(argv[0]).name.startswith("python") and "-I" in argv


@needs_sudo
def test_a_stop_signal_before_the_command_starts_still_reaches_it() -> None:
    proc = subprocess.Popen(
        [str(SCRIPT), "--", "sleep", "600"],
        stdin=subprocess.DEVNULL,
        env=os.environ | {"OWN_PIDS_GRACE": "600"},
    )
    try:
        # The namespace's first process still reading the environment back, before
        # bash, the init, starts the command.
        deadline = time.monotonic() + 20
        while not any(map(reading_the_environment, descendants(proc.pid))):
            assert proc.poll() is None
            assert time.monotonic() < deadline, "never saw the namespace start"

        proc.terminate()

        assert proc.wait(timeout=20) == 143
    finally:
        # Killing own-pids alone would leave the command running: kill the init.
        for pid in descendants(proc.pid):
            try:
                if os.stat(f"/proc/{pid}").st_uid == os.getuid():
                    os.kill(pid, 9)
                    break
            except OSError:
                continue
        proc.wait()


@needs_sudo
def test_the_command_is_told_it_runs_in_a_namespace() -> None:
    result = own_pids(*python("import os; print(os.environ.get('OWN_PIDS'))"))

    assert result.stdout == "1\n", result.stderr


@pytest.fixture
def refusing_sudo(tmp_path: Path) -> dict[str, str]:
    """An environment whose sudo asks for a password, with temp files in tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    sudo.chmod(0o755)
    temp = tmp_path / "temp"
    temp.mkdir()
    return os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(temp)}


@pytest.mark.parametrize("args", [("--", "touch", "ran"), ("--check",)])
def test_without_sudo_it_says_no_namespace_was_made(
    refusing_sudo: dict[str, str], tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = own_pids(*args, env=refusing_sudo, cwd=tmp_path)

    assert result.returncode == NO_NAMESPACE
    assert result.stderr.splitlines() == [
        "sudo: a password is required",
        "own-pids: no PID namespace: sudo -n unshare exited 1 before the command started",
    ]
    assert not (tmp_path / "ran").exists()
    assert list((tmp_path / "temp").iterdir()) == [], "it left the environment's file"


def test_without_a_command_it_prints_usage() -> None:
    result = own_pids()

    assert result.returncode == NO_NAMESPACE
    assert "own-pids -- CMD" in result.stderr


def test_help_says_what_it_needs() -> None:
    result = own_pids("-h")

    assert result.returncode == 0
    assert "passwordless" in result.stdout


@needs_sudo
def test_the_init_reads_no_startup_file_but_the_command_gets_bash_env(
    tmp_path: Path,
) -> None:
    startup = tmp_path / "startup.sh"
    startup.write_text("echo sourced\n")
    env = os.environ | {"BASH_ENV": str(startup)}

    result = own_pids(*python("import os; print(os.environ['BASH_ENV'])"), env=env)

    assert result.stdout == f"{startup}\n", result.stdout + result.stderr
