"""lib/foldable.py: which commits of a batch only refine code an earlier one added.
build-state's and wt-ship's tests cover what they do with the answer."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
import foldable

MAIN_CODE = "".join(f"main line {n}\n" for n in range(1, 11))


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A repo on main with app.py holding ten lines, made once: each test copies it."""
    repo = tmp_path_factory.mktemp("template")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    write_commit(repo, "start", {"app.py": MAIN_CODE})
    return repo


@pytest.fixture
def repo(template, tmp_path, monkeypatch):
    shutil.copytree(template, tmp_path, symlinks=True, dirs_exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True, text=True
    ).stdout


def write_commit(repo, message, files):
    """Commit `files` (path: text, or bytes) as they are given; return the commit."""
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def edit(repo, path, old, new):
    text = (repo / path).read_text()
    assert old in text
    return text.replace(old, new, 1)


def feature_lines(name, count=4):
    return "".join(f"{name} part {n}\n" for n in range(1, count + 1))


def flagged(upstream="main"):
    return {fold.sha: fold for fold in foldable.foldable(upstream)}


def branch(repo):
    git(repo, "checkout", "-q", "-b", "work")


def test_a_commit_that_only_changes_an_earlier_commits_lines_is_foldable(repo):
    branch(repo)
    feature = write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    refine = write_commit(
        repo,
        "Tidy the feature",
        {"app.py": edit(repo, "app.py", "feature part 2\n", "feature part two\n")},
    )

    found = flagged()

    assert list(found) == [refine]
    assert found[refine].target == feature
    assert found[refine].share == 1.0
    assert found[refine].subject == "Tidy the feature"
    assert found[refine].relies_on == ()


def test_the_first_commit_and_a_commit_changing_mains_code_are_not(repo):
    branch(repo)
    write_commit(
        repo,
        "Change main's code",
        {"app.py": MAIN_CODE.replace("main line 3\n", "main line three\n")},
    )
    write_commit(
        repo,
        "Change more of main's code",
        {"app.py": edit(repo, "app.py", "main line 7\n", "main line seven\n")},
    )

    assert flagged() == {}


def test_a_chain_of_refinements_folds_into_the_commit_that_stays(repo):
    branch(repo)
    feature = write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    first = write_commit(
        repo,
        "Fix the feature",
        {"app.py": edit(repo, "app.py", "feature part 2\n", "feature part two\n")},
    )
    second = write_commit(
        repo,
        "Fix the fix",
        {"app.py": edit(repo, "app.py", "feature part two\n", "feature part 2b\n")},
    )

    found = flagged()

    assert set(found) == {first, second}
    assert found[second].target == feature


@pytest.mark.parametrize("feature_changed, foldable_expected", [(3, True), (2, False)])
def test_the_share_counts_non_blank_lines_and_flags_from_three_quarters(
    repo, feature_changed, foldable_expected
):
    branch(repo)
    write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + "\n" + feature_lines("f", 3)}
    )
    # One of main's lines and 3 (75%) or 2 (67%) of the feature's. The blank line
    # between them, which the feature added and this replaces, isn't counted.
    text = (repo / "app.py").read_text().replace("main line 10\n\n", "ten\nfiller\n")
    for n in range(1, feature_changed + 1):
        text = text.replace(f"f part {n}\n", f"f part {n}!\n")
    change = write_commit(repo, "Change both", {"app.py": text})

    found = flagged()

    assert (change in found) is foldable_expected
    if foldable_expected:
        assert found[change].share == 0.75


@pytest.mark.parametrize(
    "where, foldable_expected",
    [
        ("between the feature's lines", True),
        ("between main's lines", False),
        ("after the file's last line", True),
    ],
)
def test_an_insertion_is_judged_by_the_lines_either_side(
    repo, where, foldable_expected
):
    branch(repo)
    write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    text = (repo / "app.py").read_text()
    if where == "between the feature's lines":
        text = text.replace("feature part 2\n", "feature part 2\nnew line\n")
    elif where == "between main's lines":
        text = text.replace("main line 3\n", "main line 3\nnew line\n")
    else:
        text += "new line\n"
    insert = write_commit(repo, "Insert a line", {"app.py": text})

    assert (insert in flagged()) is foldable_expected


def test_an_insertion_before_the_first_line_of_a_batch_file_is_judged(repo):
    branch(repo)
    write_commit(repo, "Add a module", {"mod.py": feature_lines("mod")})
    insert = write_commit(
        repo,
        "Insert at the top",
        {"mod.py": "header\n" + (repo / "mod.py").read_text()},
    )

    assert insert in flagged()


def test_a_commit_that_only_adds_files_is_not_foldable(repo):
    branch(repo)
    write_commit(repo, "Add a module", {"mod.py": feature_lines("mod")})
    write_commit(repo, "Add another", {"other.py": feature_lines("other")})

    assert flagged() == {}


def test_a_commit_with_the_keep_trailer_is_not_foldable(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    (repo / "app.py").write_text(edit(repo, "app.py", "f part 2\n", "f part two\n"))
    git(repo, "commit", "-qam", "A feature of its own\n\nFold-check: keep")

    assert flagged() == {}


def test_a_renamed_file_is_followed(repo):
    branch(repo)
    feature = write_commit(repo, "Add a module", {"mod.py": feature_lines("mod", 8)})
    git(repo, "mv", "mod.py", "renamed.py")
    text = (repo / "renamed.py").read_text().replace("mod part 3\n", "mod part 3!\n")
    rename = write_commit(repo, "Rename and fix it", {"renamed.py": text})

    found = flagged()

    assert found[rename].target == feature


def test_crlf_and_a_lone_carriage_return_are_read_as_one_line_each(repo):
    branch(repo)
    lines = [f"crlf part {n}\r\n".encode() for n in range(1, 5)]
    lines[1] = b"has a lone \r inside\r\n"
    write_commit(repo, "Add a CRLF file", {"win.txt": b"".join(lines)})
    changed = b"".join(lines).replace(b"crlf part 3", b"crlf part three")
    fix = write_commit(repo, "Fix it", {"win.txt": changed})

    found = flagged()

    assert found[fix].share == 1.0


def test_a_commit_using_a_name_a_commit_in_between_added_says_so(repo):
    branch(repo)
    feature = write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    write_commit(repo, "Add a helper", {"helper.py": "def shiny_helper():\n    pass\n"})
    uses = write_commit(
        repo,
        "Use the helper in the feature",
        {"app.py": edit(repo, "app.py", "feature part 2\n", "shiny_helper()\n")},
    )

    found = flagged()

    assert found[uses].target == feature
    assert found[uses].relies_on == ("shiny_helper",)


@pytest.mark.parametrize(
    "between, relied_on",
    [
        ("SHINY_LIMIT = 3\n", ("SHINY_LIMIT",)),
        ("shiny_cleanup() {\n  :\n}\n", ("shiny_cleanup",)),
        ("const shinyValue = 1;\n", ("shinyValue",)),
        # Prose, or a string, that names the word defines nothing.
        ("pytest reports Interrupted for this\n", ()),
    ],
)
def test_only_a_name_a_commit_in_between_defines_is_relied_on(repo, between, relied_on):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    write_commit(repo, "Add something", {"other.txt": between})
    word = relied_on[0] if relied_on else "Interrupted"
    uses = write_commit(
        repo,
        "Use it in the feature",
        {"app.py": edit(repo, "app.py", "f part 2\n", f"call {word} here\n")},
    )

    assert flagged()[uses].relies_on == relied_on


def test_a_name_an_earlier_fold_into_the_same_target_adds_is_no_reliance(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    write_commit(
        repo,
        "Fix the feature with a helper",
        {"app.py": edit(repo, "app.py", "f part 2\n", "def shiny_helper(): pass\n")},
    )
    uses = write_commit(
        repo,
        "Call the helper",
        {"app.py": edit(repo, "app.py", "f part 3\n", "shiny_helper()\n")},
    )

    assert flagged()[uses].relies_on == ()


def test_the_upstream_need_not_be_an_ancestor(repo):
    branch(repo)
    feature = write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    refine = write_commit(
        repo,
        "Tidy the feature",
        {"app.py": edit(repo, "app.py", "feature part 2\n", "feature part two\n")},
    )
    git(repo, "checkout", "-q", "main")
    write_commit(repo, "Main moves on", {"other.txt": "x\n"})
    git(repo, "checkout", "-q", "work")

    assert flagged()[refine].target == feature


def test_a_repos_blame_ignore_revs_file_is_not_applied(repo):
    branch(repo)
    feature = write_commit(
        repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("feature")}
    )
    (repo / ".ignore-revs").write_text(f"{feature}\n")
    git(repo, "config", "blame.ignoreRevsFile", ".ignore-revs")
    refine = write_commit(
        repo,
        "Tidy the feature",
        {"app.py": edit(repo, "app.py", "feature part 2\n", "feature part two\n")},
    )

    assert refine in flagged()


def test_a_path_that_isnt_utf8_is_handled(repo):
    branch(repo)
    name = os.fsdecode(b"caf\xe9.txt")
    write_commit(repo, "Add a file", {name: feature_lines("cafe")})
    fix = write_commit(
        repo, "Fix it", {name: edit(repo, name, "cafe part 2\n", "cafe part two\n")}
    )

    assert fix in flagged()


def test_a_chain_stops_at_a_commit_left_in_place(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    write_commit(repo, "Add a helper", {"helper.py": "def shiny_helper():\n    pass\n"})
    uses = write_commit(
        repo,
        "Use the helper",
        {"app.py": edit(repo, "app.py", "f part 2\n", "shiny_helper()\n")},
    )
    refine = write_commit(
        repo,
        "Refine that use",
        {"app.py": edit(repo, "app.py", "shiny_helper()\n", "shiny_helper()  # x\n")},
    )

    found = flagged()

    assert found[uses].relies_on == ("shiny_helper",)
    assert found[refine].target == uses
    assert found[refine].relies_on == ()


def test_a_name_from_a_commit_left_in_place_still_counts_as_relied_on(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    write_commit(repo, "Add a helper", {"helper.py": "def shiny_helper():\n    pass\n"})
    write_commit(
        repo,
        "Use the helper",
        {"app.py": edit(repo, "app.py", "f part 2\n", "shiny_helper()\n")},
    )
    again = write_commit(
        repo,
        "Use it again",
        {"app.py": edit(repo, "app.py", "f part 3\n", "shiny_helper()\n")},
    )

    assert flagged()[again].relies_on == ("shiny_helper",)


def test_a_name_an_earlier_fold_only_mentions_is_still_relied_on(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    write_commit(
        repo,
        "Mention it",
        {"app.py": edit(repo, "app.py", "f part 2\n", "# see shiny_helper\n")},
    )
    write_commit(repo, "Add a helper", {"helper.py": "def shiny_helper():\n    pass\n"})
    uses = write_commit(
        repo,
        "Use the helper",
        {"app.py": edit(repo, "app.py", "f part 3\n", "shiny_helper()\n")},
    )

    assert flagged()[uses].relies_on == ("shiny_helper",)


def test_filling_a_file_that_was_added_empty_is_not_judged(repo):
    branch(repo)
    write_commit(repo, "Add an empty file", {"empty.py": ""})
    write_commit(repo, "Fill it", {"empty.py": feature_lines("filled")})

    assert flagged() == {}


def test_a_submodule_entry_is_skipped(repo):
    branch(repo)
    write_commit(repo, "Add the feature", {"app.py": MAIN_CODE + feature_lines("f")})
    start = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{start},sub")
    git(repo, "commit", "-q", "-m", "Add a submodule")
    moved = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--cacheinfo", f"160000,{moved},sub")
    (repo / "app.py").write_text(edit(repo, "app.py", "f part 2\n", "f part two\n"))
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "Move the submodule and refine")

    found = flagged()

    assert [fold.subject for fold in found.values()] == [
        "Move the submodule and refine"
    ]
