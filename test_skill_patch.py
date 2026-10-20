"""bin/skill-patch: saving hand edits as patches and putting them back."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from skill_testing import commit, isolate, lock_for, make_skill, tp

SCRIPT = Path(__file__).resolve().parent / "bin" / "skill-patch"
loader = importlib.machinery.SourceFileLoader("skill_patch", str(SCRIPT))
spec = importlib.util.spec_from_loader("skill_patch", loader)
sp = importlib.util.module_from_spec(spec)
loader.exec_module(sp)


@pytest.fixture
def world(tmp_path, monkeypatch):
    installed, clone = isolate(monkeypatch, tmp_path)
    make_skill(installed, "pr")
    up = make_skill(clone, "skills/pr")
    commit(clone)
    tp.LOCK.write_text(json.dumps({"skills": {"pr": lock_for(clone, "skills/pr")}}))
    monkeypatch.setattr(tp, "third_party_skills", lambda: {"a/s": ["pr"]})
    monkeypatch.setattr(tp, "fetch", lambda source, cache: clone)
    return installed / "pr", up


def test_save_needs_a_why_for_a_new_patch(world, capsys):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    assert sp.main(["save", "pr"]) == 1
    assert "--why" in capsys.readouterr().err
    assert sp.main(["save", "pr", "--why", "because"]) == 0
    patch = tp.PATCHES / "pr.patch"
    assert tp.whys(patch) == ["because"] and "+mine" in patch.read_text()


def test_save_again_keeps_the_why_and_takes_the_new_edit(world):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "because"])
    (skill / "SKILL.md").write_text("mine, revised\n")
    assert sp.main(["save", "pr"]) == 0
    text = (tp.PATCHES / "pr.patch").read_text()
    assert text.startswith("# Why: because\n") and "+mine, revised" in text


def test_save_removes_the_patch_once_the_skill_matches_upstream(world, capsys):
    skill, up = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "because"])
    (skill / "SKILL.md").write_text((up / "SKILL.md").read_text())
    assert sp.main(["save", "pr"]) == 0
    assert not (tp.PATCHES / "pr.patch").exists()


def test_save_refuses_when_upstream_moved_since_install(world, capsys):
    _, up = world
    (up / "SKILL.md").write_text("upstream moved\n")
    commit(up.parent.parent)
    assert sp.main(["save", "pr", "--why", "w"]) == 1
    assert "--update" in capsys.readouterr().err


def test_save_refuses_a_skill_not_in_the_list(world, capsys):
    make_skill(tp.INSTALLED, "nope")
    assert sp.main(["save", "nope", "--why", "w"]) == 1
    assert "THIRD_PARTY_SKILLS" in capsys.readouterr().err


def test_apply_restores_a_patch_after_an_update_and_reports_conflicts(world, capsys):
    skill, _ = world
    original = (skill / "SKILL.md").read_text()
    (skill / "SKILL.md").write_text(original + "mine\n")
    sp.main(["save", "pr", "--why", "w"])
    (skill / "SKILL.md").write_text(original)  # what an update leaves behind
    assert sp.main(["apply"]) == 0
    assert (skill / "SKILL.md").read_text() == original + "mine\n"
    assert sp.main(["apply"]) == 0  # already on

    (skill / "SKILL.md").write_text("rewritten upstream\n")
    assert sp.main(["apply", "pr"]) == 1
    assert "CONFLICT pr" in capsys.readouterr().out


def test_a_new_why_on_a_patched_skill_layers_on_the_earlier_reasons(world, capsys):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    (skill / "SKILL.md").write_text("mine\nand more\n")
    capsys.readouterr()
    assert sp.main(["save", "pr", "--why", "second"]) == 0
    patch = tp.PATCHES / "pr.patch"
    assert tp.whys(patch) == ["first", "second"] and "+and more" in patch.read_text()
    out = capsys.readouterr().out
    assert "first" in out and "second" in out


def test_saving_without_a_why_keeps_every_reason(world):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    sp.main(["save", "pr", "--why", "second"])
    (skill / "SKILL.md").write_text("mine, revised\n")
    assert sp.main(["save", "pr"]) == 0
    assert tp.whys(tp.PATCHES / "pr.patch") == ["first", "second"]


def test_the_same_reason_again_isnt_repeated(world):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    sp.main(["save", "pr", "--why", "first"])
    assert tp.whys(tp.PATCHES / "pr.patch") == ["first"]


def test_replace_why_drops_the_earlier_reasons(world):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    sp.main(["save", "pr", "--why", "second"])
    assert sp.main(["save", "pr", "--why", "only this", "--replace-why"]) == 0
    assert tp.whys(tp.PATCHES / "pr.patch") == ["only this"]


def test_replace_why_needs_a_why(world, capsys):
    with pytest.raises(SystemExit) as exit_:
        sp.main(["save", "pr", "--replace-why"])
    assert exit_.value.code == 2
    assert "--replace-why needs --why" in capsys.readouterr().err


def test_save_prints_the_reasons_it_keeps_and_drops(world, capsys):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    assert "first" in capsys.readouterr().out
    sp.main(["save", "pr", "--why", "second", "--replace-why"])
    out = capsys.readouterr().out
    assert "second" in out and "dropped" in out and "first" in out


def test_a_reason_is_one_line_however_it_is_typed(world):
    skill, _ = world
    (skill / "SKILL.md").write_text("mine\n")
    sp.main(["save", "pr", "--why", "first"])
    sp.main(["save", "pr", "--why", "  first "])
    sp.main(["save", "pr", "--why", "two\nlines"])
    assert tp.whys(tp.PATCHES / "pr.patch") == ["first", "two lines"]
