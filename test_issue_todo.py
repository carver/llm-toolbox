"""bin/issue-todo: a GitHub issue's task-list items across its body and comments, and ticking one."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "issue-todo"
loader = importlib.machinery.SourceFileLoader("issue_todo", str(SCRIPT))
spec = importlib.util.spec_from_loader("issue_todo", loader)
tool = importlib.util.module_from_spec(spec)
loader.exec_module(tool)

BODY = """Plan:

- [ ] The forecast question
- [x] A missing control
  - [ ] a nested step
* [X] Starred and capital X

```
- [ ] inside a code block, not a task
```
"""


def test_items_are_the_task_list_lines_outside_code_blocks():
    assert [(i.line, i.done, i.text) for i in tool.items(BODY)] == [
        (2, False, "The forecast question"),
        (3, True, "A missing control"),
        (4, False, "a nested step"),
        (5, True, "Starred and capital X"),
    ]


def test_ticking_an_item_checks_its_box_and_appends_the_note_in_italics():
    (item, *_) = tool.items(BODY)
    lines = tool.tick(BODY, item, "done in #42").splitlines()
    assert lines[2] == "- [x] The forecast question *done in #42*"
    assert lines[3:] == BODY.splitlines()[3:]


def test_undo_unticks_an_item_and_keeps_its_text():
    item = tool.items(BODY)[1]
    assert (
        tool.tick(BODY, item, None, undo=True).splitlines()[3]
        == "- [ ] A missing control"
    )


class FakeGh:
    """Serves issue 7 of owner/repo, with a comment holding two items, and records every PATCH."""

    def __init__(self, body=BODY):
        self.issue = {
            "number": 7,
            "title": "Criterion: consistent",
            "state": "open",
            "body": body,
        }
        self.comments = [
            {
                "id": 11,
                "user": {"login": "alice"},
                "created_at": "2026-10-23T20:39:33Z",
                "body": "No tasks.",
            },
            {
                "id": 12,
                "user": {"login": "robo"},
                "created_at": "2026-10-24T01:29:36Z",
                "body": "## To-dos\n\n- [ ] Replace the forecast measure\n- [ ] Add hub",
            },
        ]
        self.patched = []

    def __call__(self, *args, stdin=None):
        if args[:2] == ("repo", "view"):
            return "owner/repo\n"
        if "-X" in args:
            self.patched.append((args[-3], json.loads(stdin)["body"]))
            return "{}"
        path = args[-1]
        if path.endswith("/issues/7"):
            return json.dumps(self.issue)
        if path.endswith("/issues/7/comments"):
            return json.dumps(self.comments)
        if "/issues/comments/" in path:
            (comment,) = [c for c in self.comments if path.endswith(f"/{c['id']}")]
            return json.dumps(comment)
        raise AssertionError(f"unexpected gh call {args}")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(tool, "gh", fake)
    return fake


def test_show_lists_the_bodys_items_then_each_comments_with_its_id(fake, capsys):
    assert tool.main(["show", "7"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "#7 Criterion: consistent (open)"
    assert "body" in out[1] and out[2].startswith("  [ ] 1  The forecast question")
    assert any("comment 12 robo 2026-10-24" in line for line in out)
    assert "  [ ] 2  Add hub" in out
    assert not any("alice" in line for line in out)


def test_tick_finds_the_one_open_item_matching_in_a_comment_and_writes_that_comment(
    fake, capsys
):
    assert (
        tool.main(["tick", "7", "forecast measure", "--note", "built in 8d5068e"]) == 0
    )
    assert fake.patched == [
        (
            "repos/owner/repo/issues/comments/12",
            "## To-dos\n\n- [x] Replace the forecast measure *built in 8d5068e*\n- [ ] Add hub",
        )
    ]
    assert (
        "- [x] Replace the forecast measure *built in 8d5068e*"
        in capsys.readouterr().out
    )


def test_tick_in_the_body_writes_the_issue(fake):
    assert tool.main(["tick", "7", "nested step"]) == 0
    (path, body) = fake.patched[0]
    assert path == "repos/owner/repo/issues/7" and "  - [x] a nested step" in body


def test_a_match_in_several_items_or_none_writes_nothing_and_lists_the_candidates(
    fake, capsys
):
    assert tool.main(["tick", "7", "forecast"]) == 1
    err = capsys.readouterr().err
    assert "The forecast question" in err and "Replace the forecast measure" in err
    assert tool.main(["tick", "7", "nothing like this"]) == 1
    assert fake.patched == []


def test_a_dry_run_prints_the_new_line_and_writes_nothing(fake, capsys):
    assert tool.main(["tick", "7", "Add hub", "--dry-run"]) == 0
    assert "- [x] Add hub" in capsys.readouterr().out and fake.patched == []


def test_a_crlf_body_keeps_its_line_endings_and_each_tick_lands_on_its_item():
    body = "- [ ] one\r\n- [ ] two\r\n- [ ] three\r\n"
    first = tool.tick(body, tool.items(body)[0], "note A")
    second = tool.tick(first, tool.items(first)[1], "note B")
    assert second == "- [x] one *note A*\r\n- [x] two *note B*\r\n- [ ] three\r\n"


def test_ordered_list_items_count_and_a_fence_closes_only_on_its_own_kind():
    body = (
        "1. [ ] first\n2) [x] second\n````\n```\n- [ ] in code\n```\n````\n- [ ] after"
    )
    assert [i.text for i in tool.items(body)] == ["first", "second", "after"]


def test_an_exact_match_wins_over_items_that_merely_contain_it(fake):
    fake.comments[1]["body"] = "- [ ] Add hub\n- [ ] Add hub arms"
    assert tool.main(["tick", "7", "Add hub"]) == 0
    assert fake.patched[0][1] == "- [x] Add hub\n- [ ] Add hub arms"


def test_no_open_match_says_how_many_ticked_items_match(fake, capsys):
    assert tool.main(["tick", "7", "missing control"]) == 1
    assert "1 ticked item matches; --undo clears it" in capsys.readouterr().err


def test_a_part_that_changed_since_it_was_read_is_left_alone(fake, capsys):
    original = fake.__call__

    def edited_meanwhile(*args, stdin=None):
        out = original(*args, stdin=stdin)
        if args[-1].endswith(
            "/issues/7/comments"
        ):  # the next read sees someone else's edit
            fake.comments[1]["body"] += "\n- [ ] Added by someone else"
        return out

    tool.gh = edited_meanwhile
    assert tool.main(["tick", "7", "Add hub"]) == 1
    assert fake.patched == [] and "changed since" in capsys.readouterr().err


def test_undo_through_the_command_clears_the_box(fake):
    assert tool.main(["tick", "7", "Starred and capital X", "--undo"]) == 0
    assert "* [ ] Starred and capital X" in fake.patched[0][1]


def test_show_cuts_a_long_item_to_one_line(fake, capsys):
    fake.issue["body"] = "- [ ] " + "x" * 300
    tool.main(["show", "7"])
    line = next(line for line in capsys.readouterr().out.splitlines() if "xxx" in line)
    assert line.endswith("…") and len(line) < 120
