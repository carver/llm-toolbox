"""bin/shape, run as an agent runs it: files in a scratch folder, or stdin."""

import json
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "shape"
# Planted in values and in keys that aren't field names: none may reach the output.
SECRET = "hunter2"


def shape(cwd: Path, *args: str, stdin: str | bytes | None = None):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=cwd,
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        capture_output=True,
        check=False,
    )


def out(result) -> str:
    return result.stdout.decode()


def err(result) -> str:
    return result.stderr.decode()


def lines(*paths: str) -> str:
    return "".join(f"  {path}\n" for path in paths)


def test_json_shows_each_path_with_its_type_and_none_of_its_values(tmp_path):
    (tmp_path / "runs.json").write_text(
        json.dumps(
            {
                "name": SECRET,
                "runs": [
                    {"id": f"{SECRET}-1", "score": 0.4375, "tags": ["a", "b"]},
                    {"id": f"{SECRET}-2", "score": None, "tags": []},
                    {"id": f"{SECRET}-3", "tags": ["c", "d", "e", "f"]},
                ],
                "count": 98765,
            }
        )
    )

    result = shape(tmp_path, "runs.json")

    assert result.returncode == 0, err(result)
    assert out(result) == "runs.json: json\n" + lines(
        ". map",
        ".name str",
        ".runs list[3]",
        ".runs[] map",
        ".runs[].id str",
        ".runs[].score float|null (2 of 3)",
        ".runs[].tags list[0..4]",
        ".runs[].tags[] str",
        ".count int",
    )
    for value in (SECRET, "0.4375", "98765"):
        assert value not in out(result) + err(result)


def test_yaml_names_its_own_types(tmp_path):
    (tmp_path / "types.yaml").write_text(
        "when: 2026-11-07\n"
        "at: 2026-11-07T10:00:00Z\n"
        "ok: yes\n"
        "n: 3\n"
        "x: 1.5\n"
        "blob: !!binary aGVsbG8=\n"
        "none: ~\n"
    )

    result = shape(tmp_path, "types.yaml")

    assert result.returncode == 0, err(result)
    assert out(result) == "types.yaml: yaml\n" + lines(
        ". map",
        ".when date",
        ".at datetime",
        ".ok bool",
        ".n int",
        ".x float",
        ".blob bytes",
        ".none null",
    )


def test_a_yaml_stream_of_several_documents_is_a_list_of_them(tmp_path):
    (tmp_path / "docs.yml").write_text(f"a: 1\n---\na: {SECRET}\nb: [1]\n")

    result = shape(tmp_path, "docs.yml")

    assert result.returncode == 0, err(result)
    assert out(result) == "docs.yml: yaml, 2 documents\n" + lines(
        ". list[2]",
        ".[] map",
        ".[].a int|str",
        ".[].b list[1] (1 of 2)",
        ".[].b[] int",
    )


def test_a_path_holding_lists_maps_and_scalars_joins_their_types(tmp_path):
    (tmp_path / "mixed.json").write_text('[1, {"a": 2}, [3, 4]]')

    result = shape(tmp_path, "mixed.json")

    assert out(result) == "mixed.json: json\n" + lines(
        ". list[3]",
        ".[] int|map|list[2]",
        ".[].a int",
        ".[][] int",
    )


def test_markdown_front_matter_and_how_many_body_lines_follow(tmp_path):
    (tmp_path / "entry.md").write_text(
        f"---\ntitle: {SECRET}\ntags: [a, b]\n---\n{SECRET} body\n\nmore\n"
    )

    result = shape(tmp_path, "entry.md")

    assert result.returncode == 0, err(result)
    assert out(result) == "entry.md: front matter, 3 body lines\n" + lines(
        ". map", ".title str", ".tags list[2]", ".tags[] str"
    )
    assert SECRET not in out(result) + err(result)


def test_front_matter_may_end_with_three_dots(tmp_path):
    (tmp_path / "entry.markdown").write_text("---\ntitle: x\n...\nbody\n")

    result = shape(tmp_path, "entry.markdown")

    assert out(result) == "entry.markdown: front matter, 1 body line\n" + lines(
        ". map", ".title str"
    )


def test_markdown_without_front_matter_says_so(tmp_path):
    (tmp_path / "README.md").write_text(f"# {SECRET}\n\ntext\n")

    result = shape(tmp_path, "README.md")

    assert result.returncode == 0, err(result)
    assert out(result) == "README.md: no front matter, 3 lines\n"


def test_keys_that_arent_field_names_are_counted_under_a_star(tmp_path):
    (tmp_path / "titles.json").write_text(
        json.dumps(
            {
                "by_title": {
                    f"Does {SECRET} hold?": 1,
                    f"{SECRET}@example.com": 2,
                    "2026-11-07": 3,
                },
                "x" * 32: True,
                "y" * 33: True,
                SECRET + "x" * 30: False,
                f"{SECRET}.example": None,
            }
        )
    )

    result = shape(tmp_path, "titles.json")

    assert result.returncode == 0, err(result)
    assert out(result) == "titles.json: json\n" + lines(
        ". map",
        ".by_title map",
        ".by_title.* int (3 keys)",
        f".{'x' * 32} bool",
        ".* bool|null (3 keys)",
    )
    assert SECRET not in out(result) + err(result)


def test_yaml_keys_that_arent_strings_are_hidden_too(tmp_path):
    # Not 1 and true: Python counts them as one key.
    (tmp_path / "keys.yaml").write_text("2: a\ntrue: b\n~: c\n2026-11-07: d\n")

    result = shape(tmp_path, "keys.yaml")

    assert out(result) == "keys.yaml: yaml\n" + lines(". map", ".* str (4 keys)")


def test_a_single_hidden_key_is_one_key(tmp_path):
    (tmp_path / "one.json").write_text(json.dumps({SECRET + " key": 1}))

    assert out(shape(tmp_path, "one.json")) == "one.json: json\n" + lines(
        ". map", ".* int (1 key)"
    )


@pytest.mark.parametrize(
    "text, expected",
    [('{"a": [1]}', "json"), ("a: [1]\n", "yaml")],
)
def test_stdin_and_other_names_are_json_when_they_parse_as_json(
    tmp_path, text, expected
):
    (tmp_path / "reply.txt").write_text(text)

    from_stdin = shape(tmp_path, "-", stdin=text)
    from_file = shape(tmp_path, "reply.txt")

    body = lines(". map", ".a list[1]", ".a[] int")
    assert out(from_stdin) == f"-: {expected}\n" + body
    assert out(from_file) == f"reply.txt: {expected}\n" + body


def test_a_byte_order_mark_is_skipped(tmp_path):
    (tmp_path / "bom.json").write_bytes(b'\xef\xbb\xbf{"a": 1}')

    assert out(shape(tmp_path, "bom.json")) == "bom.json: json\n" + lines(
        ". map", ".a int"
    )


FAILURES = [
    ("bad.json", f'{{"key": "{SECRET}",', r"not valid JSON \(line 1, column \d+\)"),
    (
        "bad.yaml",
        f"key: [{SECRET}\nnext: 1\n",
        r"not valid YAML \(line \d+, column \d+\)",
    ),
    ("tag.yaml", f"key: !{SECRET} x\n", r"not valid YAML \(line 1, column \d+\)"),
    ("alias.yaml", f"key: *{SECRET}\n", r"not valid YAML \(line 1, column \d+\)"),
    ("date.yaml", "when: 2026-13-45\n", r"not valid YAML"),
    ("open.md", f"---\ntitle: {SECRET}\n", r"front matter has no closing --- line"),
    (
        "broken.md",
        f"---\ntitle: ok\nbad: [{SECRET}\n---\nbody\n",
        r"front matter isn't valid YAML \(line [3-5], column \d+\)",
    ),
    ("deep.json", "[" * 100_000 + "]" * 100_000, r"nested too deep"),
    ("loop.yaml", "&a [*a]\n", r"nested too deep"),
]


# Named by file: a test's id goes into PYTEST_CURRENT_TEST, and deep.json's text would
# make the environment too big to start shape with.
@pytest.mark.parametrize("name, text, reason", FAILURES, ids=[f[0] for f in FAILURES])
def test_a_file_that_wont_parse_fails_in_one_line_without_quoting_it(
    tmp_path, name, text, reason
):
    (tmp_path / name).write_text(text)

    result = shape(tmp_path, name)

    assert result.returncode == 1
    assert re.fullmatch(f"shape: {re.escape(name)}: {reason}\n", err(result)), err(
        result
    )
    assert out(result) == ""
    assert SECRET not in err(result)


def test_unreadable_files_fail_in_one_line_each_and_the_rest_are_shown(tmp_path):
    (tmp_path / "binary.json").write_bytes(b'{"a": "\xff\xfe"}')
    (tmp_path / "folder.json").mkdir()
    (tmp_path / "good.json").write_text('{"a": 1}')

    result = shape(tmp_path, "missing.json", "binary.json", "folder.json", "good.json")

    assert result.returncode == 1
    assert err(result) == (
        "shape: missing.json: no such file\n"
        "shape: binary.json: not UTF-8 text\n"
        "shape: folder.json: is a directory\n"
    )
    assert out(result) == "good.json: json\n" + lines(". map", ".a int")


def test_no_file_is_a_usage_error(tmp_path):
    assert shape(tmp_path).returncode == 2


def test_help_shows_the_output_format(tmp_path):
    result = shape(tmp_path, "-h")

    assert result.returncode == 0
    assert ".runs[].score float|null (2 of 3)" in out(result)
