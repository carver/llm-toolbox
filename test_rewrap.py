"""rewrap reflows docstring paragraphs and comment blocks that run past the line
length."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "rewrap"


def rewrap(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args], cwd=cwd, text=True, capture_output=True, check=False
    )


def widest(text: str) -> int:
    return max(len(line) for line in text.splitlines())


LONG = "word " * 30  # 150 characters of prose


def test_a_long_docstring_paragraph_is_reflowed_and_a_short_one_kept(tmp_path):
    source = (
        "def f():\n"
        f'    """{LONG.strip()}\n'
        "\n"
        "    A short paragraph that already fits.\n"
        '    """\n'
        "    return 1\n"
    )
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "40", "a.py")

    assert done.returncode == 0, done.stdout + done.stderr
    text = path.read_text()
    assert widest(text) <= 40
    assert text.startswith('def f():\n    """word word')
    assert (
        '\n\n    A short paragraph that already fits.\n    """\n    return 1\n' in text
    )
    assert text.split() == source.split()  # the same words, in order
    assert "a.py" in done.stdout


def test_the_closing_quotes_stay_after_the_last_word(tmp_path):
    path = tmp_path / "a.py"
    path.write_text(f'"""{LONG.strip()}"""\n')

    rewrap(tmp_path, "--width", "40", "a.py")

    text = path.read_text()
    assert text.rstrip("\n").endswith('word"""') and widest(text) <= 40


def test_a_long_comment_block_is_reflowed_at_its_indent(tmp_path):
    path = tmp_path / "a.py"
    path.write_text(f"def f():\n    # {LONG.strip()}\n    # and more\n    return 1\n")

    rewrap(tmp_path, "--width", "40", "a.py")

    lines = path.read_text().splitlines()
    comments = [line for line in lines if line.lstrip().startswith("#")]
    assert len(comments) > 2 and all(line.startswith("    # ") for line in comments)
    assert widest(path.read_text()) <= 40
    assert lines[-1] == "    return 1"


def test_examples_list_items_and_doctests_are_left_as_they_are(tmp_path):
    kept = [
        f"        uv run tool --flag {'x' * 60}",
        f"    - a list item {'y ' * 30}",
        f"    >>> call({'z' * 60})",
    ]
    source = 'def f():\n    """Summary.\n\n' + "\n".join(kept) + '\n    """\n'
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text() == source
    assert done.returncode == 1  # the long lines are left, and listed
    for number in (4, 5, 6):
        assert f"a.py:{number}:" in done.stdout


def test_code_and_strings_are_never_touched_but_listed(tmp_path):
    source = f'x = "{"s" * 60}"  # {"c" * 30}\n'
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text() == source
    assert done.returncode == 1 and "a.py:1:" in done.stdout


def test_the_width_comes_from_ruffs_line_length(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 50\n")
    (tmp_path / "pkg").mkdir()
    path = tmp_path / "pkg" / "a.py"
    path.write_text(f"# {LONG.strip()}\n")

    rewrap(tmp_path, "pkg/a.py")

    assert 40 < widest(path.read_text()) <= 50


def test_a_ruff_toml_sets_the_width_too(tmp_path):
    (tmp_path / "ruff.toml").write_text("line-length = 60\n")
    path = tmp_path / "a.py"
    path.write_text(f"# {LONG.strip()}\n")

    rewrap(tmp_path, "a.py")

    assert 50 < widest(path.read_text()) <= 60


def test_without_a_ruff_setting_the_width_is_88(tmp_path):
    path = tmp_path / "a.py"
    path.write_text(f"# {(LONG * 2).strip()}\n")

    rewrap(tmp_path, "a.py")

    assert 80 < widest(path.read_text()) <= 88


def test_check_reports_without_writing(tmp_path):
    source = f"# {LONG.strip()}\n"
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--check", "--width", "40", "a.py")

    assert path.read_text() == source
    assert done.returncode == 1 and "a.py" in done.stdout


def test_a_script_metadata_block_is_left_as_it_is(tmp_path):
    source = (
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        f"# dependencies = [{', '.join(f'"package{i}"' for i in range(9))}]\n"
        "# ///\n"
    )
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "60", "a.py")

    assert path.read_text() == source and "a.py:3:" in done.stdout


def test_short_paragraphs_and_comment_blocks_keep_their_line_breaks(tmp_path):
    source = (
        "def f():\n"
        f'    """{LONG.strip()}\n'
        "\n"
        "    Two short lines\n"
        "    that would join.\n"
        '    """\n'
        "    # two short\n"
        "    # comment lines\n"
        "    return 1\n"
    )
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    text = path.read_text()
    assert "    Two short lines\n    that would join.\n" in text
    assert "    # two short\n    # comment lines\n" in text


def test_the_closing_quotes_count_toward_the_width(tmp_path):
    path = tmp_path / "a.py"
    path.write_text(
        '"""' + "word " * 7 + 'wordy"""\n'
    )  # 46 characters: 43 without the quotes

    rewrap(tmp_path, "--width", "44", "a.py")

    assert widest(path.read_text()) <= 44


@pytest.mark.parametrize(
    "source",
    [
        f'def f(): """{LONG.strip()}"""\n',
        f'x = 1\n"""{LONG.strip()}"""  # noqa: E501\n',
        f'"""{LONG.strip()}"""; x = 1\n',
        f'"""{LONG.strip()}""" "tail"\n',
    ],
)
def test_a_docstring_sharing_its_line_with_code_is_left_as_it_is(tmp_path, source):
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text() == source and done.returncode == 1


def test_a_bare_opening_line_stays_bare(tmp_path):
    source = f'def f():\n    """\n    {LONG.strip()}\n    """\n'
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    text = path.read_text()
    assert text.startswith('def f():\n    """\n    word') and widest(text) <= 40


def test_windows_line_endings_are_kept(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(f"x = 1\r\n# {LONG.strip()}\r\ny = 2\r\n".encode())

    rewrap(tmp_path, "--width", "40", "a.py")

    data = path.read_bytes()
    assert data.startswith(b"x = 1\r\n# word") and data.endswith(b"\r\ny = 2\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_a_file_that_cant_be_read_is_reported_and_the_rest_still_rewrapped(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"# caf\xe9\n")
    (tmp_path / "open.py").write_text("x = (\n")
    good = tmp_path / "good.py"
    good.write_text(f"# {LONG.strip()}\n")

    done = rewrap(tmp_path, "--width", "40", "bad.py", "open.py", "good.py")

    assert done.returncode == 2
    assert "bad.py" in done.stderr and "open.py" in done.stderr
    assert widest(good.read_text()) <= 40


def test_a_paragraph_that_cant_be_refilled_shorter_is_reported_not_rewritten(tmp_path):
    source = f"# https://example.com/{'x' * 60}\n"
    path = tmp_path / "a.py"
    path.write_text(source)

    done = rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text() == source
    assert "rewrapped" not in done.stdout and "a.py:1:" in done.stdout


def test_check_lists_the_lines_as_numbered_on_disk(tmp_path):
    path = tmp_path / "a.py"
    path.write_text(f"# {LONG.strip()}\nx = '{'s' * 60}'\n")

    done = rewrap(tmp_path, "--check", "--width", "40", "a.py")

    assert "a.py:2:" in done.stdout


@pytest.mark.parametrize(
    "kept",
    [
        f"    1. a numbered item {'w ' * 30}",
        f"    :param x: a field {'w ' * 30}",
        f"    Parameters {'w ' * 30}\n    ----------",
    ],
)
def test_numbered_items_fields_and_section_titles_are_kept(tmp_path, kept):
    source = f'def f():\n    """Summary.\n\n{kept}\n    """\n'
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text() == source


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa: E501",
        "# pyright: strict",
        "# pylint: disable=x",
        "# mypy: ignore",
        "# isort: skip",
        "# %%",
    ],
)
def test_a_directive_comment_isnt_merged_into_prose(tmp_path, comment):
    source = f"{comment}\n# {LONG.strip()}\n"
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    assert path.read_text().startswith(f"{comment}\n# word")


def test_comment_lines_indented_deeper_or_at_another_indent_stay_apart(tmp_path):
    source = f"# {LONG.strip()}\n#     code(sample)\nif x:\n    # inner\n    pass\n"
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    text = path.read_text()
    assert "\n#     code(sample)\n" in text and "\n    # inner\n" in text


def test_a_dot_ruff_toml_sets_the_width_too(tmp_path):
    (tmp_path / ".ruff.toml").write_text("line-length = 60\n")
    path = tmp_path / "a.py"
    path.write_text(f"# {LONG.strip()}\n")

    rewrap(tmp_path, "a.py")

    assert 50 < widest(path.read_text()) <= 60


def test_adjacent_comments_at_two_indents_stay_apart(tmp_path):
    source = f"if x:\n    # {LONG.strip()}\n# outer\n    pass\n"
    path = tmp_path / "a.py"
    path.write_text(source)

    rewrap(tmp_path, "--width", "40", "a.py")

    assert "\n# outer\n    pass\n" in path.read_text()
