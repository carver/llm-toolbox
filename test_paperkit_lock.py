"""The build lock: a build of a paper waits while another process builds it, names that process, and runs once
it ends, so two builds never clear each other's figures mid-compile."""

import os
import subprocess
import sys
import threading
import time

import pytest

from paperkit_testing import LIB, write_minimal

sys.path.insert(0, str(LIB))
from paperkit.build import build, build_lock, lock_holder

HOLD = (
    "import sys; from pathlib import Path; from paperkit.build import build_lock\n"
    + (
        "with build_lock(Path(sys.argv[1])):\n    print('held', flush=True)\n    sys.stdin.read()\n"
    )
)


@pytest.fixture
def holder(tmp_path):
    """Another process holding the lock on <tmp_path>/build until its stdin closes."""
    out = tmp_path / "build"
    out.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(out)],
        env=os.environ | {"PYTHONPATH": str(LIB)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline() == "held\n"
    yield process
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    process.wait(timeout=10)


def release(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    process.wait(timeout=10)


class InThread:
    """`work` run in a thread; `wait` is True once it ended, and re-raises what it raised."""

    def __init__(self, work) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None

        def run() -> None:
            try:
                work()
            except BaseException as error:  # noqa: BLE001  (re-raised by wait())
                self.error = error
            self.done.set()

        threading.Thread(target=run, daemon=True).start()

    def wait(self, seconds: float) -> bool:
        ended = self.done.wait(seconds)
        if self.error:
            raise self.error
        return ended


def test_the_lock_names_the_process_holding_it_and_frees_when_it_ends(tmp_path, holder):
    assert lock_holder(tmp_path / "build") == holder.pid
    release(holder)
    assert lock_holder(tmp_path / "build") is None


def test_a_second_holder_waits_for_the_first_and_says_whom_it_waits_for(
    tmp_path, holder, capsys
):
    def hold() -> None:
        with build_lock(tmp_path / "build"):
            pass

    done = InThread(hold)
    err = ""
    for _ in range(100):  # until the thread has said whom it waits for, or 10 s
        err += capsys.readouterr().err
        if f"pid {holder.pid}" in err:
            break
        time.sleep(0.1)
    assert f"pid {holder.pid}" in err
    assert not done.wait(0)
    release(holder)
    assert done.wait(5)


def test_a_build_waits_while_another_process_builds_the_same_paper(tmp_path, holder):
    write_minimal(tmp_path)
    done = InThread(lambda: build(tmp_path, sources={}))
    assert not done.wait(1)
    release(holder)
    assert done.wait(30)
    assert (tmp_path / "build" / "DRAFT-paper.pdf").exists()
