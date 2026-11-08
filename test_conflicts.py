"""conflicts lists a file's conflict hunks, resolves them by side, and --continue
finishes the rebase, cherry-pick, merge, revert or am they came from."""

import os
import shutil
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


class Link(str):
    """A symlink's target, for `commit`."""


def commit(repo, message, files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None or isinstance(text, Link):
            path.unlink(missing_ok=True)
        if isinstance(text, Link):
            path.symlink_to(text)
        elif isinstance(text, bytes):
            path.write_bytes(text)
        elif text is not None:
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def new_repo(path):
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.com")
    return path


# Markers a file holds on purpose, as the tests of a conflict tool do.
FIXTURE = "<<<<<<< fixture\nkept\n=======\nkept too\n>>>>>>> fixture\n"


def diverged(repo, base, theirs, ours):
    """A repo whose main (checked out) and other branch from `base`: each a dict of
    file texts, with None for a file the commit deletes."""
    new_repo(repo)
    commit(repo, "base", base)
    git(repo, "checkout", "-q", "-b", "other")
    commit(repo, "theirs", theirs)
    git(repo, "checkout", "-q", "main")
    commit(repo, "ours", ours)
    return repo


def stop(repo, *args):
    """Run a git command that must stop at a conflict."""
    run = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    assert run.returncode != 0, run


def merge_conflict(repo, attributes=""):
    """A merge stopped at a conflict in a.txt, whose text also holds FIXTURE."""
    diverged(
        repo,
        {".gitattributes": attributes, "a.txt": f"{FIXTURE}line\n"},
        {"a.txt": f"{FIXTURE}theirs\n"},
        {"a.txt": f"{FIXTURE}ours\n"},
    )
    stop(repo, "merge", "-q", "other")
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


@pytest.fixture(scope="module")
def stopped_template(tmp_path_factory):
    """A rebase of two commits onto `up`, stopped at the first, where a.txt and b.txt
    conflict; the second conflicts in c.txt. Made once: each test copies it."""
    repo = new_repo(tmp_path_factory.mktemp("stopped"))
    commit(repo, "base", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    git(repo, "checkout", "-q", "-b", "up")
    commit(repo, "up", {"a.txt": "a up\n", "b.txt": "b up\n", "c.txt": "c up\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, "first", {"a.txt": "a mine\n", "b.txt": "b mine\n"})
    commit(repo, "second", {"c.txt": "c mine\n"})
    stop(repo, "rebase", "up")
    return repo


@pytest.fixture
def stopped(stopped_template, tmp_path):
    shutil.copytree(stopped_template, tmp_path, symlinks=True, dirs_exist_ok=True)
    return tmp_path


def unmerged(repo):
    return git(repo, "diff", "--name-only", "--diff-filter=U").split()


def subjects(repo):
    return git(repo, "log", "--format=%s").split("\n")[:-1]


def test_continue_refuses_while_a_file_holds_a_hunk(stopped):
    conflicts(stopped, "a.txt", "all", "theirs")

    done = conflicts(stopped, "--continue")

    assert done.returncode == 1, done
    assert "b.txt: 1 hunk left" in done.stderr and "a.txt" not in done.stderr
    assert unmerged(stopped) == ["a.txt", "b.txt"]  # nothing staged
    assert subjects(stopped)[0] == "up"


def test_continue_commits_and_lists_the_next_stops_conflicts(stopped):
    conflicts(stopped, "a.txt", "all", "theirs", "b.txt", "all", "ours")

    done = conflicts(stopped, "--continue")

    assert done.returncode == 1, done
    assert git(stopped, "show", "HEAD:a.txt") == "a mine\n"
    assert git(stopped, "show", "HEAD:b.txt") == "b up\n"
    assert subjects(stopped)[0] == "first" and unmerged(stopped) == ["c.txt"]
    assert "c.txt, hunk 1" in done.stdout and "c mine" in done.stdout
    assert "stopped" in done.stderr and "conflicts --continue" in done.stderr
    assert "hint:" not in done.stdout + done.stderr  # git's own advice to git add


def test_continue_runs_the_rebase_to_its_end(stopped):
    (stopped / "a.txt").write_text("Title\n=======\n")  # an underline, not a marker
    conflicts(stopped, "b.txt", "all", "both")
    conflicts(stopped, "--continue")
    conflicts(stopped, "c.txt", "all", "theirs")

    done = conflicts(stopped, "--continue")

    assert done.returncode == 0, done
    assert subjects(stopped) == ["second", "first", "up", "base"]
    assert git(stopped, "show", "HEAD:a.txt") == "Title\n=======\n"
    assert git(stopped, "status", "--porcelain") == ""


@pytest.mark.parametrize("then", ["keep", "delete", "link"])
def test_continue_refuses_a_file_staged_with_a_hunk_left(stopped, then):
    conflicts(stopped, "a.txt", "all", "ours")
    git(stopped, "add", "a.txt", "b.txt")
    if then != "keep":  # what's staged is what would be committed
        (stopped / "b.txt").unlink()
    if then == "link":
        (stopped / "b.txt").symlink_to("a.txt")

    done = conflicts(stopped, "--continue")

    assert done.returncode == 1, done
    assert "b.txt: 1 hunk left" in done.stderr
    assert subjects(stopped)[0] == "up"


def test_continue_stops_when_staging_fails(stopped):
    conflicts(stopped, "a.txt", "all", "ours", "b.txt", "all", "ours")
    (stopped / ".git" / "index.lock").write_text("")

    done = conflicts(stopped, "--continue")

    assert done.returncode == 128, done
    assert "index.lock" in done.stderr and "stopped" not in done.stderr
    assert subjects(stopped)[0] == "up"


def test_continue_with_nothing_to_continue_exits_3(tmp_path):
    outside = conflicts(tmp_path, "--continue")
    (tmp_path / "repo").mkdir()
    repo = new_repo(tmp_path / "repo")
    commit(repo, "base", {"a.txt": "a\n"})
    idle = conflicts(repo, "--continue")

    assert outside.returncode == 3 and idle.returncode == 3
    assert "in progress" in idle.stderr


BINARY_MERGE = {".gitattributes": "f.txt merge=binary\n", "f.txt": "f\n"}


@pytest.mark.parametrize(
    "base, theirs, ours, why",
    [
        ({}, {"f.txt": None}, {"f.txt": "f mine\n"}, "deleted by them"),
        ({}, {"f.txt": "f theirs\n"}, {"f.txt": None}, "deleted by us"),
        ({}, {"f.txt": b"\0theirs"}, {"f.txt": b"\0mine"}, "binary"),
        ({}, {"f.txt": b"\0theirs"}, {"f.txt": "text mine\n"}, "binary"),
        ({"f.txt": Link("a")}, {"f.txt": Link("b")}, {"f.txt": Link("c")}, "a symlink"),
        (BINARY_MERGE, {"f.txt": "f theirs\n"}, {"f.txt": "f mine\n"}, "merged by"),
    ],
)
def test_continue_leaves_a_conflict_without_markers_to_you(
    tmp_path, base, theirs, ours, why
):
    repo = diverged(tmp_path, {"f.txt": "f\n", **base}, theirs, ours)
    stop(repo, "merge", "other")

    refused = conflicts(repo, "--continue")
    git(repo, "add", "-A")
    done = conflicts(repo, "--continue")

    assert refused.returncode == 1, refused
    assert f"f.txt: {why}" in refused.stderr and "git rm f.txt" in refused.stderr
    assert done.returncode == 0, done
    assert len(git(repo, "log", "-1", "--format=%P").split()) == 2


def test_continue_stops_at_the_fixture_only_while_a_real_hunk_is_left(tmp_path):
    merge_conflict(tmp_path, "")

    refused = conflicts(tmp_path, "--continue")
    conflicts(tmp_path, "a.txt", "1", "ours")
    done = conflicts(tmp_path, "--continue")

    assert refused.returncode == 1 and "a.txt: 1 hunk left" in refused.stderr
    assert done.returncode == 0, done
    assert git(tmp_path, "show", "HEAD:a.txt") == f"{FIXTURE}ours\n"


def test_continue_counts_hunks_in_a_file_kept_in_another_encoding(tmp_path):
    def utf16(text):
        return text.encode("utf-16")

    repo = diverged(
        tmp_path,
        {
            ".gitattributes": "f.txt working-tree-encoding=UTF-16\n",
            "f.txt": utf16("f\n"),
        },
        {"f.txt": utf16("o\n")},
        {"f.txt": utf16("m\n")},
    )
    stop(repo, "merge", "other")

    done = conflicts(repo, "--continue")

    assert done.returncode == 1 and "f.txt: 1 hunk left" in done.stderr, done


def test_continue_counts_hunks_in_a_crlf_file(tmp_path):
    repo = diverged(
        tmp_path, {"f.txt": "f\r\n"}, {"f.txt": "o\r\n"}, {"f.txt": "m\r\n"}
    )
    stop(repo, "merge", "other")

    done = conflicts(repo, "--continue")

    assert done.returncode == 1 and "f.txt: 1 hunk left" in done.stderr


def sequence_repo(repo):
    """main and other each change f.txt from the base, and other then adds g.txt."""
    diverged(repo, {"f.txt": "f\n"}, {"f.txt": "f theirs\n"}, {"f.txt": "f ours\n"})
    git(repo, "checkout", "-q", "other")
    commit(repo, "then", {"g.txt": "g\n"})
    git(repo, "checkout", "-q", "main")
    return repo


def start_merge(repo):
    stop(repo, "merge", "other")


def start_cherry_pick(repo):
    stop(repo, "cherry-pick", "main..other")


def start_hand_committed_pick(repo):
    """A range cherry-pick whose conflicted commit was committed by hand, so only
    the sequencer remembers the pick."""
    stop(repo, "cherry-pick", "main..other")
    conflicts(repo, "f.txt", "all", "theirs")
    git(repo, "commit", "-qam", "theirs, by hand")


def start_revert(repo):
    commit(repo, "after", {"f.txt": "f after\n"})
    stop(repo, "revert", "--no-edit", "HEAD~1")


def start_am(repo):
    patches = repo / ".git" / "patches"
    git(repo, "format-patch", "-q", "-o", str(patches), "main..other")
    stop(repo, "am", "-3", *sorted(map(str, patches.iterdir())))


@pytest.mark.parametrize(
    "start, last",
    [
        (start_merge, "Merge branch 'other'"),
        (start_cherry_pick, "then"),
        (start_hand_committed_pick, "then"),
        (start_revert, 'Revert "ours"'),
        (start_am, "then"),
    ],
)
def test_continue_finishes_each_kind_of_operation(tmp_path, start, last):
    repo = sequence_repo(tmp_path)
    start(repo)
    if unmerged(repo):
        conflicts(repo, "f.txt", "all", "theirs")

    done = conflicts(repo, "--continue")

    assert done.returncode == 0, done
    assert subjects(repo)[0] == last
    assert git(repo, "status", "--porcelain") == ""


def test_continue_names_paths_as_conflicts_takes_them(tmp_path):
    repo = diverged(
        tmp_path,
        {"sub/a.txt": "a\n", "all": "x\n"},
        {"sub/a.txt": "a theirs\n", "all": "x theirs\n"},
        {"sub/a.txt": "a ours\n", "all": "x ours\n"},
    )
    stop(repo, "merge", "other")

    top = conflicts(repo, "--continue")
    sub = conflicts(repo / "sub", "--continue")

    assert "./all: 1 hunk left" in top.stderr and "sub/a.txt: 1" in top.stderr
    assert "../all: 1 hunk left" in sub.stderr and "\n  a.txt: 1" in sub.stderr


def test_continue_stages_a_name_with_glob_characters_as_itself(tmp_path):
    repo = diverged(
        tmp_path,
        {"a*.txt": "a\n", "ab.txt": "b\n"},
        {"a*.txt": "a theirs\n"},
        {"a*.txt": "a ours\n"},
    )
    stop(repo, "merge", "other")
    (repo / "ab.txt").write_text("b, not for this commit\n")
    conflicts(repo, "a*.txt", "all", "ours")

    done = conflicts(repo, "--continue")

    assert done.returncode == 0, done
    assert git(repo, "status", "--porcelain") == " M ab.txt\n"


def test_continue_passes_on_a_failure_that_isnt_a_stop(tmp_path):
    repo = diverged(tmp_path, {"f.txt": "f\n"}, {"f.txt": "o\n"}, {"f.txt": "m\n"})
    stop(repo, "merge", "other")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho no thanks >&2\nexit 1\n")
    hook.chmod(0o755)
    conflicts(repo, "f.txt", "all", "ours")

    done = conflicts(repo, "--continue")

    assert done.returncode == 1 and "no thanks" in done.stderr
    assert "stopped" not in done.stderr
