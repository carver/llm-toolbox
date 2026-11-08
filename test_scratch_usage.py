"""bin/scratch-usage, run as an agent runs it, on a fake scratch root in tmp_path
and a fake df file, with real processes holding folders open."""

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "scratch-usage"
MIB = 1 << 20
GIB = 1 << 30
MINUTE, HOUR, DAY = 60, 3600, 86400


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "claude-1000"
    path.mkdir()
    return path


@pytest.fixture
def df(tmp_path):
    """Writes the fake df file: size, used and available bytes."""
    path = tmp_path / "df"

    def write(size, used, avail):
        path.write_text(f"{size} {used} {avail}\n")
        return path

    write(20 * GIB, 10 * GIB, 10 * GIB)
    return write


def run(root, df_path, *args, cwd=None):
    env = {**os.environ, "SCRATCH_USAGE_ROOT": str(root)}
    if df_path is not None:
        env["SCRATCH_USAGE_DF"] = str(df_path)
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def mib(n: int) -> int:
    """n MiB (2 or more) less room for the folders' own blocks, so that sizes, which
    round up as du -h's do, print as n.0M."""
    return n * MIB - 64 * 1024


def fill(folder: Path, size: int, age: float, name="scratchpad/data.bin"):
    """A folder holding one file of `size` bytes, with every mtime in it `age`
    seconds ago."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    when = time.time() - age
    for p in [path, *path.relative_to(folder).parents]:
        os.utime(folder / p, (when, when))
    return folder


def line_for(out: str, folder: Path) -> str:
    """The output line that ends with or names `folder`, by its absolute path."""
    found = [line for line in out.splitlines() if f" {folder}" in line]
    assert len(found) == 1, out
    return found[0]


@pytest.fixture
def sessions(root):
    project = root / "-home-carver-code-claudedev-proj"
    other = root / "-home-carver-code-claudedev-other"
    return {
        "old": fill(project / "aaaa-old", mib(4), 3.5 * DAY),
        "recent": fill(project / "bbbb-recent", mib(2), 5.5 * HOUR),
        "new": fill(other / "cccc-new", mib(3), 20.5 * MINUTE),
    }


def test_session_folders_come_biggest_first_with_size_age_and_absolute_path(
    root, df, sessions
):
    result = run(root, df(20 * GIB, 10 * GIB, 10 * GIB))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:3] == [
        line_for(result.stdout, sessions["old"]),
        line_for(result.stdout, sessions["new"]),
        line_for(result.stdout, sessions["recent"]),
    ]
    assert line_for(result.stdout, sessions["old"]).split() == [
        "4.0M",
        "3d",
        "ago",
        str(sessions["old"]),
    ]
    assert line_for(result.stdout, sessions["new"]).split()[:3] == [
        "3.0M",
        "20m",
        "ago",
    ]
    assert line_for(result.stdout, sessions["recent"]).split()[:3] == [
        "2.0M",
        "5h",
        "ago",
    ]


def test_the_age_is_the_newest_write_anywhere_inside(root, df, sessions):
    fill(sessions["old"], 1024, 2.5 * HOUR, name="tasks/deep/x.output")
    os.utime(sessions["old"], (time.time() - 9 * DAY,) * 2)
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
    assert line_for(out, sessions["old"]).split()[1:3] == ["2h", "ago"]


def test_a_symlink_counts_as_itself_not_what_it_points_at(root, df, tmp_path):
    big = tmp_path / "dataset.bin"
    big.write_bytes(os.urandom(4 * MIB))
    linked = fill(root / "-home-x" / "dddd-linked", mib(2), HOUR)
    (linked / "scratchpad" / "dataset.bin").symlink_to(big)
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
    assert line_for(out, linked).split()[0] == "2.0M"


def test_a_hard_linked_file_counts_once(root, df):
    folder = fill(root / "-home-x" / "eeee-linked", mib(2), HOUR)
    data = folder / "scratchpad" / "data.bin"
    os.link(data, folder / "scratchpad" / "copy.bin")
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
    assert line_for(out, folder).split()[0] == "2.0M"


def test_a_folder_a_process_works_in_or_holds_a_file_open_in_is_in_use(
    root, df, sessions
):
    held = sessions["new"] / "tasks" / "job.output"
    held.parent.mkdir()
    procs = []
    try:
        with held.open("w") as output:
            for kw in (
                {"cwd": sessions["recent"] / "scratchpad"},
                {"stdout": output},
                {"cwd": sessions["new"]},
            ):
                procs.append(subprocess.Popen(["sleep", "60"], **kw))
        out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
        assert line_for(out, sessions["recent"]).endswith(
            f"in use by pid {procs[0].pid} (sleep)"
        )
        assert line_for(out, sessions["new"]).endswith(
            f"in use by pid {procs[1].pid} (sleep) and 1 more"
        )
        assert "in use" not in line_for(out, sessions["old"])
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


def test_its_own_working_folder_does_not_count_as_in_use(root, df, sessions):
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB), cwd=sessions["old"]).stdout
    assert "in use" not in line_for(out, sessions["old"])


def test_files_and_folders_outside_session_folders_get_lines_too(root, df, sessions):
    stray = fill(root / "an-test", mib(5), 26.5 * HOUR)
    loose = root / "notes.md"
    loose.write_text("x")
    in_project = sessions["old"].parent / "loose.txt"
    in_project.write_bytes(os.urandom(4 * MIB))
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
    assert line_for(out, stray).split()[:3] == ["5.0M", "26h", "ago"]
    assert line_for(out, in_project).split()[0] == "4.0M"
    assert out.splitlines()[:2] == [line_for(out, stray), line_for(out, in_project)]
    line_for(out, loose)


def test_limit_cuts_the_list_and_counts_the_rest(root, df, sessions):
    out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB), "--limit", "1").stdout
    lines = out.splitlines()
    assert lines[0] == line_for(out, sessions["old"])
    # mib(3) and mib(2), with their folders' blocks: 4.89M, rounded up.
    assert lines[1] == "... and 2 more: 4.9M"
    assert str(sessions["new"]) not in out


def test_a_negative_limit_is_a_usage_error(root, df):
    result = run(root, df(20 * GIB, 10 * GIB, 10 * GIB), "--limit", "-1")
    assert result.returncode == 2 and result.stdout == ""


def test_the_default_limit_is_ten_lines(root, df):
    for n in range(12):
        fill(root / "-home-x" / f"s{n:02}", (n + 1) * 64 * 1024, HOUR)
    lines = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout.splitlines()
    assert sum(" ago " in line for line in lines) == 10
    assert lines[10].startswith("... and 2 more: ")


def test_the_total_and_the_free_space_end_the_output(root, df, sessions):
    result = run(root, df(20 * GIB, 15 * GIB, int(4.535 * GIB)))
    *_, total, free = result.stdout.splitlines()
    # 8.81M of files and some 36K of folders, rounded up.
    assert total == f"total 8.9M under {root} (3 entries)"
    # df -h rounds 4.535G up to 4.6G; its Use% is used / (used + available), rounded
    # up: 15 / 19.535 is 76.8%.
    assert free.startswith("filesystem ")
    assert free.endswith(": 4.6G free of 20G, 77% used")


def test_a_missing_root_says_so_and_succeeds(tmp_path, df):
    missing = tmp_path / "claude-1000"
    result = run(missing, df(20 * GIB, 10 * GIB, 10 * GIB))
    assert result.returncode == 0
    assert result.stdout == f"no scratch folders: {missing} does not exist\n"


def test_a_root_it_cannot_list_fails_in_one_line(tmp_path, df):
    unlistable = tmp_path / "claude-1000"
    unlistable.write_text("a file, not a folder")
    result = run(unlistable, df(20 * GIB, 10 * GIB, 10 * GIB))
    assert (result.returncode, result.stdout) == (1, "")
    assert len(result.stderr.splitlines()) == 1 and str(unlistable) in result.stderr


def test_an_unreadable_root_fails_in_one_line(root, df, sessions):
    root.chmod(0)
    try:
        result = run(root, df(20 * GIB, 10 * GIB, 10 * GIB))
    finally:
        root.chmod(0o700)
    assert (result.returncode, result.stdout) == (1, "")
    assert len(result.stderr.splitlines()) == 1 and str(root) in result.stderr


@pytest.mark.parametrize(
    "used, avail, percent",
    [
        (90, 10, 90),
        (99, 1, 99),
        # 89.01%: df rounds up, so it shows 90%.
        (8901, 1099, 90),
    ],
)
def test_warn_if_full_prints_one_line_naming_scratch_usage_from_90_percent(
    root, df, used, avail, percent
):
    unit = 1 << 20
    result = run(
        root, df((used + avail) * unit, used * unit, avail * unit), "--warn-if-full"
    )
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    assert f"{percent}%" in lines[0] and "`scratch-usage`" in lines[0]


def test_warn_if_full_says_nothing_below_90_percent(root, df, sessions):
    result = run(root, df(100 * MIB, 89 * MIB, 11 * MIB), "--warn-if-full")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_warn_if_full_says_in_one_line_when_it_cannot_check_and_still_succeeds(
    root, tmp_path
):
    result = run(root, tmp_path / "no-such-df", "--warn-if-full")
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1 and "scratch-usage --warn-if-full failed" in lines[0]
    assert "`df -h /tmp`" in lines[0]


def test_an_unreadable_folder_is_left_out_and_counted(root, df, sessions):
    locked = sessions["old"] / "scratchpad" / "locked"
    locked.mkdir()
    (locked / "inside.bin").write_bytes(os.urandom(mib(2)))
    locked.chmod(0)
    try:
        out = run(root, df(20 * GIB, 10 * GIB, 10 * GIB)).stdout
    finally:
        locked.chmod(0o700)
    assert line_for(out, sessions["old"]).split()[0] == "4.0M"
    assert "1 folder could not be read, so the sizes leave it out" in out
