"""apply-edits replaces old text with new across files, all or nothing."""

import shutil
import subprocess
import sys
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


def test_aiders_labelled_markers_work(repo: Path) -> None:
    result = apply(
        repo,
        "--- notes.md\n<<<<<<< SEARCH\nOld line.\n=======\nNew line.\n>>>>>>> REPLACE\n",
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "notes.md").read_text() == "# Notes\n\nNew line."


def test_aiders_bare_path_and_fences_work(repo: Path) -> None:
    result = apply(
        repo,
        "notes.md\n```markdown\n<<<<<<< SEARCH\nOld line.\n=======\nNew line.\n"
        ">>>>>>> REPLACE\n```\n\nsrc/a.py\n```python\n<<<<<<< SEARCH\n    return 1\n"
        "=======\n    return 10\n>>>>>>> REPLACE\n```\n",
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "notes.md").read_text() == "# Notes\n\nNew line."
    assert "return 10" in (repo / "src" / "a.py").read_text()


def test_other_labelled_marker_lines_are_content(repo: Path) -> None:
    (repo / "merge.txt").write_text("a\n<<<<<<< HEAD\nb\n")

    result = apply(
        repo, "--- merge.txt\n<<<<<<<\n<<<<<<< HEAD\nb\n=======\nb\n>>>>>>>\n"
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "merge.txt").read_text() == "a\nb\n"


@pytest.mark.parametrize(
    ("edits", "says"),
    [
        (
            "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew line.\n=======\n",
            "line 2: block has a second ======= at line 6; end it with >>>>>>> REPLACE",
        ),
        (
            "--- notes.md\n<<<<<<<\nOld line.\n>>>>>>>\n",
            "line 2: block reaches >>>>>>> at line 4 with no ======= between old and new text",
        ),
        (
            "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew line.\n",
            "line 2: block never closed; end it with >>>>>>> REPLACE",
        ),
        (
            "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew.\n<<<<<<<\nx\n=======\ny\n>>>>>>>\n",
            "line 2: block not closed before the next <<<<<<< at line 6; end it with >>>>>>> REPLACE",
        ),
        (
            "=== notes.md\n<<<<<<<\nOld line.\n=======\nNew line.\n>>>>>>>\n",
            "line 1: start a file's blocks with '--- PATH', got '=== notes.md'",
        ),
        (
            "--- notes.md\n<<<<<<<\nOld line.\n=======\nNew.\n>>>>>>>\nNow the readme:\n",
            "line 7: expected '--- PATH' or <<<<<<< SEARCH, got 'Now the readme:'",
        ),
        (
            "missing.md\n<<<<<<<\nOld line.\n=======\nNew line.\n>>>>>>>\n",
            "line 1: no such file: missing.md",
        ),
    ],
    ids=[
        "second divider",
        "no divider",
        "unclosed",
        "next opens",
        "=== header",
        "prose",
        "bare missing",
    ],
)
def test_malformed_blocks_say_what_to_do(repo: Path, edits: str, says: str) -> None:
    result = apply(repo, edits)

    assert result.returncode == 2
    assert result.stderr.strip() == f"apply-edits: {says}"
    assert (repo / "notes.md").read_text() == "# Notes\n\nOld line."


def test_a_file_name_with_no_block_after_it_is_no_header(repo: Path) -> None:
    edits = "--- notes.md\n<<<<<<<\n# Notes\n=======\n# Log\n>>>>>>>\nsrc/a.py\n"
    before = (repo / "src" / "a.py").read_text()

    result = apply(repo, edits)

    assert result.returncode == 2
    assert "got 'src/a.py'" in result.stderr
    assert (repo / "src" / "a.py").read_text() == before
    assert (repo / "notes.md").read_text() == "# Notes\n\nOld line."


def test_markers_may_end_in_spaces(repo: Path) -> None:
    result = apply(
        repo,
        "--- notes.md\n<<<<<<< SEARCH \nOld line.\n======= \nNew.\n>>>>>>> REPLACE  \n",
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "notes.md").read_text() == "# Notes\n\nNew."


def test_a_divider_with_a_label_is_content(repo: Path) -> None:
    (repo / "odd.txt").write_text("a\n======= None\nb\n")

    result = apply(repo, "--- odd.txt\n<<<<<<<\n======= None\n=======\n>>>>>>>\n")

    assert result.returncode == 0, result.stderr
    assert (repo / "odd.txt").read_text() == "a\nb\n"


def test_a_squashed_equals_header_gets_the_hint(repo: Path) -> None:
    result = apply(repo, "===notes.md\n<<<<<<<\nOld line.\n=======\nNew.\n>>>>>>>\n")

    assert result.returncode == 2
    assert "start a file's blocks with '--- PATH'" in result.stderr


def test_a_path_with_no_blocks_is_left_alone(repo: Path) -> None:
    result = apply(
        repo,
        "--- notes.md\n--- src/a.py\n<<<<<<<\n    return 1\n=======\n    return 3\n>>>>>>>\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "src/a.py: 1 edit\n"


WRAPPED = (
    "def f(path, figure):\n"
    '    (path / "demo.py").write_text(\n'
    '        DEMO + (EXTRA if figure else "")\n'
    "    )\n"
    '    return "a  b"\n'
)


def test_a_python_block_matches_a_call_the_formatter_wrapped(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text(WRAPPED)
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\n"
        '    (path / "demo.py").write_text(DEMO + (EXTRA if figure else ""))\n'
        "=======\n"
        '    (path / "demo.py").write_text(DEMO)\n'
        ">>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == (
        'def f(path, figure):\n    (path / "demo.py").write_text(DEMO)\n    return "a  b"\n'
    )
    assert "formatting" in result.stdout


def test_a_script_run_by_uv_counts_as_python(tmp_path: Path) -> None:
    (tmp_path / "tool").write_text(
        "#!/usr/bin/env -S uv run --quiet --script\nprint(f(a,\n      b))\n"
    )
    result = apply(
        tmp_path,
        "--- tool\n<<<<<<< SEARCH\nprint(f(a, b))\n=======\nprint(g)\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "tool").read_text().endswith("\nprint(g)\n")


@pytest.mark.parametrize(
    "source, old",
    [
        (
            "if x:\n    y()\nz()\n",
            "if x:\n    y()\n    z()",
        ),  # indentation makes a block
        ("a = 1\nb = 2\n", "a = 1 b = 2"),  # a line break ends a statement
        (WRAPPED, '    return "a b"'),  # strings as they are
        ("x = 1  # a  note\n", "x = 1  # a note"),  # comments too
        # the indentation after a comment line too
        (
            "if a:\n    x = f(1,\n      2)\n# c\ny = 2\n",
            "    x = f(1, 2)\n# c\n    y = 2",
        ),
    ],
)
def test_whitespace_that_changes_the_python_still_misses(
    tmp_path: Path, source: str, old: str
) -> None:
    (tmp_path / "f.py").write_text(source)
    result = apply(
        tmp_path, f"--- f.py\n<<<<<<< SEARCH\n{old}\n=======\nnew\n>>>>>>> REPLACE\n"
    )
    assert result.returncode == 1
    assert (tmp_path / "f.py").read_text() == source


def test_a_line_break_between_words_still_misses_in_other_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.md").write_text("Some text\nwrapped here.\n")
    result = apply(
        tmp_path,
        "--- notes.md\n<<<<<<< SEARCH\nSome text wrapped here.\n=======\nnew\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 1


def test_a_loose_match_found_twice_needs_more_context(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = f(a,\n      b)\ny = 1\nx = f(a,\n  b)\n")
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\nx = f(a, b)\n=======\nx = 0\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 1
    assert "found 2 times" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "source, old",
    [
        # the first line's indentation places the code: 4 spaces would move it out of `if b:`
        ("if a:\n    if b:\n        f(x,\n          y)\n", "    f(x, y)"),
        # an indented statement isn't one that sits mid-line
        ("a = 1; b = f(x,\n y)\n", "    b = f(x, y)"),
        # an f-string's nested quotes and brackets stay inside it (Python 3.12)
        ('x = f"{d["a b"]}"\n', 'x = f"{d["a  b"]}"'),
        ('x = f"{d["("]}"\na = 1\nb = 2\n', "a = 1 b = 2"),
        # two tokens aren't one
        ("y = a ** f(b,\n  c)\n", "y = a * * f(b, c)"),
        # a string's own text, whatever its prefix, quotes or escapes
        ('x = rb"a  b"\n', 'x = rb"a b"'),
        ('x = "\\"("\ny = 1\nz = 2\n', "y = 1 z = 2"),
        ('x = """a\n"(" b"""\ny = 1\nz = 2\n', "y = 1 z = 2"),
    ],
)
def test_whitespace_that_changes_python_misses_whatever_the_tokens(
    tmp_path: Path, source: str, old: str
) -> None:
    (tmp_path / "f.py").write_text(source)
    result = apply(
        tmp_path, f"--- f.py\n<<<<<<< SEARCH\n{old}\n=======\nnew\n>>>>>>> REPLACE\n"
    )
    assert result.returncode == 1, (tmp_path / "f.py").read_text()
    assert (tmp_path / "f.py").read_text() == source


def test_a_backslash_continuation_is_a_line_break_python_ignores(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.py").write_text("x = 1 + \\\n    2\ny = 3\n")
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\nx = 1 + 2\n=======\nx = 3\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "x = 3\ny = 3\n"


def test_a_loose_match_keeps_a_crlf_files_line_endings(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_bytes(
        b"def f():\r\n    return g(a,\r\n             b)\r\n"
    )
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\n    return g(a, b)\n=======\n    return 0\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_bytes() == b"def f():\r\n    return 0\r\n"


def test_a_stub_file_is_python_too(tmp_path: Path) -> None:
    (tmp_path / "f.pyi").write_text("def f(a: int,\n      b: int) -> int: ...\n")
    result = apply(
        tmp_path,
        "--- f.pyi\n<<<<<<< SEARCH\ndef f(a: int, b: int) -> int: ...\n=======\ndef f() -> int: ...\n"
        ">>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_loose_match_found_twice_names_both_lines(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = f(a,\n      b)\ny = 1\nx = f(a,\n  b)\n")
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\nx = f(a, b)\n=======\nx = 0\n>>>>>>> REPLACE\n",
    )
    assert "at lines 1, 4" in result.stderr


@pytest.mark.parametrize(
    "source, old",
    [
        ("x = 1\n", "x =\n1"),  # a line break outside brackets splits the statement
        ("def f():\n    return x\n", "    returnx"),  # two words need their space
        ('print(f"{x = }")\n', 'print(f"{x=}")'),  # an f-string prints its own spacing
    ],
)
def test_whitespace_python_reads_still_misses(
    tmp_path: Path, source: str, old: str
) -> None:
    (tmp_path / "f.py").write_text(source)
    result = apply(
        tmp_path, f"--- f.py\n<<<<<<< SEARCH\n{old}\n=======\nnew\n>>>>>>> REPLACE\n"
    )
    assert result.returncode == 1
    assert (tmp_path / "f.py").read_text() == source


def test_an_old_text_with_a_backslash_continuation_matches_the_joined_line(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.py").write_text("x = 1 + 2\n")
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\nx = 1 + \\\n    2\n=======\nx = 3\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "x = 3\n"


def test_a_comment_line_between_statements_matches_loosely(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("f(a,\n  b)\n# note\ny = 2\n")
    result = apply(
        tmp_path,
        "--- f.py\n<<<<<<< SEARCH\nf(a, b)\n# note\ny = 2\n=======\nz = 1\n>>>>>>> REPLACE\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "z = 1\n"


def test_a_comment_in_brackets_runs_to_the_end_of_its_line(tmp_path: Path) -> None:
    source = "f(\n    a,  # note\n    b,\n)\n"
    (tmp_path / "f.py").write_text(source)
    # the old text's comment takes in the `b,` after it
    old = "f(\n    a,  # note b,\n)"
    result = apply(
        tmp_path, f"--- f.py\n<<<<<<< SEARCH\n{old}\n=======\nnew\n>>>>>>> REPLACE\n"
    )
    assert result.returncode == 1, (tmp_path / "f.py").read_text()
    assert (tmp_path / "f.py").read_text() == source


def loose(
    tmp_path: Path, name: str, source: str, old: str, new: str = "new"
) -> subprocess.CompletedProcess[str]:
    """Apply one block to a file holding `source`."""
    (tmp_path / name).write_text(source)
    return apply(
        tmp_path,
        f"--- {name}\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n",
    )


FORMATTED = """\
def show(results, layer):
    rows = []
    for L, (a, b, c) in results:
        rows.append([f"{L}{' *' if L == layer else ''}", f"{a:.3f}", f"{b:.3f}", f"{c:.3f}"])
    print(
        table(
            [
                "layer",
                "held-out advice e8",
                "aesthetics unpopular vs popular",
                "insecure vs ChatDoctor",
            ],
            rows,
        )
    )
    return rows
"""
"""A file as `ruff format` left it, with the print call exploded."""

AS_WRITTEN = """\
        rows.append([f"{L}{' *' if L == layer else ''}", f"{a:.3f}", f"{b:.3f}", f"{c:.3f}"])
    print(table(["layer", "held-out advice e8", "aesthetics unpopular vs popular", "insecure vs ChatDoctor"], rows))"""
"""The same lines as they were written, before ruff format."""


def test_a_block_written_before_ruff_exploded_a_call_matches(tmp_path: Path) -> None:
    result = loose(
        tmp_path,
        "f.py",
        FORMATTED,
        AS_WRITTEN,
        "        rows.append([L, a, b, c])\n    print(table(HEADER, rows))",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == (
        "def show(results, layer):\n    rows = []\n    for L, (a, b, c) in results:\n"
        "        rows.append([L, a, b, c])\n    print(table(HEADER, rows))\n    return rows\n"
    )
    assert result.stdout == (
        "f.py: 1 edit (block 1 matched across formatting that doesn't change the Python)\n"
    )


def test_a_trailing_comma_the_file_lacks_matches(tmp_path: Path) -> None:
    result = loose(tmp_path, "f.py", "f(a, b)\n", "f(\n    a,\n    b,\n)")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "new\n"


@pytest.mark.parametrize(
    "source, old",
    [
        ("t = (x,)\n", "t = (x)"),  # a tuple isn't its one item
        ("t = (x)\n", "t = (x,)"),
        ("y = a[x,]\n", "y = a[x]"),  # nor is a subscript by a tuple
        ("y = a[x]\n", "y = a[x,]"),
    ],
)
def test_a_comma_that_changes_the_python_still_misses(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 1, (tmp_path / "f.py").read_text()
    assert (tmp_path / "f.py").read_text() == source


def test_a_formatter_match_needs_a_file_that_parses(tmp_path: Path) -> None:
    source = "f(\n    a,\n)\nx = = 1\n"
    result = loose(tmp_path, "f.py", source, "f(a)")
    assert result.returncode == 1
    assert (tmp_path / "f.py").read_text() == source


@pytest.mark.parametrize(
    "source, old",
    [
        ("let t = (\n    x,\n);\n", "let t = (x);"),  # a tuple isn't its one item
        ("let t = (x);\n", "let t = (\n    x,\n);"),
        ("let t = (x,);\n", "let t = (x);"),
        # a formatter adds a comma only on a line of its own, so nothing else matches
        ("let t = g(x,);\n", "let t = g(x);"),
    ],
)
def test_other_files_keep_a_comma_that_makes_a_tuple(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.rs", source, old)
    assert result.returncode == 1, (tmp_path / "f.rs").read_text()
    assert (tmp_path / "f.rs").read_text() == source


@pytest.mark.parametrize(
    "source, old",
    [
        ('x = "a"\n', "x = 'a'"),
        ('y = "it\'s"\n', "y = 'it\\'s'"),
        ("z = f\"{d['k']} {x!r}\"\n", "z = f'{d[\"k\"]} {x!r}'"),
        ('w = "b"\n', "w = u'b'"),
        ("v = 0xFF + 1e5\n", "v = 0XFF + 1E5"),
    ],
    ids=["quotes", "escapes", "f-string", "u prefix", "number case"],
)
def test_a_literal_ruff_respelled_matches(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "new\n"


@pytest.mark.parametrize(
    "source, old",
    [
        ('x = "a"\n', "x = b'a'"),  # bytes aren't text
        ('x = "a"\n', "x = 'A'"),
        ("x = 1\n", "x = 1.0"),
    ],
)
def test_a_literal_with_another_value_still_misses(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 1
    assert (tmp_path / "f.py").read_text() == source


def test_a_block_that_differs_from_the_formatted_code_shows_the_closest_text(
    tmp_path: Path,
) -> None:
    result = loose(
        tmp_path, "f.py", FORMATTED, AS_WRITTEN.replace("advice e8", "advice e9")
    )
    assert result.returncode == 1
    assert "f.py block 1 ('rows.append(" in result.stderr
    assert (
        "old text not found" in result.stderr
        and "closest is lines 4-5" in result.stderr
    )
    assert (tmp_path / "f.py").read_text() == FORMATTED


@pytest.mark.parametrize(
    "source, old",
    [
        # ruff wrapped a long condition in parentheses
        (
            "cond = (\n    first(a)\n    and second(b)\n    or third(c)\n)\nx = 1\n",
            "cond = first(a) and second(b) or third(c)\nx = 1",
        ),
        ("x = (\n    a\n    and b\n)\n", "x = a and b"),
        # ruff took out parentheses that did nothing
        ("if a:\n    pass\nq = 1\n", "if (a):\n    pass\nq = (1)"),
        ("def f():\n    return a + b\n", "def f():\n    return (a +\n            b)"),
        # its own parenthesis closes before the comma both sides have
        (
            "assert (\n    r.code\n    == 1\n), (\n    tmp / 'f'\n).read_text()\n",
            "assert r.code == 1, (tmp / 'f').read_text()",
        ),
    ],
    ids=[
        "added",
        "added at the end",
        "removed",
        "removed with a line break",
        "added before a comma",
    ],
)
def test_parentheses_ruff_added_or_removed_match(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "new\n"


@pytest.mark.parametrize(
    "source, old",
    [
        ("y = x * (a + b)\n", "y = x * a + b"),
        ("y = x * a + b\n", "y = x * (a + b)"),
        ("f((a, b))\n", "f(a, b)"),  # one tuple isn't two arguments
    ],
)
def test_parentheses_that_change_the_python_still_miss(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 1, (tmp_path / "f.py").read_text()
    assert (tmp_path / "f.py").read_text() == source


def test_a_block_inside_parentheses_ruff_added_matches_once(tmp_path: Path) -> None:
    result = loose(tmp_path, "f.py", "x = (\n    a\n    and b\n)\n", "a and b")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == "x = (\n    new\n)\n"


def test_indentation_still_counts_in_a_formatter_match(tmp_path: Path) -> None:
    source = "if x:\n    f(\n        a,\n    )\nz()\n"
    result = loose(tmp_path, "f.py", source, "if x:\n    f(a)\n    z()")
    assert result.returncode == 1
    assert (tmp_path / "f.py").read_text() == source


@pytest.mark.parametrize(
    "source, old, after",
    [
        (
            (
                "def f(base):\n    if base:\n        (\n            hunk.base,\n"
                '            section,\n        ) = [], "base"\n    return section\n'
            ),
            '        hunk.base, section = [], "base"',
            "def f(base):\n    if base:\n        new\n    return section\n",
        ),
        ("(\n    a,\n    b,\n) = c\n", "a, b = c", "new\n"),
    ],
    ids=["indented", "top level"],
)
def test_a_statement_ruff_opened_with_a_parenthesis_matches(
    tmp_path: Path, source: str, old: str, after: str
) -> None:
    indent = old[: len(old) - len(old.lstrip())]
    result = loose(tmp_path, "f.py", source, old, f"{indent}new")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == after


@pytest.mark.parametrize(
    "source, old",
    [
        # a comment after code isn't one on a line of its own
        ("x = f(\n    a,\n)  # about x\ny = 2\n", "x = f(a)\n# about x\ny = 2"),
        # nor is a comment line at another indentation
        (
            "if a:\n    x = f(\n        b,\n    )\n    # inside\ny = 2\n",
            "if a:\n    x = f(b)\n# inside\ny = 2",
        ),
    ],
)
def test_comment_lines_still_count_in_a_formatter_match(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.py", source, old)
    assert result.returncode == 1, (tmp_path / "f.py").read_text()
    assert (tmp_path / "f.py").read_text() == source


WRITTEN = """\
def report(results, layer, width=3):
    rows = [[name, f'{score:.{width}f}', 'yes' if ok else 'no'] for name, score, ok in results if score > 0.5]
    header = ('layer', 'held-out advice', 'aesthetics unpopular vs popular', 'insecure vs ChatDoctor')
    total = sum(score for _, score, _ in results)  # every score, for the footer
    if (layer is not None and layer in [row[0] for row in rows] and len(rows) > 10):
        print(table(header, rows, title=f'layer {layer}', highlight=layer, width=0XFF))
    first, second, third_value_name = rows[0], rows[1], rows[2] if len(rows) > 2 else None
    assert first[0] != second[0], f'duplicate layer {first[0]} in the report for {layer}'
    return (first, second, total)
"""
"""Code as an agent wrote it, before ruff format wrapped, quoted and parenthesized it."""


def test_every_statement_written_before_ruff_format_still_applies(
    tmp_path: Path,
) -> None:
    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--line-length", "40", "-"],
        input=WRITTEN,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    (tmp_path / "f.py").write_text(formatted)
    statements = WRITTEN.splitlines()[1:]
    blocks = "".join(
        f"<<<<<<< SEARCH\n{line}\n=======\n{line}\n>>>>>>> REPLACE\n"
        for line in statements
    )

    result = apply(tmp_path, f"--- f.py\n{blocks}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.py").read_text() == WRITTEN


RUSTFMT_OUT = """\
fn check(state: &State, form: &Form) -> Result<()> {
    if form.token.is_empty() {
        let found = lookup(
            &state.pool,
            &form.token,
            "a  spaced  label",
        );
        assert!(
            found.contains("unrecognised positive_control.checks"),
            "{found}"
        );
        let date = Date::parse(
            found.trim(),
            format_description!("[year]-[month]-[day]"),
        )
        .ok()?;
        record(
            date.expect("a date the parser read once already, so it can't fail now"),
        );
    }
    Ok(())
}
"""
"""Rust as rustfmt leaves it: calls split across lines, deeper indentation."""


@pytest.mark.parametrize(
    "old, first, last",
    [
        # a call split across lines, with the trailing comma rustfmt adds, and the
        # indentation the agent wrote before the code moved into an `if`
        (
            '    let found = lookup(&state.pool, &form.token, "a  spaced  label");',
            3,
            7,
        ),
        # a macro call rustfmt split without a trailing comma
        (
            '    assert!(found.contains("unrecognised positive_control.checks"), "{found}");',
            8,
            11,
        ),
        # a method chain rustfmt broke before the `.`
        (
            '    let date = Date::parse(found.trim(), format_description!("[year]-[month]-[day]")).ok()?;',
            12,
            16,
        ),
        # a call of one argument, which rustfmt gave a trailing comma
        (
            '        record(date.expect("a date the parser read once already, so it can\'t fail now"));',
            17,
            19,
        ),
        # a `{` on a line of its own
        ("fn check(state: &State, form: &Form) -> Result<()>\n{", 1, 1),
    ],
    ids=["call", "macro", "chain", "one argument", "brace"],
)
def test_a_block_written_before_rustfmt_rewrapped_it_matches(
    tmp_path: Path, old: str, first: int, last: int
) -> None:
    result = loose(tmp_path, "f.rs", RUSTFMT_OUT, old, "    replaced();")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = RUSTFMT_OUT.splitlines(keepends=True)
    assert (tmp_path / "f.rs").read_text() == "".join(
        [*lines[: first - 1], "    replaced();\n", *lines[last:]]
    )
    assert (
        result.stdout
        == "f.rs: 1 edit (block 1 matched ignoring whitespace and trailing commas)\n"
    )


def test_a_loose_match_puts_the_new_text_in_as_written(tmp_path: Path) -> None:
    source = "fn f() {\n    if x {\n        g(\n            a,\n            b,\n        );\n    }\n}\n"
    result = loose(tmp_path, "f.rs", source, "    g(a, b);", "    h(a,b);")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.rs").read_text() == (
        "fn f() {\n    if x {\n    h(a,b);\n    }\n}\n"
    )


def test_a_rustfmt_rewrap_matches_after_real_rustfmt(tmp_path: Path) -> None:
    rustfmt = shutil.which("rustfmt")
    if rustfmt is None:
        pytest.skip("rustfmt isn't on PATH")
    written = (
        "fn f(state: &State) {\n"
        '    let reply = state.client.post("/entries").header("x", "y").body(text.clone()).send().await.unwrap();\n'
        '    for value in [date_in(100), "next spring".to_string(), "2026-13-40".to_string()] {\n'
        "        check(value);\n"
        "    }\n"
        "}\n"
    )
    formatted = subprocess.run(
        [rustfmt, "--edition", "2021", "--emit", "stdout"],
        input=written,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert formatted != written
    (tmp_path / "f.rs").write_text(formatted)
    blocks = "".join(
        f"<<<<<<< SEARCH\n{line}\n=======\n{line}\n>>>>>>> REPLACE\n"
        for line in written.splitlines()[1:3]
    )

    result = apply(tmp_path, f"--- f.rs\n{blocks}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        (tmp_path / "f.rs")
        .read_text()
        .startswith(
            "fn f(state: &State) {\n" + "".join(written.splitlines(keepends=True)[1:3])
        )
    )


@pytest.mark.parametrize(
    "source, old, new, result",
    [
        # a block moved deeper, every line by the same step
        (
            "fn f() {\n    if c {\n        g(\n            a,\n        );\n        h();\n    }\n}\n",
            "    g(a);\n    h();",
            "    k();",
            "fn f() {\n    if c {\n    k();\n    }\n}\n",
        ),
        # an empty new text deletes the lines, line break and all
        (
            "fn f() {\n    g(\n        a,\n    );\n    k();\n}\n",
            "    g(a);",
            "",
            "fn f() {\n    k();\n}\n",
        ),
        # brackets that hold two items aren't a tuple of one
        ("let t = (\n    a,\n    b,\n);\n", "let t = (a, b);", "t", "t\n"),
        # a `{` on its own line after a signature, an operator leading a line
        ("fn f(a: u8)\n{\n", "fn f(a: u8) {", "g", "g\n"),
        ("let ok = first\n    && second;\n", "let ok = first && second;", "t", "t\n"),
        # a block's closer on its own line
        (
            "let v = if c {\n    a\n} else {\n    b\n};\n",
            "let v = if c { a } else { b };",
            "t",
            "t\n",
        ),
    ],
    ids=["moved deeper", "deleted", "two items", "brace", "operator", "closer"],
)
def test_more_rewraps_match_in_other_files(
    tmp_path: Path, source: str, old: str, new: str, result: str
) -> None:
    done = loose(tmp_path, "f.rs", source, old, new)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "f.rs").read_text() == result


def test_a_loose_match_reads_a_crlf_files_strings_across_lines(tmp_path: Path) -> None:
    (tmp_path / "f.rs").write_bytes(b'let s = "a\r\nb";\r\nf(x,\r\n  y);\r\n')
    result = apply(
        tmp_path,
        '--- f.rs\n<<<<<<< SEARCH\nlet s = "a\nb";\nf(x, y);\n=======\nnew\n>>>>>>> REPLACE\n',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "f.rs").read_bytes() == b"new\r\n"


def test_a_loose_match_found_twice_in_another_file_needs_more_context(
    tmp_path: Path,
) -> None:
    source = (
        "fn f() {\n    g(\n        a,\n        b,\n    );\n    g(a,\n      b);\n}\n"
    )
    result = loose(tmp_path, "f.rs", source, "g(a, b);")
    assert result.returncode == 1
    assert (
        "found 2 times (ignoring whitespace and trailing commas), at lines 2, 6"
        in result.stderr
    )
    assert (tmp_path / "f.rs").read_text() == source


@pytest.mark.parametrize(
    "source, old",
    [
        # whitespace inside a string, in any of its quotes
        ('let s = f("a  b",\n    c);\n', 'let s = f("a b", c);'),
        ("let s = f(`a\n  b`,\n    c);\n", "let s = f(`a\n b`, c);"),
        ("let c = f('a  b',\n    c);\n", "let c = f('a b', c);"),
        # and inside a comment, or across the line break that ends one
        ("x = f(a,\n  b) // a  note\n", "x = f(a, b) // a note"),
        ("// note\nlet x = 1;\n", "// note let x = 1;"),
        ("# note\nx = 1\n", "# note x = 1"),
        # a line break between two words separates them: a shell's two commands
        ("make build\nmake test\n", "make build make test"),
        # spaces that separate tokens, or join them
        ("let y = a - -b;\n", "let y = a --b;"),
        ("x = a + = b\n", "x = a += b"),
        ('let x = r "a";\n', 'let x = r"a";'),
        # a line break between statements, or after a block
        ("a();\nb();\n", "a(); b();"),
        ("}\nfoo();\n", "} foo();"),
        # a backslash continuation is a line break other files may not ignore, and a
        # backslash before a space may escape it
        ("run \\\n  --flag\n", "run --flag"),
        ("run \\\n  --flag\n", "run \\ --flag"),
        # a comment's line break may end a preprocessor line
        ("f(a,\n  b)\n#define X 1\n", "f(a, b) #define X 1"),
        # an indented old text is a line's start, not a statement mid-line
        ("fn f() { g(a,\n    b); }\n", "    g(a, b);"),
        # a block's line break, even inside parentheses, may end a statement
        ("foo(func() {\n\tx := a\n\t(*b).c()\n})\n", "foo(func() { x := a (*b).c() })"),
        # a line break after a word may end a statement before punctuation
        ("return\n{ a: 1 };\n", "return { a: 1 };"),
        ("val x = a\n- b\n", "val x = a - b"),
        ("make\n-j4\n", "make -j4"),
        # a space next to an operator may matter
        ("x=1\nf(a,\n  b)\n", "x = 1\nf(a, b)"),
        ("run --flag\nf(a,\n  b)\n", "run -- flag\nf(a, b)"),
        # strings a lexer may misread still keep their spaces
        ("msg = '''\na  b\n'''\n", "msg = '''\na b\n'''"),
        ("it's 'a  b' ok\n", "it's 'a b' ok"),
        ('let p = r"C:\\";\nlet s = f("a  b", c);\n', 'let s = f("a b", c);'),
        ("const re = /'/; f('a  b')\n", "const re = /'/; f('a b')"),
        # indentation that carries meaning: a recipe's tab, a key's level
        ("all:\n\tgcc -o x x.c\n", "    gcc -o x x.c"),
        ("a:\n  b: 1\n  c: 2\n", "a:\n  b: 1\nc: 2"),
        # a call's parentheses come straight after its name
        ("return (\n    x,\n);\n", "return (x);"),
        # an old text that starts with a blank line has no line to start at
        ("fn f() {\n\n    g(\n        a,\n    );\n}\n", "\n    g(a);"),
    ],
)
def test_whitespace_that_can_change_meaning_still_misses_in_other_files(
    tmp_path: Path, source: str, old: str
) -> None:
    result = loose(tmp_path, "f.txt", source, old)
    assert result.returncode == 1, (tmp_path / "f.txt").read_text()
    assert "closest is" in result.stderr
    assert (tmp_path / "f.txt").read_text() == source
