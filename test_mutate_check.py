"""mutate-check breaks code briefly with apply-edits blocks and says whether a command
caught it, always putting the code back. With --diff it makes the mutants itself, from
the Python and shell lines a git diff changed."""

import os
import shutil
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
# The same size as CODE, like BREAK, but double(3) is 5 here, not 6.
SAME_SIZE = BREAK.replace("return n + 3", "return n + 2")
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


# -E: a Python that ignores PYTHON* variables writes the mutant's .pyc anyway.
@pytest.mark.parametrize("flags", [(), ("-E",)], ids=["plain", "-E"])
def test_a_same_size_mutants_bytecode_is_not_left_for_later_runs(
    repo: Path, flags: tuple[str, ...]
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    imports = (sys.executable, *flags, "-c", "import calc; assert calc.double(3) == 6")

    result = check(repo, SAME_SIZE, *imports, env=env)
    after = subprocess.run(
        [sys.executable, "-c", "import calc; print(calc.double(3))"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert after.stdout.strip() == "6", after.stdout + after.stderr


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


# pytest 9.1.1 with pytest-xdist 3.8.0 and -x, when a test fails: it exits 2, the
# status of an interrupted run, though a test ran and failed.
XDIST_STOPPED_AFTER_A_FAILURE = (
    "FAILED test_calc.py::test_double - assert 5 == 4\n"
    "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    "!!!!!!!!!!!! xdist.dsession.Interrupted: stopping after 1 failures !!!!!!!!!!!!!\n"
    "1 failed, 1 passed in 0.91s\n"
)


def test_pytest_stopped_by_x_after_a_failure_is_caught(repo: Path) -> None:
    fake = repo / "fakebin"
    fake.mkdir()
    (fake / "pytest").write_text("#!/bin/sh\ncat pytest.out\nexit 2\n")
    (fake / "pytest").chmod(0o755)
    (repo / "pytest.out").write_text(XDIST_STOPPED_AFTER_A_FAILURE)
    env = os.environ | {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}

    result = check(repo, BREAK, "pytest", "-x", "-n", "2", env=env)

    assert result.returncode == 0, result.stdout
    assert "caught: the command exited 2" in result.stdout


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


# --diff mode, in a git repo whose one commit holds calc.py as BASE_CALC.

BASE_CALC = "def double(n):\n    return n * 2\n"
# Lines 5 to 8 are new: a default, a condition and two returns with strings.
LABEL = BASE_CALC + (
    "\n\n"
    "def label(n, loud=False):\n"
    "    if loud:\n"
    '        return "BIG"\n'
    '    return "small"\n'
)
SUMMARY = (
    "mutate-check: {} caught, {} survived, {} inconclusive, {} skipped (didn't parse)"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("template")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@example.com")
    (root / "calc.py").write_text(BASE_CALC)
    git(root, "add", "calc.py")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def project(template: Path, tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(template, root, symlinks=True)
    return root


def mutate(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def diff_check(
    cwd: Path,
    *cmd: str,
    base: str = "HEAD",
    options: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return mutate(cwd, "--diff", base, *options, "--", *cmd, env=env)


def python(code: str) -> tuple[str, ...]:
    """A command that runs `code` after importing everything from calc."""
    return (sys.executable, "-c", f"from calc import *; {code}")


def add(root: Path, files: dict[str, str]) -> None:
    """Write the files and stage them, so git diff sees the new ones."""
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", *files)


def test_diff_lists_the_mutants_the_command_lets_through(project: Path) -> None:
    (project / "calc.py").write_text(LABEL)
    (project / "calc.py").chmod(0o640)

    result = diff_check(project, *python("assert label(1) == 'small'"))

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        'calc.py:7: deleted: return "BIG"',
        "calc.py:7: \"BIG\" -> 'XXBIGXX'",
        SUMMARY.format(4, 2, 0, 0),
    ]
    assert (project / "calc.py").read_text() == LABEL
    assert (project / "calc.py").stat().st_mode & 0o777 == 0o640


def test_diff_exits_0_and_prints_only_the_summary_when_all_are_caught(
    project: Path,
) -> None:
    (project / "calc.py").write_text(LABEL)

    result = diff_check(
        project, *python("assert label(1) == 'small' and label(1, True) == 'BIG'")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [SUMMARY.format(6, 0, 0, 0)]


TOOL = """\
def tool(path, items, flag):
    out = run(path)
    return out
"""


def test_diff_mutates_only_lines_changed_since_base_through_a_move(
    project: Path,
) -> None:
    add(project, {"tool.py": TOOL})
    git(project, "commit", "-q", "-m", "tool")
    base = git(project, "rev-parse", "HEAD").strip()
    (project / "pkg").mkdir()
    git(project, "mv", "tool.py", "pkg/tool.py")
    (project / "pkg/tool.py").write_text(TOOL + "LIMIT = low\n")
    git(project, "commit", "-q", "-am", "move tool and add a limit")
    with (project / "pkg/tool.py").open("a") as f:
        f.write("RATE = fast\n")

    result = diff_check(project, "true", base=base)

    assert sorted(result.stdout.splitlines()[:-1]) == [
        "pkg/tool.py:4: deleted: LIMIT = low",
        "pkg/tool.py:5: deleted: RATE = fast",
    ], result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == SUMMARY.format(0, 2, 0, 0)


# A line or two for each Python operator, and the code they leave alone: imports,
# TYPE_CHECKING and __main__ checks, strings in a module's data tables, digests and
# URLs, annotations, docstrings, f-strings, strings with spaces in them (messages),
# assert, break, pass, and a return or continue that ends its function or loop.
OPERATORS = '''\
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

PINS = [("tool", "1.0")]
DIGEST = "0123456789abcdef0123456789abcdef"
URL = "https://example.com/x"


def tool(path: "Path", items, flag) -> "Result":
    """Gives each operator something to mutate."""
    os.chmod(path, 0o755)
    if flag == "x86_64":
        return None
    while items and not flag:
        items.pop()
    ok = path is not None
    run(path, check=True, filter="data")
    root = path / "keep"
    say(f"made {root}", "a message")
    return ok


def nothing(items):
    for item in items:
        assert item
        if not item:
            break
        continue
    pass
    return None


@dataclass(frozen=True)
class Point:
    x: "int" = 0


def wide(a, b):
    print(a, flush=True)
    return (a
            and b)


if __name__ == "__main__":
    main()
'''


def test_diff_makes_one_mutant_per_operator_and_site_in_python(
    project: Path,
) -> None:
    add(project, {"tool.py": OPERATORS})

    result = diff_check(project, "true")

    assert sorted(result.stdout.splitlines()[:-1]) == sorted(
        [
            'tool.py:7: deleted: PINS = [("tool", "1.0")]',
            'tool.py:8: deleted: DIGEST = "0123456789abcdef0123456789abcdef"',
            'tool.py:9: deleted: URL = "https://example.com/x"',
            "tool.py:14: deleted: os.chmod(path, 0o755)",
            'tool.py:15: negated: flag == "x86_64"',
            "tool.py:15: \"x86_64\" -> 'XXx86_64XX'",
            "tool.py:16: deleted: return None",
            "tool.py:17: negated: items and not flag",
            "tool.py:17: dropped from the and: items",
            "tool.py:17: dropped from the and: not flag",
            "tool.py:18: deleted: items.pop()",
            "tool.py:19: is not -> is: path is not None",
            "tool.py:19: deleted: ok = path is not None",
            'tool.py:20: deleted: run(path, check=True, filter="data")',
            "tool.py:20: check=True -> check=False",
            "tool.py:20: \"data\" -> 'XXdataXX'",
            "tool.py:21: \"keep\" -> 'XXkeepXX'",
            'tool.py:21: deleted: root = path / "keep"',
            'tool.py:22: deleted: say(f"made {root}", "a message")',
            "tool.py:23: deleted: return ok",
            "tool.py:29: negated: not item",
            'tool.py:38: deleted: x: "int" = 0',
            "tool.py:42: deleted: print(a, flush=True)",
            "tool.py:43: deleted: return (a and b)",
            "tool.py:43: dropped from the and: a",
            "tool.py:44: dropped from the and: b",
            "tool.py:48: deleted: main()",
        ]
    ), result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == SUMMARY.format(0, 27, 0, 0)
    assert result.returncode == 1


SHELL = """\
#!/bin/sh
set -eu
if [ "$1" = "x" ]; then
  echo yes
fi
[ "$2" != y ] && echo no
while read -r line; do
  echo "$line"
done
cat <<EOF
body
EOF
greet() {
  echo hi
}
printf '%s\\n' \\
  one two
dir="${DIR:-keep/data}"
[ -d "$dir" ] || {
  echo "no $dir" >&2
}
exit 0
"""


def test_diff_makes_one_mutant_per_operator_and_site_in_shell(
    project: Path,
) -> None:
    add(project, {"run.sh": SHELL})

    result = diff_check(project, "true")

    assert sorted(result.stdout.splitlines()[:-1]) == sorted(
        [
            "run.sh:2: deleted: set -eu",
            'run.sh:3: negated: [ "$1" = "x" ]',
            "run.sh:4: deleted: echo yes",
            'run.sh:6: deleted: [ "$2" != y ] && echo no',
            'run.sh:6: != -> =: [ "$2" != y ]',
            "run.sh:7: negated: read -r line",
            'run.sh:8: deleted: echo "$line"',
            "run.sh:14: deleted: echo hi",
            "run.sh:16: deleted: printf '%s\\n' one two",
            'run.sh:18: deleted: dir="${DIR:-keep/data}"',
            "run.sh:18: ${DIR:-keep/data} -> ${DIR:-XXkeep/dataXX}",
            'run.sh:19: deleted: [ -d "$dir" ] || { echo "no $dir" >&2 }',
            'run.sh:20: deleted: echo "no $dir" >&2',
        ]
    ), result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == SUMMARY.format(0, 13, 0, 0)


def test_diff_mutates_code_files_by_name_or_shebang_and_skips_tests(
    project: Path,
) -> None:
    add(
        project,
        {
            "bin/tool": "#!/usr/bin/env python3\nprint(1)\n",
            "bin/uvtool": "#!/usr/bin/env -S uv run --quiet --script\nprint(2)\n",
            "bin/script": "#!/bin/bash\necho hi\n",
            "bin/notes": "print(3)\n",
            "tests/helper.py": "print(4)\n",
            "test_calc.py": "print(5)\n",
            "calc_test.py": "print(6)\n",
            "conftest.py": "print(7)\n",
            "skill_testing.py": "print(8)\n",
            "bin/test-run.sh": "echo 9\n",
            "notes.md": "print(10)\n",
            "data.json": '{"a": "b"}\n',
        },
    )
    (project / "untracked.py").write_text("print(11)\n")

    result = diff_check(project, "true")

    untracked = "untracked, so not mutated (git add -N brings one in): untracked.py"
    assert result.stdout.splitlines() == [
        f"mutate-check: {untracked}",
        "bin/script:2: deleted: echo hi",
        "bin/tool:2: deleted: print(1)",
        "bin/uvtool:2: deleted: print(2)",
        SUMMARY.format(0, 3, 0, 0),
    ], result.stdout + result.stderr


def test_a_code_file_that_doesnt_parse_or_isnt_utf8_is_named(project: Path) -> None:
    add(project, {"broken.py": "def f(:\n", "latin1.py": ""})
    (project / "latin1.py").write_bytes(b"NAME = 'caf\xe9'\n")

    result = diff_check(project, "touch", "ran")

    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "mutate-check: doesn't parse, so not mutated: broken.py",
        "mutate-check: isn't UTF-8, so not mutated: latin1.py",
        "mutate-check: nothing to mutate in the changed code it could read",
    ]
    assert not (project / "ran").exists()


def test_pythons_warnings_about_the_code_stay_out_of_the_output(project: Path) -> None:
    # Deleting the call leaves the bad escape in the mutant, for the parse check.
    add(project, {"tool.py": 'PATTERN = re.compile("\\d+")\nrun(PATTERN)\n'})

    result = diff_check(project, "true")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr == ""


def test_max_takes_each_statements_first_mutant_and_messages_last(
    project: Path,
) -> None:
    (project / "calc.py").write_text(
        BASE_CALC + "\n\ndef f(path):\n    say('starting a run')\n"
        "    run(path,\n        'keep')\n    return path\n"
    )

    result = diff_check(project, "true", options=("--max", "2"))

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "calc.py:7: deleted: run(path, 'keep')",
        "calc.py:9: deleted: return path",
        SUMMARY.format(0, 2, 0, 0) + "; 2 left out by --max 2",
    ], result.stdout + result.stderr


def test_a_command_that_fails_unmutated_runs_no_mutant(project: Path) -> None:
    (project / "calc.py").write_text(LABEL)
    log = project.parent / "runs.log"

    result = diff_check(project, "sh", "-c", f"echo run >> {log}; echo broken; exit 1")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "fails with no mutation" in result.stdout
    assert "  broken" in result.stdout.splitlines()
    assert log.read_text() == "run\n"
    assert (project / "calc.py").read_text() == LABEL


WAIT = BASE_CALC + "\n\ndef wait(n):\n    while n > 0:\n        n -= 1\n    return n\n"


def test_a_mutant_that_hangs_is_stopped_after_ten_unmutated_runs(
    project: Path,
) -> None:
    (project / "calc.py").write_text(WAIT)
    env = os.environ | {"MUTATE_CHECK_MIN_TIMEOUT": "0.5", "MUTATE_CHECK_GRACE": "0.2"}
    started = time.monotonic()

    result = diff_check(project, *python("assert wait(3) == 0"), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [SUMMARY.format(2, 0, 1, 0)]
    assert time.monotonic() - started < 30
    assert (project / "calc.py").read_text() == WAIT


def test_a_run_that_judges_no_mutant_exits_3(project: Path) -> None:
    add(
        project,
        {
            "test_calc.py": "from calc import TWICE\n\n\ndef test_twice():\n"
            "    assert TWICE == 4\n",
            "calc.py": BASE_CALC + "RATE = 2\nTWICE = RATE * 2\n",
        },
    )

    result = diff_check(project, *PYTEST)

    # Each mutant breaks the import, so pytest collects nothing: inconclusive.
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.splitlines() == [SUMMARY.format(0, 0, 2, 0)]


def test_a_command_that_rewrites_the_code_stops_the_run(project: Path) -> None:
    (project / "calc.py").write_text(LABEL)

    result = diff_check(project, "sh", "-c", "echo '# formatted' >> calc.py")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "rewrote calc.py" in result.stderr
    assert (project / "calc.py").read_text() == LABEL


def test_diff_against_a_branch_that_moved_takes_its_merge_base(project: Path) -> None:
    git(project, "switch", "-q", "-c", "work")
    git(project, "switch", "-q", "main")
    (project / "calc.py").write_text(BASE_CALC.replace("n * 2", "n * 3"))
    git(project, "commit", "-q", "-am", "main moves on")
    git(project, "switch", "-q", "work")
    (project / "calc.py").write_text(LABEL)

    result = diff_check(project, "true", base="main")

    assert "calc.py:2:" not in result.stdout, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == SUMMARY.format(0, 6, 0, 0)


def test_without_bash_shell_files_are_named_not_mutated(
    project: Path, tmp_path: Path
) -> None:
    add(project, {"run.sh": "#!/bin/sh\necho hi\n", "tool.py": "print(1)\n"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, target in [("python3", sys.executable), ("git", shutil.which("git"))]:
        assert target is not None
        (bin_dir / name).symlink_to(target)

    result = diff_check(
        project,
        sys.executable,
        "-c",
        "pass",
        env=os.environ | {"PATH": str(bin_dir)},
    )

    assert result.stdout.splitlines() == [
        "mutate-check: no bash to check shell mutants, so not mutated: run.sh",
        "tool.py:1: deleted: print(1)",
        SUMMARY.format(0, 1, 0, 0),
    ], result.stdout + result.stderr


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_diff_puts_the_code_back_when_stopped_mid_run(
    project: Path, sig: signal.Signals
) -> None:
    (project / "calc.py").write_text(LABEL)
    log, started = project.parent / "runs.log", project.parent / "started"
    # The baseline and the first mutant pass; the second mutant's run waits.
    third_waits = (
        f'echo run >> {log}; [ "$(wc -l < {log})" -lt 3 ] || '
        f"{{ touch {started}; sleep 30; }}"
    )
    proc = subprocess.Popen(
        [str(SCRIPT), "--diff", "HEAD", "--", "sh", "-c", third_waits],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        cwd=project,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not started.exists():
        assert time.monotonic() < deadline, "the second mutant never ran"
        time.sleep(0.05)
    assert (project / "calc.py").read_text() != LABEL

    proc.send_signal(sig)
    out, _ = proc.communicate(timeout=10)

    assert proc.returncode == 128 + sig
    assert (project / "calc.py").read_text() == LABEL
    assert out.splitlines() == ["calc.py:5: False -> True", SUMMARY.format(0, 1, 0, 0)]


def test_a_shell_mutant_that_doesnt_parse_is_skipped_not_run(project: Path) -> None:
    add(project, {"run.sh": '#!/bin/sh\nmsg="first\nsecond"\n'})
    log = project.parent / "runs.log"

    result = diff_check(project, "sh", "-c", f"echo run >> {log}")

    # No mutant judged: the same status as a run with only inconclusive ones.
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.splitlines() == [SUMMARY.format(0, 0, 0, 2)]
    assert log.read_text() == "run\n"


@pytest.mark.parametrize(
    ("options", "named"),
    [
        (("--diff", "no-such-rev"), "no-such-rev"),
        (("--diff",), "--diff"),
        (("--max", "3"), "--max"),
        (("--diff", "HEAD", "--max", "0"), "--max"),
        (("--diff", "HEAD", "--max", "lots"), "--max"),
        (("--diff", "HEAD", "--edits", "blocks.edits"), "--edits"),
    ],
)
def test_a_bad_diff_option_is_a_usage_error_that_runs_nothing(
    project: Path, options: tuple[str, ...], named: str
) -> None:
    (project / "calc.py").write_text(LABEL)

    result = mutate(project, *options, "--", "touch", "ran")

    assert result.returncode == 2, result.stdout + result.stderr
    assert named in result.stderr
    assert not (project / "ran").exists()
    assert (project / "calc.py").read_text() == LABEL


def test_diff_outside_a_git_repo_is_a_usage_error(tmp_path: Path) -> None:
    result = diff_check(tmp_path, "touch", "ran")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "git" in result.stderr
    assert not (tmp_path / "ran").exists()


def test_a_diff_with_no_code_lines_runs_nothing(project: Path) -> None:
    add(project, {"README.md": "# calc\n", "test_calc.py": "def test(): pass\n"})

    result = diff_check(project, "touch", "ran")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no Python or shell line" in result.stdout
    assert "only uncommitted changes count" in result.stdout
    assert not (project / "ran").exists()


def test_help_shows_the_diff_mode_with_an_example() -> None:
    result = subprocess.run(
        [str(SCRIPT), "-h"], text=True, capture_output=True, check=False
    )

    assert "--diff BASE" in result.stdout
    assert "  mutate-check --diff " in result.stdout
