"""apply-edits replaces old text with new across files, all or nothing."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "apply-edits"


def apply(cwd: Path, edits: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT)], input=edits, cwd=cwd, text=True, capture_output=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    )
    (tmp_path / "notes.md").write_text("# Notes\n\nOld line.")
    return tmp_path


def test_blocks_across_files_apply_in_order(repo: Path) -> None:
    result = apply(
        repo,
        """--- src/a.py
<<<<<<<
    return 1
=======
    return 10
>>>>>>>
<<<<<<<
    return 10


def b():
=======
    return 10


def bee():
>>>>>>>
--- notes.md
<<<<<<<
Old line.
=======
New line.
>>>>>>>
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["src/a.py: 2 edits", "notes.md: 1 edit"]
    assert (repo / "src" / "a.py").read_text() == (
        "def a():\n    return 10\n\n\ndef bee():\n    return 2\n"
    )
    assert (repo / "notes.md").read_text() == "# Notes\n\nNew line."


def test_an_empty_new_text_deletes(repo: Path) -> None:
    result = apply(
        repo, "--- src/a.py\n<<<<<<<\n\n\ndef b():\n    return 2\n=======\n>>>>>>>\n"
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "src" / "a.py").read_text() == "def a():\n    return 1\n"


def test_a_miss_writes_nothing_and_shows_the_closest_text(repo: Path) -> None:
    before = (repo / "notes.md").read_text()

    result = apply(
        repo,
        """--- notes.md
<<<<<<<
Old line.
=======
New line.
>>>>>>>
--- src/a.py
<<<<<<<
def b():
    return 3
=======
def b():
    return 4
>>>>>>>
""",
    )

    assert result.returncode == 1
    assert (repo / "notes.md").read_text() == before
    assert "src/a.py block 1" in result.stderr
    assert "not found" in result.stderr
    assert "lines 5-6" in result.stderr
    assert "-    return 3" in result.stderr and "+    return 2" in result.stderr


def test_old_text_found_more_than_once_needs_more_context(repo: Path) -> None:
    result = apply(repo, "--- src/a.py\n<<<<<<<\ndef \n=======\nfn \n>>>>>>>\n")

    assert result.returncode == 1
    assert "found 2 times" in result.stderr and "lines 1, 5" in result.stderr


@pytest.mark.parametrize(
    "edits",
    [
        "",
        "--- missing.py\n<<<<<<<\na\n=======\nb\n>>>>>>>\n",
        "--- notes.md\n<<<<<<<\nOld line.\n>>>>>>>\n",
        "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew line.\n",
        "<<<<<<<\nOld line.\n=======\nNew line.\n>>>>>>>\n",
        "--- notes.md\n<<<<<<<\n=======\nNew line.\n>>>>>>>\n",
    ],
    ids=["nothing", "no such file", "no divider", "unclosed", "no file", "empty old"],
)
def test_malformed_input_is_a_usage_error(repo: Path, edits: str) -> None:
    before = (repo / "notes.md").read_text()

    result = apply(repo, edits)

    assert result.returncode == 2
    assert result.stderr
    assert (repo / "notes.md").read_text() == before


def test_a_path_given_twice_gets_both_sections(repo: Path) -> None:
    result = apply(
        repo,
        "--- notes.md\n<<<<<<<\n# Notes\n=======\n# Log\n>>>>>>>\n"
        "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew line.\n>>>>>>>\n",
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "notes.md").read_text() == "# Log\n\nNew line."
    assert result.stdout.splitlines() == ["notes.md: 2 edits"]


def test_windows_line_endings_are_kept(repo: Path) -> None:
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")

    result = apply(repo, "--- crlf.txt\n<<<<<<<\nb\nc\n=======\nB\nC\n>>>>>>>\n")

    assert result.returncode == 0, result.stderr
    assert (repo / "crlf.txt").read_bytes() == b"a\r\nB\r\nC\r\n"


def test_a_symlink_is_edited_through_to_its_target(repo: Path) -> None:
    (repo / "link.md").symlink_to(repo / "notes.md")

    result = apply(
        repo, "--- link.md\n<<<<<<<\nOld line.\n=======\nNew line.\n>>>>>>>\n"
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "link.md").is_symlink()
    assert (repo / "notes.md").read_text() == "# Notes\n\nNew line."


def test_a_binary_file_is_a_usage_error(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x00junk")

    result = apply(repo, "--- blob.bin\n<<<<<<<\njunk\n=======\nx\n>>>>>>>\n")

    assert result.returncode == 2
    assert "blob.bin" in result.stderr and "Traceback" not in result.stderr


def test_the_closest_text_shows_the_files_line_numbers(repo: Path) -> None:
    result = apply(
        repo, "--- src/a.py\n<<<<<<<\n\ndef b():\n    return 3\n=======\nx\n>>>>>>>\n"
    )

    assert "block 1 ('def b():')" in result.stderr
    assert "@@ -1" not in result.stderr
