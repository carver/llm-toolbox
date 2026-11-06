"""mutate-check breaks code briefly with apply-edits blocks and says whether a command
caught it, always putting the code back."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "mutate-check"

CODE = "def double(n):\n    return n * 2\n"
TEST = "from calc import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
BREAK = """--- calc.py
<<<<<<<
    return n * 2
=======
    return n + 3
>>>>>>>
"""
HARMLESS = """--- calc.py
<<<<<<<
    return n * 2
=======
    return 2 * n
>>>>>>>
"""
PYTEST = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "test_calc.py",
]


def check(
    cwd: Path,
    edits: str,
    *cmd: str,
    options: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *options, "--", *cmd],
        input=edits,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text(CODE)
    (tmp_path / "test_calc.py").write_text(TEST)
    return tmp_path


def test_caught_mutation_exits_0_with_the_failing_assertion(repo: Path) -> None:
    result = check(repo, BREAK, *PYTEST)

    assert result.returncode == 0, result.stderr
    assert "caught" in result.stdout
    assert "E       assert 5 == 4" in result.stdout
    assert len(result.stdout.splitlines()) <= 4
    assert (repo / "calc.py").read_text() == CODE


def test_missed_mutation_exits_1(repo: Path) -> None:
    result = check(repo, HARMLESS, *PYTEST)

    assert result.returncode == 1
    assert "not caught" in result.stdout
    assert (repo / "calc.py").read_text() == CODE


def test_a_command_without_pytest_output_shows_its_last_line(repo: Path) -> None:
    result = check(repo, BREAK, "sh", "-c", "echo first; echo; echo 'it broke'; exit 4")

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1].strip() == "it broke"


def test_blocks_that_dont_apply_run_nothing(repo: Path) -> None:
    edits = BREAK.replace("n * 2", "n * 9")

    result = check(repo, edits, "touch", "ran")

    assert result.returncode == 2
    assert "old text not found" in result.stderr
    assert not (repo / "ran").exists()
    assert (repo / "calc.py").read_text() == CODE


def test_without_a_command_it_prints_usage(repo: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT)],
        input=BREAK,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "mutate-check" in result.stderr
    assert (repo / "calc.py").read_text() == CODE


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_code_is_put_back_when_it_is_stopped(repo: Path, sig: signal.Signals) -> None:
    proc = subprocess.Popen(
        [str(SCRIPT), "--", "sh", "-c", "touch started; sleep 30"],
        stdin=subprocess.PIPE,
        cwd=repo,
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(BREAK)
    proc.stdin.close()
    for _ in range(100):
        if (repo / "started").exists():
            break
        time.sleep(0.05)
    assert (repo / "calc.py").read_text() != CODE

    proc.send_signal(sig)
    proc.wait(timeout=10)

    assert (repo / "calc.py").read_text() == CODE


def test_help_shows_the_format(repo: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), "-h"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "<<<<<<<" in result.stdout


def test_a_command_that_wont_start_is_a_usage_error_not_a_miss(repo: Path) -> None:
    result = check(repo, BREAK, "no-such-command-xyz")

    assert result.returncode == 2
    assert "no-such-command-xyz" in result.stderr
    assert "Traceback" not in result.stderr
    assert (repo / "calc.py").read_text() == CODE


def test_output_that_isnt_utf8_is_still_read(repo: Path) -> None:
    result = check(repo, BREAK, "sh", "-c", r"printf 'bad \377 byte\n'; exit 1")

    assert result.returncode == 0
    assert "bad" in result.stdout


def test_a_file_the_command_deletes_is_put_back(repo: Path) -> None:
    (repo / "calc.py").chmod(0o640)

    result = check(repo, BREAK, "sh", "-c", "rm calc.py; exit 1")

    assert result.returncode == 0, result.stderr
    assert (repo / "calc.py").read_text() == CODE
    assert (repo / "calc.py").stat().st_mode & 0o777 == 0o640
    assert [p.name for p in repo.iterdir() if p.name.startswith(".")] == []


def test_a_child_ignoring_term_is_killed_once_the_grace_ends(repo: Path) -> None:
    proc = subprocess.Popen(
        [str(SCRIPT), "--", "sh", "-c", "trap '' TERM; touch started; sleep 30"],
        stdin=subprocess.PIPE,
        cwd=repo,
        text=True,
        env=os.environ | {"MUTATE_CHECK_GRACE": "0.2"},
    )
    assert proc.stdin is not None
    proc.stdin.write(BREAK)
    proc.stdin.close()
    for _ in range(100):
        if (repo / "started").exists():
            break
        time.sleep(0.05)

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=2)

    assert (repo / "calc.py").read_text() == CODE


RUST_BUILD_FAILURE = (
    "error: function `double` is never used\n"
    "error: could not compile `calc` (lib) due to 1 previous error\n"
    "error: command `cargo test --no-run --message-format=json-render-diagnostics` "
    "exited with code 101\n"
)


def test_a_build_that_fails_is_inconclusive_not_caught(repo: Path) -> None:
    (repo / "build.txt").write_text(RUST_BUILD_FAILURE)
    result = check(repo, BREAK, "sh", "-c", "cat build.txt; exit 101")

    assert result.returncode == 3, result.stdout
    assert "inconclusive" in result.stdout
    assert "could not compile" in result.stdout
    assert (repo / "calc.py").read_text() == CODE


def test_a_rust_type_error_is_inconclusive(repo: Path) -> None:
    result = check(
        repo, BREAK, "sh", "-c", "echo 'error[E0308]: mismatched types'; exit 101"
    )

    assert result.returncode == 3, result.stdout


def test_pytest_that_collects_nothing_is_inconclusive(repo: Path) -> None:
    broken = BREAK.replace("return n + 3", "return n +")

    result = check(repo, broken, *PYTEST)

    assert result.returncode == 3, result.stdout
    assert "inconclusive" in result.stdout
    assert (repo / "calc.py").read_text() == CODE


def test_a_command_that_runs_past_the_timeout_is_inconclusive(repo: Path) -> None:
    started = time.monotonic()
    result = check(
        repo,
        BREAK,
        "sleep",
        "30",
        options=("--timeout", "0.5"),
        env=os.environ | {"MUTATE_CHECK_GRACE": "0.2"},
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "0.5" in result.stdout and "hung" in result.stdout
    assert time.monotonic() - started < 10
    assert (repo / "calc.py").read_text() == CODE


def test_a_bad_timeout_is_a_usage_error(repo: Path) -> None:
    result = check(repo, BREAK, "true", options=("--timeout", "soon"))

    assert result.returncode == 2
    assert (repo / "calc.py").read_text() == CODE


def test_a_failing_test_that_prints_rust_errors_is_still_caught(repo: Path) -> None:
    output = (
        "error[E0599]: no method named `frob`\n"
        "test ui::compile_fail ... FAILED\n"
        "test result: FAILED. 3 passed; 1 failed\n"
    )
    (repo / "out.txt").write_text(output)
    result = check(repo, BREAK, "sh", "-c", "cat out.txt; exit 101")

    assert result.returncode == 0, result.stdout


def test_a_timeout_stops_the_commands_children_too(repo: Path) -> None:
    result = check(
        repo,
        BREAK,
        "sh",
        "-c",
        "trap '' TERM; sleep 30 & echo $! > child; wait",
        options=("--timeout", "0.5"),
        env=os.environ | {"MUTATE_CHECK_GRACE": "0.2"},
    )

    assert result.returncode == 3, result.stdout + result.stderr
    child = int((repo / "child").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert (repo / "calc.py").read_text() == CODE


def test_an_endless_timeout_is_a_usage_error(repo: Path) -> None:
    result = check(repo, BREAK, "true", options=("--timeout", "inf"))

    assert result.returncode == 2, result.stdout + result.stderr
