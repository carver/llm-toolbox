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
    edits: str | None,
    *cmd: str,
    options: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run mutate-check with `edits` on stdin, or with none (None), as bgrun runs it."""
    return subprocess.run(
        [str(SCRIPT), *options, "--", *cmd],
        input=edits,
        stdin=subprocess.DEVNULL if edits is None else None,
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


def test_a_mutation_that_leaves_code_unused_says_why_and_how_to_keep_it_used(
    repo: Path,
) -> None:
    (repo / "build.txt").write_text(RUST_BUILD_FAILURE)
    result = check(repo, BREAK, "sh", "-c", "cat build.txt; exit 101")

    assert result.returncode == 3, result.stdout
    assert "function `double` is never used" in result.stdout
    assert "let _ =" in result.stdout, result.stdout


def test_the_cause_and_its_place_come_before_cargos_lines(repo: Path) -> None:
    (repo / "build.txt").write_text(
        "error: could not compile `calc` (lib) due to 1 previous error\n"
        "error: could not compile `calc` (lib test) due to 1 previous error\n"
        "error: command `cargo test --no-run` exited with code 101\n"
        "error: field `rate` is never read\n"
        "  --> src/limits.rs:12:5\n"
    )
    result = check(repo, BREAK, "sh", "-c", "cat build.txt; exit 101")

    shown = result.stdout.splitlines()
    assert "field `rate` is never read" in shown[1], result.stdout
    assert "src/limits.rs:12:5" in shown[2], result.stdout
    assert "let _ =" in result.stdout


def test_a_short_format_error_and_a_clippy_unused_lint_are_shown(repo: Path) -> None:
    (repo / "build.txt").write_text(
        "src/lib.rs:4:5: error: unused `self` argument\n"
        "error: could not compile `calc` (lib) due to 1 previous error\n"
    )
    result = check(repo, BREAK, "sh", "-c", "cat build.txt; exit 101")

    assert "src/lib.rs:4:5: error: unused `self` argument" in result.stdout
    assert "let _ =" in result.stdout


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
    deadline = time.monotonic() + 10
    while running(child):
        assert time.monotonic() < deadline, "the command's child outlived the timeout"
        time.sleep(0.05)
    assert (repo / "calc.py").read_text() == CODE


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_an_endless_timeout_is_a_usage_error(repo: Path) -> None:
    result = check(repo, BREAK, "true", options=("--timeout", "inf"))

    assert result.returncode == 2, result.stdout + result.stderr


# nextest's and cargo's own output, captured from cargo-nextest 0.9.151 and cargo 1.98
# on a throwaway crate, for a fake `cargo` first on PATH to print.
NEXTEST_BAD_FILTER = (
    "  error: invalid string\n"
    "   ╭────\n"
    " 1 │ test(=)\n"
    "   ·       ▲\n"
    "   ·       ╰── invalid string\n"
    "   ╰────\n"
    "\n"
    "error: failed to parse filterset\n"
)
NEXTEST_NO_TESTS = (
    "────────────\n"
    " Nextest run ID cf63cce1-fe12-49d1-a301-b99182380a84 with nextest profile: default\n"
    "    Starting 0 tests across 1 binary (2 tests skipped)\n"
    "────────────\n"
    "     Summary [   0.003s] 0 tests run: 0 passed, 2 skipped\n"
    "error: no tests to run\n"
    "(hint: use `--no-tests` to customize)\n"
)
NEXTEST_CRASHED = (
    "────────────\n"
    " Nextest run ID e8e8fc38-d12e-48ac-aa12-6599576fd817 with nextest profile: default\n"
    "    Starting 2 tests across 1 binary\n"
    "        PASS [   0.031s] (1/2) calc tests::doubles\n"
    "     SIGABRT [   0.036s] (2/2) calc tests::deep\n"
    "  stdout ───\n"
    "\n"
    "    running 1 test\n"
    "  stderr ───\n"
    "\n"
    "    thread 'tests::deep' (3993502) has overflowed its stack\n"
    "    fatal runtime error: stack overflow, aborting\n"
    "\n"
    "    (test aborted with signal 6: SIGABRT)\n"
    "\n"
    "  Cancelling due to test failure\n"
    "────────────\n"
    "     Summary [   0.043s] 2 tests run: 1 passed, 1 failed, 0 skipped\n"
    "     SIGABRT [   0.036s] (2/2) calc tests::deep\n"
    "error: test run failed\n"
)
# The same Summary as nextest colours it under CARGO_TERM_COLOR=always.
NEXTEST_CRASHED_IN_COLOUR = NEXTEST_CRASHED.replace(
    "     Summary [   0.043s] 2 tests run",
    "     \x1b[31;1mSummary\x1b[0m [   0.043s] \x1b[1m2\x1b[0m tests run",
)
# libtest refusing an option it doesn't know, from `cargo test -q -- --bogus`: cargo's
# line that a test binary failed follows, though no test ran.
LIBTEST_BAD_OPTION = (
    "error: Unrecognized option: 'bogus'\nerror: test failed, to rerun pass `--lib`\n"
)
# libtest prints no `test result:` line for a binary that dies under a test.
LIBTEST_CRASHED = (
    "\n"
    "running 2 tests\n"
    ".\n"
    "thread 'tests::deep' (3993413) has overflowed its stack\n"
    "fatal runtime error: stack overflow, aborting\n"
    "error: test failed, to rerun pass `--lib`\n"
    "\n"
    "Caused by:\n"
    "  process didn't exit successfully: `target/debug/deps/calc-8bfc7c3af6e6b5c2"
    " --quiet` (signal: 6, SIGABRT: process abort signal)\n"
)


@pytest.fixture
def cargo(repo: Path) -> dict[str, str]:
    """An environment whose `cargo` prints cargo.out and exits with cargo.status."""
    fake = repo / "fakebin"
    fake.mkdir()
    (fake / "cargo").write_text(
        '#!/bin/sh\ncat cargo.out\nexit "$(cat cargo.status)"\n'
    )
    (fake / "cargo").chmod(0o755)
    return os.environ | {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}


def cargo_prints(repo: Path, output: str, status: int) -> None:
    (repo / "cargo.out").write_text(output)
    (repo / "cargo.status").write_text(str(status))


NEXTEST = ("cargo", "nextest", "run", "--cargo-quiet")


@pytest.mark.parametrize(
    ("command", "output", "status", "shown"),
    [
        (
            (*NEXTEST, "-E", "test(=)"),
            NEXTEST_BAD_FILTER,
            94,
            "error: failed to parse filterset",
        ),
        (
            (*NEXTEST, "-E", "test(=nomatch)"),
            NEXTEST_NO_TESTS,
            4,
            "error: no tests to run",
        ),
        (
            ("cargo", "test", "-q", "--", "--bogus"),
            LIBTEST_BAD_OPTION,
            101,
            "error: Unrecognized option: 'bogus'",
        ),
    ],
    ids=["nextest bad filter", "nextest no tests", "libtest bad option"],
)
def test_a_rust_test_run_that_ran_no_test_is_inconclusive_not_caught(
    repo: Path,
    cargo: dict[str, str],
    command: tuple[str, ...],
    output: str,
    status: int,
    shown: str,
) -> None:
    cargo_prints(repo, output, status)

    result = check(repo, BREAK, *command, env=cargo)

    assert result.returncode == 3, result.stdout
    assert f"inconclusive: the command exited {status}" in result.stdout
    assert "judged the mutation" in result.stdout
    assert f"  {shown}" in result.stdout.splitlines()
    assert len(result.stdout.splitlines()) <= 4, result.stdout
    assert (repo / "calc.py").read_text() == CODE


@pytest.mark.parametrize(
    "command",
    [
        ["cargo", "test", "-q"],
        ["cargo", "+nightly", "t"],
        ["cargo", "-q", "test"],
        ["fakebin/cargo", "test"],
        ["sh", "-c", "cd . && cargo nextest run --cargo-quiet"],
    ],
)
def test_each_way_of_writing_a_rust_test_run_is_read_as_one(
    repo: Path, cargo: dict[str, str], command: list[str]
) -> None:
    cargo_prints(repo, "error: unexpected argument '--tset' found\n", 1)

    result = check(repo, BREAK, *command, env=cargo)

    assert result.returncode == 3, result.stdout


@pytest.mark.parametrize(
    "command", [["cargo", "run", "--bin", "test"], ["cargo", "tree"]]
)
def test_a_cargo_command_that_isnt_a_test_run_is_still_caught(
    repo: Path, cargo: dict[str, str], command: list[str]
) -> None:
    cargo_prints(repo, "error: the check found a mismatch\n", 1)

    result = check(repo, BREAK, *command, env=cargo)

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("command", "output", "status"),
    [
        (NEXTEST, NEXTEST_CRASHED, 100),
        (NEXTEST, NEXTEST_CRASHED_IN_COLOUR, 100),
        (("cargo", "test", "-q"), LIBTEST_CRASHED, 101),
    ],
    ids=["nextest", "nextest in colour", "libtest"],
)
def test_a_rust_test_that_fails_or_crashes_is_caught(
    repo: Path,
    cargo: dict[str, str],
    command: tuple[str, ...],
    output: str,
    status: int,
) -> None:
    cargo_prints(repo, output, status)

    result = check(repo, BREAK, *command, env=cargo)

    assert result.returncode == 0, result.stdout
    assert f"caught: the command exited {status}" in result.stdout


def test_edits_from_a_file_break_the_code_with_stdin_closed(repo: Path) -> None:
    blocks = repo.parent / "break.edits"
    blocks.write_text(BREAK)

    result = check(repo, None, *PYTEST, options=("--edits", str(blocks)))

    assert result.returncode == 0, result.stderr
    assert "E       assert 5 == 4" in result.stdout
    assert (repo / "calc.py").read_text() == CODE


@pytest.mark.parametrize("edits_first", [True, False])
def test_edits_and_timeout_go_in_either_order(repo: Path, edits_first: bool) -> None:
    blocks = repo.parent / "break.edits"
    blocks.write_text(BREAK)
    edits, timeout = ("--edits", str(blocks)), ("--timeout", "60")

    options = edits + timeout if edits_first else timeout + edits
    result = check(repo, None, *PYTEST, options=options)

    assert result.returncode == 0, result.stderr


def test_an_edits_file_that_cant_be_read_runs_nothing(repo: Path) -> None:
    missing = repo.parent / "missing.edits"

    result = check(repo, None, "touch", "ran", options=("--edits", str(missing)))

    assert result.returncode == 2
    assert "missing.edits" in result.stderr
    assert not (repo / "ran").exists()


def test_an_edits_file_that_isnt_utf8_is_a_usage_error(repo: Path) -> None:
    blocks = repo.parent / "latin1.edits"
    blocks.write_bytes(BREAK.encode() + b"caf\xe9\n")

    result = check(repo, None, "touch", "ran", options=("--edits", str(blocks)))

    assert result.returncode == 2
    assert "latin1.edits" in result.stderr and "UTF-8" in result.stderr
    assert not (repo / "ran").exists()


def test_help_says_to_use_edits_under_bgrun(repo: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), "-h"], cwd=repo, text=True, capture_output=True, check=False
    )

    assert "--edits FILE" in result.stdout and "bgrun" in result.stdout
