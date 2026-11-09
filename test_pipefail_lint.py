"""pipefail-lint flags pipelines whose reader can stop early, in shell scripts that set
pipefail, where the writer's SIGPIPE fails the pipeline."""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "pipefail-lint"
PIPEFAIL = "#!/bin/bash\nset -euo pipefail\n"
FINDING = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): ")

# Each case is a snippet of a script that sets pipefail, and the line of the snippet,
# counted from 0, that pipefail-lint flags, or None when it flags none.
CASES = [
    # The case that refused a landing: grep -q matched and exited, gh died of SIGPIPE.
    ("gh api repos/o/r/rulesets --jq '.[].name' | grep -qx main", 0),
    ("cmd | grep --quiet x", 0),
    ("cmd | grep --silent x", 0),
    ("cmd | grep -m1 x", 0),
    ("cmd | grep -m 1 x", 0),
    ("cmd | grep --max-count=1 x", 0),
    ("cmd | grep --max-count 1 x", 0),
    ("cmd | egrep -l x", 0),
    ("cmd | fgrep -L x", 0),
    ("cmd | grep --files-with-matches x", 0),
    ("cmd | grep --files-without-match x", 0),
    ("cmd | rg -q x", 0),
    ("cmd | rg --max-count 2 x", 0),
    ("cmd | head", 0),
    ("cmd | head -1", 0),
    ("cmd | head -n 300", 0),
    ("cmd | head -c 10", 0),
    ("cmd | head --lines=4", 0),
    ("cmd | sed q", 0),
    ("cmd | sed 1q", 0),
    ("cmd | sed -n '/x/{p;q}'", 0),
    ("cmd | sed -e 's/a/b/' -e 5Q", 0),
    ("cmd | sed -n '/^v/ { s/v//p; q }'", 0),
    ("cmd | awk '{ print $1; exit }'", 0),
    ("cmd | awk 'BEGIN { exit 1 }'", 0),
    ("cmd | awk -F: '/x/ { found = 1; exit } END { exit !found }'", 0),
    ("cmd | read -r first", 0),
    ("cmd | cmp -s - expected.txt", 0),
    ("x=$(cmd | head -1)", 0),
    ("f() { local y; y=$(cmd | grep -c x | head -1); }", 0),
    ("if cmd | grep -q x; then :; fi", 0),
    ("cmd | head -5 | sort", 0),
    ("cmd |& grep -q x", 0),
    ("cmd | timeout 5 grep -q x", 0),
    ("cmd | LC_ALL=C grep -q x", 0),
    ("cmd | env LC_ALL=C head -1", 0),
    ("cmd | grep -q x 2>/dev/null", 0),
    ("! cmd | grep -q x", 0),
    ("cmd \\\n  | grep -q x", 1),
    ("cat <<EOF | grep -q y\nbody\nEOF", 0),
    ("cmd | head -1 || echo failed", 0),
    ("cmd | head -1 | sort || echo none", 0),
    ("cmd | grep -m1 x | cut -d: -f2 && echo found", 0),
    ("cmd |\n  # why\n  grep -q x", 2),
    ("arr=($(cmd | head -1))", 0),
    ("cmd | builtin read -r a", 0),
    ("cmd | head -qn 2", 0),
    ("cmd | sed -n '$!{p;q}'", 0),
    (
        (
            "producer | while read -r l; do\n"
            "  inner | grep -q x  # pipefail-lint: ignore\n"
            "done | head -1"
        ),
        2,
    ),
    ('a=$(cmd | head -1) && echo "$a"', 0),
    ("# pipefail-lint: ignore\n\ncmd | grep -q x", 2),
    # A pipeline whose status nothing reads.
    ("cmd | head -1 || true", None),
    ("cmd | grep -q x || :", None),
    ("x=$(cmd | head -1) || true", None),
    ("f() { local v=$(cmd | head -1); }", None),
    ("declare -r w=$(cmd | head -1)", None),
    ("export z=$(cmd | grep -m1 x)", None),
    ('echo "first: $(cmd | head -1)"', None),
    ("LINE=$(cmd | head -1) other-command", None),
    ("[[ $(cmd | head -1) == x ]]", None),
    ("case $(cmd | head -1) in x) : ;; esac", None),
    ("for v in $(cmd | head -3); do :; done", None),
    ("echo $(( $(cmd | head -1) + 1 ))", None),
    ('cat > "$(cmd | head -1)"', None),
    ("cat <<EOF\n$(cmd | head -1)\nEOF", None),
    ("x=$(cmd | head -1; true)", None),
    ("x=$(cmd | head -1) y=2 || true", None),
    ("a && cmd | head -1 || true", None),
    ("{ cmd | head -1; } || true", None),
    ("( cmd | head -1 ) || true", None),
    ("cmd | head -1 | sort || true", None),
    # Readers that read all their input, or don't read it at all.
    ("cmd | grep x", None),
    ("cmd | grep -c x", None),
    ("cmd | grep -s x", None),
    ("cmd | grep -e -q x", None),
    ("cmd | grep -eq x", None),
    ("cmd | grep -A3 x", None),
    ("cmd | grep --regexp -q x", None),
    ("cmd | sed '$q'", None),
    ("cmd | head -qn -5", None),
    ("cmd | awk '{ n++ }\n  /exit/ { print }'", None),
    ("cmd | awk '{ print }  # exit early'", None),
    ("cmd | grep -- -q", None),
    ("cmd | rg -L x", None),
    ("cmd | head -n -1", None),
    ("cmd | head -n-1", None),
    ("cmd | head --lines=-2", None),
    ("cmd | head -c -3", None),
    ("cmd | sed 's/q/Q/g'", None),
    ("cmd | sed -n 1p", None),
    ("cmd | sed 'y/q/Q/'", None),
    ("cmd | sed '/quit/d'", None),
    ("cmd | sed 'a quit'", None),
    ("cmd | sed -e :q -e 'n;bq'", None),
    ("cmd | sed 's/x/y/w quits'", None),
    ("cmd | awk '{ n++ } END { exit n == 0 }'", None),
    ("cmd | awk '/exit/ { print }'", None),
    ("cmd | awk '{ print \"exit\" }'", None),
    ("cmd | while read -r l; do :; done", None),
    ("cmd | { read -r a; cat >/dev/null; }", None),
    ("cmd | xargs grep -q x", None),
    ("cmd | tail -1", None),
    ("cmd | sort | uniq -c | wc -l", None),
    ("diff <(cmd | head -1) file", None),
    ("head -1 file", None),
    ("grep -q x file", None),
    ('out=$(cmd); grep -q x <<<"$out"', None),
    ("echo grep -q x | cat", None),
    ("cmd | grep -q x  # pipefail-lint: ignore", None),
    ("# pipefail-lint: ignore\ncmd | grep -q x", None),
    ("cmd \\\n  | grep -q x \\\n  | sort  # pipefail-lint: ignore", None),
]


def lint(*paths, cwd=None):
    return subprocess.run(
        [str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def flagged_lines(run):
    """The line numbers pipefail-lint's findings name."""
    return [int(m["line"]) for m in map(FINDING.match, run.stdout.splitlines()) if m]


def flagged_files(run):
    """The files pipefail-lint's findings name, each once."""
    found = (m["path"] for m in map(FINDING.match, run.stdout.splitlines()) if m)
    return list(dict.fromkeys(found))


def test_it_flags_each_reader_that_can_stop_early_and_spares_the_rest(tmp_path):
    lines = PIPEFAIL.splitlines()
    case_at = {}  # line number -> the case whose snippet holds it
    expected = set()
    for snippet, flagged in CASES:
        first = len(lines) + 1
        lines += snippet.splitlines()
        case_at.update(dict.fromkeys(range(first, len(lines) + 1), snippet))
        if flagged is not None:
            expected.add((snippet, first + flagged))
    script = tmp_path / "script.sh"
    script.write_text("\n".join(lines) + "\n")

    run = lint(script)

    found = {(case_at.get(line, f"line {line}"), line) for line in flagged_lines(run)}
    assert sorted(found - expected) == [], "flagged wrongly"
    assert sorted(expected - found) == [], "missed"
    assert run.returncode == 1


def test_a_finding_names_the_file_the_line_and_the_reader_then_the_fix(tmp_path):
    script = tmp_path / "tags.sh"
    script.write_text(
        f"{PIPEFAIL}echo start\nif gh api x | grep -qx main; then :; fi\n"
    )

    run = lint(script)

    assert run.returncode == 1
    [line] = run.stdout.splitlines()
    assert line.startswith(f"{script}:4: ") and "grep -qx main" in line
    assert "out=$(cmd)" in run.stderr and '<<<"$out"' in run.stderr


@pytest.mark.parametrize(
    "after",
    [
        "a=$(cmd | head -1) && echo found",
        "a=$(cmd | head -1) && echo found  # a comment",
        "cat <<EOF | head -1\nbody\nEOF",
    ],
)
@pytest.mark.parametrize("comment", ["", "  # count them"])
def test_a_line_after_a_pipeline_of_three_stages_is_read_on_its_own(
    tmp_path, after, comment
):
    """tree-sitter-bash runs such a line into the pipeline before it."""
    script = tmp_path / "s.sh"
    script.write_text(f"{PIPEFAIL}cmd | sort | uniq -c{comment}\n{after}\n")

    assert flagged_lines(lint(script)) == [4]


@pytest.mark.parametrize(
    "setting",
    [
        "set -o pipefail",
        "set -eu -o pipefail",
        "set -o errexit -o pipefail",
        "set -Eeuo pipefail",
        "shopt -so pipefail",
    ],
)
def test_each_way_of_setting_pipefail_counts(tmp_path, setting):
    script = tmp_path / "s.sh"
    script.write_text(f"{setting}\ncmd | grep -q x\n")

    assert flagged_lines(lint(script)) == [2]


def test_only_a_script_that_sets_pipefail_is_flagged(tmp_path):
    without = tmp_path / "without.sh"
    without.write_text("#!/bin/bash\nset -eu\ncmd | grep -q x\n# set -o pipefail\n")
    (tmp_path / "with.sh").write_text(f"{PIPEFAIL}cmd | grep -q x\n")

    run = lint("without.sh", "with.sh", cwd=tmp_path)

    assert flagged_files(run) == ["with.sh"]


@pytest.mark.parametrize(
    "name, first_line",
    [
        ("tool", "#!/usr/bin/env bash"),
        ("tool", "#!/bin/sh -e"),
        ("tool", "#!/usr/bin/env -S bash -e"),
        ("tool.sh", ""),
        ("tool.bash", "# no shebang"),
    ],
)
def test_a_shell_script_is_known_by_its_name_or_its_shebang(tmp_path, name, first_line):
    script = tmp_path / name
    script.write_text(f"{first_line}\nset -o pipefail\ncmd | head -1\n")

    assert flagged_lines(lint(script)) == [3]


@pytest.mark.parametrize(
    "name, first_line",
    [
        ("tool.py", "#!/usr/bin/env python3"),
        ("notes", "set -o pipefail"),
        ("x.md", ""),
        ("tool", "#!/usr/bin/env zsh"),
    ],
)
def test_any_other_file_is_skipped(tmp_path, name, first_line):
    (tmp_path / name).write_text(f"{first_line}\nset -o pipefail\ncmd | head -1\n")
    (tmp_path / "s.sh").write_text(f"{PIPEFAIL}cmd | head -1\n")

    run = lint(name, "s.sh", cwd=tmp_path)

    assert flagged_files(run) == ["s.sh"]


def test_a_script_that_doesnt_parse_whole_is_linted_and_says_so(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(f"{PIPEFAIL}cmd | head -1\nfi )\n")

    run = lint(script)

    assert flagged_lines(run) == [3]
    assert f"{script}: part of it didn't parse" in run.stderr


def test_without_files_it_is_a_usage_error():
    run = lint()

    assert run.returncode == 2
    assert "pipefail-lint" in run.stderr


def test_a_file_it_cant_read_is_named_and_the_rest_still_checked(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(f"{PIPEFAIL}cmd | head -1\n")

    run = lint(tmp_path / "gone.sh", script)

    assert run.returncode == 2
    assert "gone.sh" in run.stderr
    assert flagged_lines(run) == [3]


def test_help_says_what_it_flags_and_the_fix():
    run = lint("-h")

    assert run.returncode == 0
    for word in ("grep -q", "head", "pipefail", '<<<"$out"', "pipefail-lint: ignore"):
        assert word in run.stdout, word
