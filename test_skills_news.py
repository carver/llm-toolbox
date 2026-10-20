"""bin/skills-news against temp skill folders, with Claude faked."""

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from skill_testing import commit, isolate, lock_for, make_skill, tp

SCRIPT = Path(__file__).resolve().parent / "bin" / "skills-news"
loader = importlib.machinery.SourceFileLoader("skills_news", str(SCRIPT))
spec = importlib.util.spec_from_loader("skills_news", loader)
news = importlib.util.module_from_spec(spec)
loader.exec_module(news)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return isolate(monkeypatch, tmp_path)


def test_unchanged_skill_is_no_news(world):
    installed, clone = world
    make_skill(installed, "pr")
    make_skill(clone, "skills/pr")
    changes, new, _ = news.find_news(
        {"a/s": ["pr"]}, {"a/s": clone}, {"a/s": ["pr"]}, {}
    )
    assert changes == [] and new == []


def test_changed_skill_reports_stat_and_patch_with_short_paths(world):
    installed, clone = world
    make_skill(installed, "pr")
    make_skill(clone, "skills/pr", body="do the new thing\n")
    (clone / "skills/pr/extra.md").write_text("more\n")
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, {})
    assert change.name == "pr"
    assert "+do the new thing" in change.patch
    assert str(installed) not in change.patch and str(clone) not in change.patch
    assert change.stats == [
        tp.FileStat(1, 1, "SKILL.md"),
        tp.FileStat(1, 0, "extra.md"),
    ]
    assert "--- a/SKILL.md\n+++ b/SKILL.md" in change.patch


def test_skill_gone_from_source_is_reported(world):
    installed, clone = world
    make_skill(installed, "pr")
    clone.mkdir()
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, {})
    assert "no longer" in change.patch


def test_first_run_records_skills_without_calling_them_new(world):
    _, clone = world
    make_skill(clone, "skills/teach")
    _, new, seen = news.find_news({"a/s": []}, {"a/s": clone}, {}, {})
    assert new == [] and seen == {"a/s": ["teach"]}


def test_skill_added_since_last_run_is_new_with_its_description(world):
    _, clone = world
    make_skill(clone, "skills/teach")
    make_skill(clone, "skills/pr", desc='"Use when writing a PR body."')
    _, [added], seen = news.find_news(
        {"a/s": []}, {"a/s": clone}, {"a/s": ["teach"]}, {}
    )
    assert (added.name, added.description) == ("pr", "Use when writing a PR body.")
    assert seen == {"a/s": ["pr", "teach"]}


def test_skill_already_listed_to_install_is_not_new(world):
    _, clone = world
    make_skill(clone, "skills/pr")
    _, new, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {"a/s": []}, {})
    assert new == []


def fake_claude(reply: dict, seen_prompts: list):
    def run(cmd, input, **kw):
        seen_prompts.append(input)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(reply), stderr="")

    return run


def test_summarize_keeps_only_names_it_asked_about():
    change = news.Change("pr", "a/s", [tp.FileStat(1, 0, "SKILL.md")], "+x")
    reply = {
        "skills": [
            {"name": "pr", "summary": "Adds x."},
            {"name": "ghost", "summary": "?"},
        ]
    }
    prompts = []
    assert news.summarize([change], run=fake_claude(reply, prompts)) == {
        "pr": "Adds x."
    }
    assert '<skill name="pr">' in prompts[0]


def test_summarize_cuts_a_huge_patch():
    change = news.Change("pr", "a/s", [], "+" * (news.MAX_PATCH_CHARS + 10))
    prompts = []
    news.summarize([change], run=fake_claude({"skills": []}, prompts))
    assert "(diff cut here)" in prompts[0]


def test_summarize_raises_when_claude_fails():
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="boom"):
        news.summarize([news.Change("pr", "a/s", [], "+x")], run=failing)


def test_render_puts_summary_beside_stats_and_points_at_update():
    change = news.Change("pr", "a/s", [tp.FileStat(3, 1, "SKILL.md")], "patch")
    added = news.NewSkill("teach", "a/s", "Teaches.")
    body = news.render([change], [added], {"pr": "Adds evidence."})
    assert (
        "## pr (a/s)\nAdds evidence.\nFiles (+added -removed):\n  +3 -1  SKILL.md"
        in body
    )
    assert "install.py --update" in body
    assert "- teach (a/s): Teaches." in body


def test_render_new_skills_only_has_no_update_hint():
    body = news.render([], [news.NewSkill("teach", "a/s", "Teaches.")], {})
    assert "--update" not in body


def test_difference_after_upstream_moved_is_upstream(world):
    installed, clone = world
    make_skill(installed, "pr")
    make_skill(clone, "skills/pr")
    commit(clone)
    lock = {"pr": lock_for(clone, "skills/pr")}
    (clone / "skills/pr/SKILL.md").write_text("new upstream\n")
    commit(clone)
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert change.kind == "upstream"


def test_difference_while_upstream_stood_still_is_a_hand_edit(world):
    installed, clone = world
    make_skill(installed, "pr", body="my tweak\n")
    make_skill(clone, "skills/pr")
    commit(clone)
    lock = {"pr": lock_for(clone, "skills/pr")}
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert change.kind == "local"


def test_difference_without_a_lock_entry_is_untracked(world):
    installed, clone = world
    make_skill(installed, "pr", body="my tweak\n")
    make_skill(clone, "skills/pr")
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, {})
    assert change.kind == "untracked"


def test_render_groups_hand_edits_apart_from_upstream():
    up = news.Change("pr", "a/s", [tp.FileStat(1, 0, "SKILL.md")], "p", "upstream")
    mine = news.Change(
        "implement", "a/s", [tp.FileStat(1, 1, "SKILL.md")], "p", "local"
    )
    body = news.render([up, mine], [], {})
    assert body.index("Changed upstream") < body.index("## pr") < body.index("--update")
    assert body.index("Edited by hand outside a patch") < body.index("## implement")


@pytest.fixture
def run_main(world, tmp_path, monkeypatch):
    installed, clone = world
    monkeypatch.setattr(tp, "third_party_skills", lambda: {"a/s": ["pr"]})
    monkeypatch.setattr(tp, "fetch", lambda source, cache: clone)
    sent = []
    monkeypatch.setattr(news, "send", lambda to, subject, body: sent.append(subject))
    monkeypatch.setattr(news, "summarize", lambda changes: {})
    return installed, clone, sent


def test_email_skips_hand_edits_alone(run_main, capsys):
    installed, clone, sent = run_main
    make_skill(installed, "pr", body="my tweak\n")
    make_skill(clone, "skills/pr")
    commit(clone)
    tp.LOCK.write_text(json.dumps({"skills": {"pr": lock_for(clone, "skills/pr")}}))
    assert news.main(["--email", "me@example.com"]) == 0
    assert sent == [] and "no news" in capsys.readouterr().out


def test_email_sends_upstream_change(run_main):
    installed, clone, sent = run_main
    make_skill(installed, "pr")
    make_skill(clone, "skills/pr")
    commit(clone)
    tp.LOCK.write_text(json.dumps({"skills": {"pr": lock_for(clone, "skills/pr")}}))
    (clone / "skills/pr/SKILL.md").write_text("new upstream\n")
    commit(clone)
    assert news.main(["--email", "me@example.com"]) == 0
    assert sent == ["Skill updates: 1 changed, 0 new"]


def save_patch(name: str, upstream: Path, installed: Path, why: str = "mine") -> None:
    tp.write_patch(name, why, tp.diff(upstream, installed)[1])


def test_patched_skill_in_sync_is_no_news(world):
    installed, clone = world
    make_skill(installed, "pr", body="my tweak\n")
    up = make_skill(clone, "skills/pr")
    commit(clone)
    save_patch("pr", up, installed / "pr")
    lock = {"pr": lock_for(clone, "skills/pr")}
    changes, _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert changes == []


def test_upstream_change_says_whether_the_patch_still_fits(world):
    installed, clone = world
    make_skill(installed, "pr", body="line one\nmy tweak\n")
    up = make_skill(clone, "skills/pr", body="line one\nline two\n")
    commit(clone)
    save_patch("pr", up, installed / "pr")
    lock = {"pr": lock_for(clone, "skills/pr")}
    (up / "extra.md").write_text("new upstream file\n")
    commit(clone)
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert change.kind == "upstream" and "pr.patch applies" in change.note
    assert change.stats == [tp.FileStat(1, 0, "extra.md")]  # the patch isn't news

    (up / "SKILL.md").write_text(
        (up / "SKILL.md").read_text().replace("line two", "line 2")
    )
    commit(clone)
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert "needs a rebase" in change.note


def test_edit_outside_the_patch_is_reported(world):
    installed, clone = world
    make_skill(installed, "pr", body="my tweak\n")
    up = make_skill(clone, "skills/pr")
    commit(clone)
    save_patch("pr", up, installed / "pr")
    (installed / "pr" / "notes.md").write_text("unsaved\n")
    lock = {"pr": lock_for(clone, "skills/pr")}
    [change], _, _ = news.find_news({"a/s": ["pr"]}, {"a/s": clone}, {}, lock)
    assert change.kind == "local" and change.stats == [tp.FileStat(0, 1, "notes.md")]


@pytest.fixture
def before_update(run_main, tmp_path, monkeypatch):
    """The installed skills, with a copy of each as it was before an update."""
    installed, _, _ = run_main
    monkeypatch.setattr(tp, "third_party_skills", lambda: {"a/s": ["pr", "teach"]})
    old = tmp_path / "before"
    make_skill(old, "pr", body="old steps\n")
    make_skill(old, "teach")
    make_skill(installed, "pr", body="new steps\n")
    make_skill(installed, "teach")
    return old


def test_since_reports_each_skill_the_update_changed(
    before_update, monkeypatch, capsys
):
    asked = []
    monkeypatch.setattr(
        news,
        "summarize",
        lambda changes, **kw: (
            asked.extend(c.name for c in changes) or {"pr": "New steps."}
        ),
    )

    assert news.main(["--since", str(before_update)]) == 0

    out = capsys.readouterr().out
    assert asked == ["pr"]
    assert "## pr (a/s)\nNew steps.\nFiles (+added -removed):" in out
    assert "teach" not in out
    assert "install.py --update" not in out


def test_since_with_nothing_changed_says_so(before_update, capsys):
    (before_update / "pr" / "SKILL.md").write_text(
        (tp.INSTALLED / "pr" / "SKILL.md").read_text()
    )

    assert news.main(["--since", str(before_update), "--no-summary"]) == 0

    assert capsys.readouterr().out == "no skill changed\n"


def test_since_still_lists_the_changes_when_claude_fails(
    before_update, monkeypatch, capsys
):
    def failing(changes, **kw):
        raise RuntimeError("claude-json failed: boom")

    monkeypatch.setattr(news, "summarize", failing)

    assert news.main(["--since", str(before_update)]) == 1

    captured = capsys.readouterr()
    assert "## pr (a/s)" in captured.out and "boom" in captured.err


def test_since_a_missing_dir_is_an_error(run_main, tmp_path, capsys):
    assert news.main(["--since", str(tmp_path / "nowhere"), "--no-summary"]) == 1
    assert "nowhere" in capsys.readouterr().err


def test_since_and_email_together_are_refused():
    with pytest.raises(SystemExit) as exited:
        news.main(["--since", "x", "--email", "me@example.com"])
    assert exited.value.code == 2


def test_summarize_takes_how_the_diffs_were_made():
    prompts = []
    news.summarize(
        [news.Change("pr", "a/s", [], "+x")],
        run=fake_claude({"skills": []}, prompts),
        diffs="from a skill's copy before an update to its copy after",
    )
    assert (
        "a diff from a skill's copy before an update to its copy after." in prompts[0]
    )
    assert "latest upstream" not in prompts[0]
