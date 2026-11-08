"""conflicts lists a file's conflict hunks and resolves them by side."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "conflicts"

TWO_HUNKS = """\
top
<<<<<<< HEAD
ours one
=======
theirs one
>>>>>>> abc123 (Add theirs)
middle
<<<<<<< HEAD
ours two
||||||| parent of abc123 (Add theirs)
base two
=======
theirs two
>>>>>>> abc123 (Add theirs)
bottom
"""


def conflicts(cwd, *args):
    return subprocess.run(
        [str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture
def path(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text(TWO_HUNKS)
    return file


def test_listing_numbers_each_hunk_with_its_sides_and_labels(path):
    shown = conflicts(path.parent, "a.txt")

    assert shown.returncode == 0, shown.stderr
    out = shown.stdout
    assert "hunk 1" in out and "hunk 2" in out and "hunk 3" not in out
    assert "lines 2-6" in out and "lines 8-14" in out
    assert "HEAD" in out and "abc123 (Add theirs)" in out
    assert out.index("ours one") < out.index("theirs one") < out.index("ours two")
    assert "base two" in out
    assert path.read_text() == TWO_HUNKS  # listing writes nothing


@pytest.mark.parametrize(
    "side, kept",
    [
        ("ours", "ours one\n"),
        ("theirs", "theirs one\n"),
        ("both", "ours one\ntheirs one\n"),
    ],
)
def test_resolving_one_hunk_keeps_that_side_and_leaves_the_other_hunk(path, side, kept):
    done = conflicts(path.parent, "a.txt", "1", side)

    assert done.returncode == 0, done.stderr
    text = path.read_text()
    assert text.startswith(f"top\n{kept}middle\n<<<<<<< HEAD\nours two\n")
    assert "1 hunk left" in done.stdout


def test_base_takes_a_diff3_hunks_base_side(path):
    done = conflicts(path.parent, "a.txt", "2", "base")

    assert done.returncode == 0, done.stderr
    assert path.read_text().endswith("middle\nbase two\nbottom\n")


def test_base_refuses_a_hunk_without_one(path):
    done = conflicts(path.parent, "a.txt", "1", "base")

    assert done.returncode == 1 and "base" in done.stderr
    assert path.read_text() == TWO_HUNKS


def test_all_resolves_every_hunk_and_says_none_are_left(path):
    done = conflicts(path.parent, "a.txt", "all", "theirs")

    assert done.returncode == 0, done.stderr
    assert path.read_text() == "top\ntheirs one\nmiddle\ntheirs two\nbottom\n"
    assert "no conflicts left" in done.stdout and "git add a.txt" in done.stdout


def test_a_hunk_number_out_of_range_changes_nothing(path):
    done = conflicts(path.parent, "a.txt", "3", "ours")

    assert done.returncode == 1 and "hunk 3" in done.stderr
    assert path.read_text() == TWO_HUNKS


@pytest.mark.parametrize(
    "text",
    [
        "<<<<<<< HEAD\nours\n=======\ntheirs\n",  # never closed
        "<<<<<<< HEAD\nours\n>>>>>>> abc\n",  # no separator
        "<<<<<<< HEAD\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> abc\n",  # nested
    ],
)
def test_malformed_markers_refuse_the_file(tmp_path, text):
    file = tmp_path / "a.txt"
    file.write_text(text)

    done = conflicts(tmp_path, "a.txt", "all", "ours")

    assert done.returncode == 1 and "a.txt" in done.stderr
    assert file.read_text() == text


def test_a_separator_line_outside_a_hunk_is_content(tmp_path):
    file = tmp_path / "a.rst"
    file.write_text("Title\n=======\n" + TWO_HUNKS)

    done = conflicts(tmp_path, "a.rst", "all", "ours")

    assert done.returncode == 0, done.stderr
    assert file.read_text().startswith("Title\n=======\ntop\nours one\n")


def test_a_file_without_conflicts_says_so(tmp_path):
    (tmp_path / "a.txt").write_text("clean\n")

    shown = conflicts(tmp_path, "a.txt")

    assert shown.returncode == 0 and "no conflicts" in shown.stdout


def test_crlf_line_endings_survive(tmp_path):
    file = tmp_path / "a.txt"
    file.write_bytes(TWO_HUNKS.replace("\n", "\r\n").encode())

    conflicts(tmp_path, "a.txt", "all", "ours")

    assert file.read_bytes() == b"top\r\nours one\r\nmiddle\r\nours two\r\nbottom\r\n"


@pytest.fixture
def two_files(path):
    other = path.parent / "b.txt"
    other.write_text(TWO_HUNKS)
    return path, other


def test_triples_resolve_each_file(two_files):
    first, second = two_files

    done = conflicts(first.parent, "a.txt", "all", "ours", "b.txt", "2", "theirs")

    assert done.returncode == 0, done.stderr
    assert first.read_text() == "top\nours one\nmiddle\nours two\nbottom\n"
    assert second.read_text().endswith("middle\ntheirs two\nbottom\n")
    assert "a.txt: resolved (ours); no conflicts left" in done.stdout
    assert "b.txt: resolved (theirs); 1 hunk left" in done.stdout


def test_triples_on_one_file_number_its_hunks_as_the_listing_did(path):
    done = conflicts(path.parent, "a.txt", "2", "theirs", "a.txt", "1", "both")

    assert done.returncode == 0, done.stderr
    assert path.read_text() == (
        "top\nours one\ntheirs one\nmiddle\ntheirs two\nbottom\n"
    )
    assert "resolved (2 theirs, 1 both); no conflicts left" in done.stdout


@pytest.mark.parametrize(
    "args, named",
    [
        (["a.txt", "b.txt", "all", "ours"], "b.txt"),  # two files, then one choice
        (["a.txt", "all", "ours", "b.txt"], "b.txt"),  # a file without its choice
        (["a.txt", "all"], "all"),
        (["a.txt", "1", "ours", "b.txt", "1", "sideways"], "sideways"),
        (["all", "1", "ours"], "all"),  # a keyword where a FILE goes
        (["ours"], "ours"),
        (["a.txt", "1", "ours", "a.txt", "1", "theirs"], "hunk 1"),
        (["a.txt", "all", "ours", "a.txt", "2", "theirs"], "all"),
    ],
)
def test_arguments_neither_files_nor_triples_change_nothing(two_files, args, named):
    done = conflicts(two_files[0].parent, *args)

    assert done.returncode == 2, done
    assert named in done.stderr and "Traceback" not in done.stderr
    assert done.stdout == ""
    assert all(file.read_text() == TWO_HUNKS for file in two_files)


def test_a_failing_triple_leaves_every_file_as_it_was(two_files):
    done = conflicts(two_files[0].parent, "a.txt", "all", "ours", "b.txt", "3", "ours")

    assert done.returncode == 1
    assert "b.txt" in done.stderr and "changed nothing" in done.stderr
    assert all(file.read_text() == TWO_HUNKS for file in two_files)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder")
def test_a_file_it_cant_write_leaves_the_others_as_they_were(path):
    locked = path.parent / "locked"
    locked.mkdir()
    (locked / "b.txt").write_text(TWO_HUNKS)
    locked.chmod(0o555)
    path.chmod(0o640)

    try:
        done = conflicts(
            path.parent, "a.txt", "all", "ours", "locked/b.txt", "1", "ours"
        )
    finally:
        locked.chmod(0o755)

    assert done.returncode == 1
    assert done.stderr.startswith("locked/b.txt:") and "changed nothing" in done.stderr
    assert path.read_text() == TWO_HUNKS and (locked / "b.txt").read_text() == TWO_HUNKS
    assert sorted(p.name for p in path.parent.iterdir()) == ["a.txt", "locked"]


def test_a_resolved_file_keeps_its_mode(path):
    path.chmod(0o751)

    conflicts(path.parent, "a.txt", "all", "ours")

    assert path.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize("args", [["gone.txt"], ["gone.txt", "all", "ours"]])
def test_a_missing_file_is_one_line_not_a_traceback(tmp_path, args):
    done = conflicts(tmp_path, *args)

    assert done.returncode == 1
    assert done.stderr.startswith("gone.txt:") and done.stderr.count("\n") == 1


def test_bytes_that_are_not_utf8_are_listed_and_survive(tmp_path):
    file = tmp_path / "a.txt"
    file.write_bytes(TWO_HUNKS.replace("ours one", "caf\xe9").encode("latin-1"))

    shown = conflicts(tmp_path, "a.txt")
    done = conflicts(tmp_path, "a.txt", "all", "ours")

    assert shown.returncode == 0 and "caf" in shown.stdout, shown.stderr
    assert done.returncode == 0, done.stderr
    assert file.read_bytes() == b"top\ncaf\xe9\nmiddle\nours two\nbottom\n"


@pytest.mark.parametrize("marker", [">>>>>>> abc", "||||||| base"])
def test_a_closing_or_base_marker_outside_a_hunk_is_malformed(tmp_path, marker):
    file = tmp_path / "a.txt"
    file.write_text(f"{TWO_HUNKS}{marker}\n")

    shown = conflicts(tmp_path, "a.txt")
    done = conflicts(tmp_path, "a.txt", "all", "ours")

    assert shown.returncode == 1 and "line 16" in shown.stderr
    assert done.returncode == 1 and "changed nothing" in done.stderr
    assert file.read_text() == f"{TWO_HUNKS}{marker}\n"


@pytest.mark.parametrize(
    "base, theirs",
    [
        ("", "a.txt conflict-marker-size=32\n"),  # the merge brings it in
        ("a.txt conflict-marker-size=32\n", ""),  # the merge takes it out
    ],
)
def test_markers_are_as_long_as_the_attributes_git_merged_with(tmp_path, base, theirs):
    new_repo(tmp_path)
    commit(tmp_path, "base", {".gitattributes": base, "a.txt": "a\n"})
    git(tmp_path, "checkout", "-q", "-b", "other")
    commit(tmp_path, "theirs", {".gitattributes": theirs, "a.txt": "a theirs\n"})
    git(tmp_path, "checkout", "-q", "main")
    commit(tmp_path, "ours", {"a.txt": "a ours\n"})
    subprocess.run(
        ["git", "merge", "other"], cwd=tmp_path, capture_output=True, check=False
    )

    shown = conflicts(tmp_path, "a.txt")

    assert "hunk 1" in shown.stdout, shown


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def commit(repo, message, files):
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def new_repo(path):
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.com")
    return path


# Markers a file holds on purpose, as the tests of a conflict tool do.
FIXTURE = "<<<<<<< fixture\nkept\n=======\nkept too\n>>>>>>> fixture\n"


def merge_conflict(repo, attributes=""):
    """A merge stopped at a conflict in a.txt, whose text also holds FIXTURE."""
    new_repo(repo)
    commit(repo, "base", {".gitattributes": attributes, "a.txt": f"{FIXTURE}line\n"})
    git(repo, "checkout", "-q", "-b", "other")
    commit(repo, "theirs", {"a.txt": f"{FIXTURE}theirs\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, "ours", {"a.txt": f"{FIXTURE}ours\n"})
    merge = subprocess.run(
        ["git", "merge", "-q", "other"], cwd=repo, capture_output=True, check=False
    )
    assert merge.returncode == 1, merge
    return repo / "a.txt"


@pytest.mark.parametrize(
    "attributes",
    [
        "",  # git's markers are 7 long, as FIXTURE's are
        "a.txt conflict-marker-size=10\n",
        "a.txt conflict-marker-size\n",  # no number: 7
        "a.txt conflict-marker-size=12abc\n",  # not a number: 7
        "a.txt conflict-marker-size=+12\n",  # a sign: 12
        "a.txt conflict-marker-size=99999999999\n",  # past an int: 7
    ],
)
def test_only_the_markers_git_wrote_make_hunks(tmp_path, attributes):
    file = merge_conflict(tmp_path, attributes)

    shown = conflicts(tmp_path, "a.txt")
    done = conflicts(tmp_path, "a.txt", "1", "theirs")

    assert shown.returncode == 0, shown.stderr
    assert "hunk 1" in shown.stdout and "hunk 2" not in shown.stdout
    assert "fixture" not in shown.stdout
    assert done.returncode == 0, done.stderr
    assert file.read_text() == f"{FIXTURE}theirs\n"
