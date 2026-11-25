"""procmatch.shortened keeps a listed command line short and the match in view."""

import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from procmatch import LISTED_CHARS, running_in, shortened

MAX = LISTED_CHARS + 40  # room for the ellipses and the length note


def test_a_short_line_is_left_whole() -> None:
    assert shortened("sleep 30", "sleep", True) == "sleep 30"


def test_a_match_far_in_shows_with_the_start_and_the_full_length() -> None:
    line = "bash -c " + "x" * 5000 + " marker tail"
    out = shortened(line, "marker", True)
    assert out.startswith("bash -c ") and "marker" in out
    assert out.endswith(f"[{len(line)} chars]") and len(out) <= MAX


def test_a_match_as_wide_as_the_line_is_still_cut() -> None:
    line = "bash -c " + "x" * 7000 + " marker"
    assert len(shortened(line, "bash.*marker", True)) <= MAX


def test_a_name_match_shows_the_start() -> None:
    line = "python3 " + "y" * 5000
    out = shortened(line, "python3", False)
    assert out.startswith("python3 ") and out.endswith(f"[{len(line)} chars]")
    assert len(out) <= MAX


def test_no_gap_marker_when_the_match_follows_the_start() -> None:
    line = "z" * 90 + "marker" + "w" * 5000
    out = shortened(line, "marker", True)
    assert "z" * 90 + "marker" in out


# running_in: the processes a worktree removal would leave in a deleted folder.


@contextmanager
def started(argv: list[str], cwd: Path) -> Iterator[subprocess.Popen]:
    # A session of its own, as a job an earlier command started would be.
    process = subprocess.Popen(argv, cwd=cwd, start_new_session=True)
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def test_a_process_whose_directory_is_inside_is_listed(tmp_path: Path) -> None:
    inner = tmp_path / "tree" / "sub"
    inner.mkdir(parents=True)
    with started(["sleep", "30"], inner) as job:
        found = running_in(tmp_path / "tree")
    assert job.pid in found and "sleep 30" in found[job.pid], found


def test_a_process_whose_executable_is_inside_is_listed(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    sleeper = tree / "sleep"  # coreutils may be one binary that runs by its name
    shutil.copy(shutil.which("sleep"), sleeper)
    with started([str(sleeper), "30"], tmp_path) as job:
        found = running_in(tree)
    assert job.pid in found, found


def test_a_process_outside_or_in_a_folder_sharing_the_prefix_is_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "tree").mkdir()
    twin = tmp_path / "tree2"
    twin.mkdir()
    with started(["sleep", "30"], twin) as job:
        found = running_in(tmp_path / "tree")
    assert job.pid not in found, found


def test_a_deleted_directory_still_counts(tmp_path: Path) -> None:
    inner = tmp_path / "tree" / "gone"
    inner.mkdir(parents=True)
    with started(["sleep", "30"], inner) as job:
        inner.rmdir()
        found = running_in(tmp_path / "tree")
    assert job.pid in found, found


def test_the_caller_and_its_pipeline_siblings_are_not_listed(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    lib = Path(__file__).resolve().parent / "lib"
    script = (
        f"import sys; sys.path.insert(0, {str(lib)!r}); from pathlib import Path;"
        f" from procmatch import running_in; print(sorted(running_in(Path({str(tree)!r}))))"
    )
    # The shell, the python reading and `cat` all run in the tree, as
    # `cd wt && wt-ship --remove | cat` would.
    done = subprocess.run(
        ["bash", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} | cat"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "[]", done.stdout
