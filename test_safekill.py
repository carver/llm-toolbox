"""safekill signals one (--one) or every (--all) matching process, never its own caller.

Every run goes through signal_guard_testing.py, which refuses a signal to any process this
test didn't start: a mutant of lib/procmatch.py's `matches` that matches every process
then fails the test instead of signalling every session on the machine (#17).
"""

import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import started_child

SCRIPT = Path(__file__).resolve().parent / "bin" / "safekill"
GUARD = Path(__file__).resolve().parent / "signal_guard_testing.py"


def guarded(tool: Path, root: int | None = None) -> list[str]:
    """The argv that runs `tool` able to signal only processes under `root`, by
    default this test's own."""
    root = os.getpid() if root is None else root
    return [sys.executable, str(GUARD), str(root), str(tool)]


def safekill(*args: str, shell: bool = False) -> subprocess.CompletedProcess[str]:
    if shell:
        # The pattern sits in the calling shell's own command line, which is what trips pkill -f.
        command = shlex.join(guarded(SCRIPT)) + " " + " ".join(args)
        return subprocess.run(
            ["bash", "-c", command + "; echo shell-survived"],
            text=True,
            capture_output=True,
            check=False,
        )
    return subprocess.run(
        [*guarded(SCRIPT), *args], text=True, capture_output=True, check=False
    )


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


SLEEP = "import time; time.sleep(300)"
IGNORE_TERM = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"


def start(
    sleepers: list[subprocess.Popen[bytes]], marker: str, code: str = SLEEP
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([sys.executable, "-c", code, marker])
    sleepers.append(proc)
    time.sleep(0.2 if code is IGNORE_TERM else 0.05)
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
def test_no_match_signals_nothing(
    sleepers: list[subprocess.Popen[bytes]], mode: str
) -> None:
    bystander = start(sleepers, uuid.uuid4().hex)
    result = safekill(mode, "-f", uuid.uuid4().hex)
    assert result.returncode == 1, result.stderr
    assert "no process matches" in result.stderr
    assert bystander.poll() is None


def test_the_guard_refuses_a_process_outside_its_tree(
    sleepers: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    # Both are this test's children, so a guard that let the signal through would
    # stop only the test's own sleeper.
    root, outside = start(sleepers, "root"), start(sleepers, "outside")
    tool = tmp_path / "kill.py"
    tool.write_text("import os, sys\nos.kill(int(sys.argv[1]), 15)\n")
    refused = subprocess.run(
        [*guarded(tool, root.pid), str(outside.pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 86, refused.stderr
    assert f"refused signal 15 to {outside.pid}" in refused.stderr
    assert outside.poll() is None
    sent = subprocess.run(
        [*guarded(tool), str(outside.pid)], text=True, capture_output=True, check=False
    )
    assert sent.returncode == 0, sent.stderr
    assert outside.wait(timeout=5) == -15


@pytest.mark.parametrize("mode", ["--one", "--all"])
def test_a_command_line_pattern_without_f_is_pointed_at_f(
    sleepers: list[subprocess.Popen[bytes]], mode: str
) -> None:
    marker = uuid.uuid4().hex
    proc = start(sleepers, marker)

    result = safekill(mode, f"{marker[:8]}.*{marker[-4:]}")

    assert result.returncode == 1
    assert "-f" in result.stderr and f"{proc.pid}" in result.stderr
    assert proc.poll() is None


def test_an_invalid_regex_name_still_says_nothing_matched() -> None:
    result = safekill("--one", "demo.py serve (")
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
    proc = start(sleepers, marker, IGNORE_TERM)
    result = safekill("--all", "-f", marker, "--wait", "0.5")
    assert result.returncode == 3
    assert f"{proc.pid} still running" in result.stderr


def test_all_waits_once_for_every_process_not_once_each(
    sleepers: list[subprocess.Popen[bytes]],
) -> None:
    marker = uuid.uuid4().hex
    for _ in range(3):
        start(sleepers, marker, IGNORE_TERM)
    began = time.monotonic()
    result = safekill("--all", "-f", marker, "--wait", "1")
    assert result.returncode == 3
    assert time.monotonic() - began < 2.5


@pytest.mark.parametrize(
    "args, problem",
    [(["-s", "BOGUS"], "signal"), (["-f", "("], "pattern")],
    ids=["unknown-signal", "bad-regex"],
)
def test_bad_input_is_a_usage_error(args: list[str], problem: str) -> None:
    result = safekill("--one", *args, *([] if "-f" in args else ["x"]))
    assert result.returncode == 2
    assert problem in result.stderr
    assert "Traceback" not in result.stderr


def test_a_shell_and_its_child_are_told_apart_in_the_refusal(shell_and_child) -> None:
    marker = uuid.uuid4().hex
    shell, child = shell_and_child(marker)
    result = safekill("--one", "-f", marker)
    assert result.returncode == 2
    child_line = next(
        line
        for line in result.stderr.splitlines()
        if line.strip().startswith(str(child))
    )
    assert f"(started by {shell})" in child_line
    assert "^" in result.stderr and "bgrun" in result.stderr
    assert Path(f"/proc/{child}").exists() and Path(f"/proc/{shell}").exists()


def test_without_f_a_parent_and_child_are_marked_but_not_told_to_anchor(
    tmp_path,
) -> None:
    name = "t" + uuid.uuid4().hex[:12]  # a process name is at most 15 characters
    script = tmp_path / name
    script.write_text(
        '#!/bin/bash\nif [ "$1" = child ]; then sleep 300; true; else "$0" child; true; fi\n'
    )
    script.chmod(0o755)
    parent = subprocess.Popen([str(script)], start_new_session=True)
    try:
        child = started_child(parent.pid, "child")
        result = safekill("--one", name)
        assert result.returncode == 2
        child_line = next(
            line
            for line in result.stderr.splitlines()
            if line.strip().startswith(str(child))
        )
        assert f"(started by {parent.pid})" in child_line
        assert "anchor" not in result.stderr
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait()
