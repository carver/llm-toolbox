"""The guard in leftover_processes.py, which conftest.py gives every test, run on small
suites of its own beside a copy of conftest.py. A test or a module's fixtures that leave
a process running error at teardown, naming each process, and it is killed with its
process group. What a fixture stops at its own teardown, a module fixture's process kept
for later tests, and processes the test doesn't own are left alone."""

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
from procmatch import running

from leftover_processes import TAG

# Each suite's tests note the pids they start, so the outer test kills them whatever
# the guard did. The suite's conftest.py is a copy of this one's, with wait_until.
PRELUDE = f"""\
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import wait_until

TAG = {TAG!r}


def note(*pids):
    with open(os.environ["NOTED_PIDS"], "a") as noted:
        noted.writelines(f"{{pid}}\\n" for pid in pids)


def untagged():
    return {{k: v for k, v in os.environ.items() if k != TAG}}


def orphan(args, cwd="/", env=None):
    \"\"\"Starts `args` from a shell in a session of its own that exits at once, so
    init adopts it, as a daemon whose starter ended; returns its pid.\"\"\"
    started = subprocess.run(
        ["bash", "-c", '"$@" >/dev/null 2>&1 & echo $!', "orphan", *args],
        cwd=cwd, env=env, capture_output=True, text=True, check=True,
        start_new_session=True,
    )
    pid = int(started.stdout)
    note(pid)
    cmdline = Path(f"/proc/{{pid}}/cmdline")
    wait_until(lambda: not cmdline.read_bytes().startswith(b"bash\\0"), f"{{args}} ran")
    return pid


def wait_for(path):
    wait_until(lambda: path.exists() and path.read_text().strip(), f"{{path}} written")
    return path.read_text().strip()

"""


class Suite:
    """A folder holding a copy of conftest.py, where `run` writes a test file and runs
    pytest on it, without xdist."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.noted = folder / "noted"
        self.basetemp = folder / "basetemp"
        (folder / "suite").mkdir()
        (folder / "suite" / "conftest.py").write_text(
            (ROOT / "conftest.py").read_text()
        )

    def run(
        self, tests: str, grace: float = 0.1, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        """In a session of its own, so a kill of pytest's group can't reach this one's."""
        (self.folder / "suite" / "test_inner.py").write_text(PRELUDE + tests)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--basetemp={self.basetemp}",
            ],
            cwd=self.folder / "suite",
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PYTEST_ADDOPTS": "",
                "NOTED_PIDS": str(self.noted),
                "LEFTOVER_PROCESSES_GRACE": str(grace),
                **(env or {}),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            start_new_session=True,
        )

    def note(self, pid: int) -> None:
        with self.noted.open("a") as noted:
            noted.write(f"{pid}\n")

    def pids(self) -> list[int]:
        if not self.noted.exists():
            return []
        return [int(line) for line in self.noted.read_text().split()]


@pytest.fixture
def suite(tmp_path):
    """Kills every noted process's group at the end; each started a session of its own."""
    made = Suite(tmp_path)
    yield made
    for pid in made.pids():
        try:
            group = os.getpgid(pid)
            if running(pid) and group != os.getpgrp():
                os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:  # gone with a group killed before it
            pass


def named(output: str, pid: int) -> str | None:
    """The command line the guard's report gives for `pid`, or None if it isn't named."""
    found = re.search(rf"^ +{pid} +\d+\.\d s +(.+)$", output, re.MULTILINE)
    return found and found.group(1)


def test_a_test_that_leaves_processes_running_errors_naming_each(suite):
    """One runs in a session of its own; the other in pytest's process group, which
    the kill spares: pytest goes on to report."""
    result = suite.run(
        """
def test_leaks():
    note(subprocess.Popen(["sleep", "300"], start_new_session=True).pid)
    note(subprocess.Popen(["sleep", "301"]).pid)
"""
    )
    own_session, pytests_group = suite.pids()
    assert result.returncode == 1, result.stdout
    assert "1 passed, 1 error" in result.stdout
    assert "ERROR test_inner.py::test_leaks" in result.stdout
    assert named(result.stdout, own_session) == "sleep 300"
    assert named(result.stdout, pytests_group) == "sleep 301"
    assert not running(own_session)
    assert not running(pytests_group)


def test_the_leftover_process_group_is_killed_with_it(suite):
    """The shell's other process was orphaned, carries no tag, works in / and names no
    test folder, so only its process group finds it. The shell writes its pid once the
    shell that started it has exited."""
    result = suite.run(
        """
GROUP = r'''
cd / || exit
env -u LEFTOVER_PROCESSES_TAG bash -c 'sleep 300 >/dev/null 2>&1 & echo $!' > "$1.tmp"
mv "$1.tmp" "$1"
exec sleep 300
'''


def test_leaks_a_group(tmp_path):
    shell = subprocess.Popen(
        ["bash", "-c", GROUP, "group", str(tmp_path / "child")], start_new_session=True
    )
    note(shell.pid)
    note(int(wait_for(tmp_path / "child")))
"""
    )
    shell, child = suite.pids()
    assert result.returncode == 1, result.stdout
    assert named(result.stdout, shell)
    assert named(result.stdout, child) is None
    assert not running(shell)
    assert not running(child)


def test_orphans_are_found_by_their_tag_or_their_folder(suite):
    """A process whose starter exited counts when it carries the test's tag, or carries
    none and works in a test's folder, even one since deleted, or names one."""
    result = suite.run(
        """
def test_orphans_a_tagged_process():
    orphan(["sleep", "300"])


def test_orphans_from_a_deleted_folder(tmp_path):
    folder = tmp_path / "gone"
    folder.mkdir()
    orphan(["sleep", "300"], cwd=folder, env=untagged())
    folder.rmdir()


def test_orphans_a_process_naming_a_test_folder(tmp_path):
    orphan(
        [sys.executable, "-c", "import time; time.sleep(300)", str(tmp_path / "named")],
        env=untagged(),
    )
"""
    )
    tagged, in_deleted, naming = suite.pids()
    assert result.returncode == 1, result.stdout
    assert "3 passed, 3 errors" in result.stdout
    assert named(result.stdout, tagged) == "sleep 300"
    assert named(result.stdout, in_deleted) == "sleep 300"
    assert "time.sleep(300)" in named(result.stdout, naming)
    assert not any(running(pid) for pid in suite.pids())


def test_a_pytest_run_inside_a_test_that_dies_leaves_its_orphans_to_the_test(suite):
    """The inner run's guard chains its tag after the test's, so what the inner run
    orphaned before it was killed, outside any folder of the test's, still counts."""
    result = suite.run(
        """
INNER = '''
import os, signal, subprocess

def test_orphans_then_dies():
    started = subprocess.run(
        ["bash", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
        cwd="/", capture_output=True, text=True, start_new_session=True,
    )
    with open(os.environ["NOTED_PIDS"], "a") as noted:
        noted.write(started.stdout)
    os.kill(os.getpid(), signal.SIGKILL)
'''


def test_runs_a_suite_that_dies(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text())
    (inner / "test_dies.py").write_text(INNER)
    died = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'inner-tmp'}"],
        cwd=inner, capture_output=True, text=True,
    )
    assert died.returncode == -signal.SIGKILL, died.stdout
"""
    )
    (orphaned,) = suite.pids()
    assert result.returncode == 1, result.stdout
    assert named(result.stdout, orphaned)
    assert not running(orphaned)


SPAWNER = """
while [ ! -s "$1" ]; do sleep 0.01; done
{ read -r folder; read -r tag; } < "$1"
cd "$folder" || exit
LEFTOVER_PROCESSES_TAG="$tag" sleep 300 &
echo $! > "$2"
wait
"""


def test_processes_the_test_does_not_own_are_left_alone(suite, tmp_path):
    """Orphans with another guard's tag, as another worker's or another run's carry,
    though one works in this test's folder, or with no tag and working elsewhere, as
    another session's daemon: even one whose folder or argument starts with pytest's
    basetemp but goes on (`pytest-1` against `pytest-12`). And a
    process that a process outside the test's tree starts during it, as another
    session's shell starting `tail -f` on a test's log, even with the test's own tag,
    in the test's folder."""
    trigger, spawned = tmp_path / "trigger", tmp_path / "spawned"
    spawner = subprocess.Popen(
        ["bash", "-c", SPAWNER, "spawner", str(trigger), str(spawned)],
        start_new_session=True,
    )
    suite.note(spawner.pid)
    result = suite.run(
        """
def test_meets_processes_it_does_not_own(tmp_path, tmp_path_factory):
    orphan(["sleep", "300"], cwd=tmp_path, env={**untagged(), TAG: "1-0"})
    orphan(["sleep", "300"], env=untagged())
    sibling = Path(f"{tmp_path_factory.getbasetemp()}2")
    sibling.mkdir()
    orphan(["sleep", "300"], cwd=sibling, env=untagged())
    orphan(
        [sys.executable, "-c", "import time; time.sleep(300)", str(sibling / "named")],
        env=untagged(),
    )
    trigger = Path(os.environ["TRIGGER"])
    trigger.write_text(f"{tmp_path}\\n{os.environ[TAG]}\\n")
    note(int(wait_for(Path(os.environ["SPAWNED"]))))
""",
        env={"TRIGGER": str(trigger), "SPAWNED": str(spawned)},
    )
    assert (result.returncode, "1 passed" in result.stdout) == (0, True), result.stdout
    assert all(running(pid) for pid in suite.pids())


def test_a_tag_that_runs_on_past_the_tests_is_not_the_tests(suite):
    """`<module>/<pid>-12` against the test's `<module>/<pid>-1`: a later test's tag in
    the same module, so the module's watch counts it instead."""
    result = suite.run(
        """
def test_orphans_a_process_with_a_later_tests_tag():
    orphan(["sleep", "300"], env={**untagged(), TAG: os.environ[TAG] + "2"})
"""
    )
    (orphaned,) = suite.pids()
    assert result.returncode == 1, result.stdout
    assert "the test left" not in result.stdout
    assert "test_inner.py's module fixtures left 1 process" in result.stdout
    assert named(result.stdout, orphaned) == "sleep 300"


JOB = """
while [ ! -s "$1" ]; do sleep 0.01; done
cd "$(cat "$1")" || exit
(env -u LEFTOVER_PROCESSES_TAG sleep 300 >/dev/null 2>&1 & echo $! > "$2.tmp")
mv "$2.tmp" "$2"
exec sleep 300
"""


def test_a_stray_orphan_is_killed_alone_sparing_its_group(suite, tmp_path):
    """Another session's job orphans an untagged process into a test's folder, which
    counts as left over; it doesn't lead its process group, so the job leading it
    lives."""
    trigger, orphaned = tmp_path / "trigger", tmp_path / "orphaned"
    job = subprocess.Popen(
        ["bash", "-c", JOB, "job", str(trigger), str(orphaned)],
        start_new_session=True,
    )
    suite.note(job.pid)
    result = suite.run(
        """
def test_meets_a_stray_orphan(tmp_path):
    Path(os.environ["TRIGGER"]).write_text(f"{tmp_path}\\n")
    note(int(wait_for(Path(os.environ["ORPHANED"]))))
""",
        env={"TRIGGER": str(trigger), "ORPHANED": str(orphaned)},
    )
    _, stray = suite.pids()
    assert result.returncode == 1, result.stdout
    assert named(result.stdout, stray) == "sleep 300"
    assert not running(stray)
    assert running(job.pid)


def test_processes_fixtures_stop_at_teardown_pass(suite):
    """A module fixture's process kept across tests and stopped at the module's end, and
    a test fixture's stopped at its teardown, which runs before the guard's."""
    result = suite.run(
        """
@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    note(proc.pid)
    yield proc
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


@pytest.fixture
def helper():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    note(proc.pid)
    yield proc
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def test_first(server, helper):
    assert server.poll() is None


def test_second(server):
    assert server.poll() is None
"""
    )
    assert (result.returncode, "2 passed" in result.stdout) == (0, True), result.stdout
    assert "error" not in result.stdout


def test_a_module_fixture_that_leaves_its_process_errors_at_the_module_end(suite):
    result = suite.run(
        """
@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    note(proc.pid)
    return proc


def test_first(server):
    pass


def test_second(server):
    pass
"""
    )
    (server,) = suite.pids()
    assert result.returncode == 1, result.stdout
    assert "2 passed, 1 error" in result.stdout
    assert "ERROR test_inner.py::test_second" in result.stdout
    assert named(result.stdout, server) == "sleep 300"
    assert not running(server)


def test_a_process_still_exiting_at_teardown_passes(suite):
    """Stopped without waiting, it takes half a second to exit, as a killed process can
    under load, within the grace."""
    result = suite.run(
        """
def test_stops_its_process_without_waiting(tmp_path):
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        ["bash", "-c", "trap 'sleep 0.5; exit' TERM; echo set > \\"$0\\"; "
         "while :; do sleep 0.05; done", str(ready)],
        start_new_session=True,
    )
    note(proc.pid)
    wait_for(ready)
    proc.terminate()
""",
        grace=10,
    )
    assert (result.returncode, "1 passed" in result.stdout) == (0, True), result.stdout
