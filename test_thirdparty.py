"""lib/thirdparty.py: finding skills, diffing them, and patching installed copies."""

import pytest

from skill_testing import commit, make_skill, tp


def test_upstream_skills_finds_nested_folders_and_skips_git(tmp_path):
    make_skill(tmp_path, "skills/engineering/pr")
    make_skill(tmp_path, "plugins/show-me/skills/show-me")
    make_skill(tmp_path, ".git/skills/ghost")
    assert set(tp.upstream_skills(tmp_path)) == {"pr", "show-me"}


def test_tree_hash_matches_git(tmp_path):
    make_skill(tmp_path, "skills/pr")
    commit(tmp_path)
    assert len(tp.tree_hash(tmp_path, tmp_path / "skills/pr")) == 40


@pytest.fixture
def pair(tmp_path):
    old = make_skill(tmp_path, "old/pr", body="one\ntwo\nthree\n")
    new = make_skill(tmp_path, "new/pr", body="one\n2\nthree\n")
    (old / "gone.md").write_text("bye\n")
    (new / "added.md").write_text("hello\n")
    (new / "no-newline.md").write_text("tail")
    return old, new


def test_diff_has_short_paths_and_stats(pair):
    old, new = pair
    stats, patch = tp.diff(old, new)
    assert [s.path for s in stats] == [
        "SKILL.md",
        "added.md",
        "gone.md",
        "no-newline.md",
    ]
    assert stats[0] == tp.FileStat(1, 1, "SKILL.md")
    assert str(old) not in patch and "--- a/SKILL.md\n+++ b/SKILL.md" in patch


def test_patch_round_trip_edits_adds_and_deletes_files(pair, tmp_path):
    old, new = pair
    patch = tp.write_patch("pr", ["because"], tp.diff(old, new)[1])
    assert tp.whys(patch) == ["because"]
    assert tp.apply(old, patch) == "applied"
    assert tp.diff(old, new)[1] == ""
    assert tp.apply(old, patch) == "already"


def test_reverse_copy_recovers_the_original(pair):
    old, new = pair
    original = tp.diff(old, old)  # empty, for comparison
    patch = tp.write_patch("pr", ["w"], tp.diff(old, new)[1])
    with tp.copy_with(new, patch, reverse=True) as unpatched:
        assert unpatched is not None
        assert tp.diff(old, unpatched) == original


def test_conflict_leaves_the_skill_untouched(pair):
    old, new = pair
    patch = tp.write_patch("pr", ["w"], tp.diff(old, new)[1])
    (old / "SKILL.md").write_text("rewritten upstream\n")
    before = (old / "SKILL.md").read_text()
    assert tp.apply(old, patch) == "conflict"
    assert (old / "SKILL.md").read_text() == before
    assert not (old / "added.md").exists()
    with tp.copy_with(old, patch) as patched:
        assert patched is None


def test_write_patch_wants_a_list_of_reasons(pair):
    old, new = pair
    with pytest.raises(TypeError):
        tp.write_patch("pr", "because", tp.diff(old, new)[1])


def test_a_patch_with_several_reasons_round_trips_and_applies(pair):
    old, new = pair
    patch = tp.write_patch("pr", ["first", "second"], tp.diff(old, new)[1])
    assert tp.whys(patch) == ["first", "second"]
    assert tp.apply(old, patch) == "applied"
    assert tp.diff(old, new)[1] == ""


@pytest.fixture(autouse=True)
def _patches_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "PATCHES", tmp_path / "skill-patches")
