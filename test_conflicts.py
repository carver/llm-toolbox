"""conflicts lists a file's conflict hunks and resolves them by side."""

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
