"""bin/issue-todo: a GitHub issue's task-list items across its body and comments, and ticking one."""

import importlib.machinery
import importlib.util
import json
import re
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
    """Serves issue 7 of owner/repo, with a comment holding two items, and records every call
    and every PATCH."""

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
        self.listing = []  # the open issues and pull requests, as GitHub lists them
        # Other issues and pull requests, by number, as GraphQL serves them.
        self.others = {
            25: {
                "state": "OPEN",
                "title": "Agents offer to post",
                "body": "- [ ] A usability run\n- [x] The skill",
                "comments": {"nodes": [{"body": "- [ ] One more"}]},
            },
            46: {
                "state": "CLOSED",
                "title": "A beta stamp",
                "body": "- [x] done",
                "comments": {"nodes": []},
            },
            48: {
                "state": "OPEN",
                "title": "No items",
                "body": "",
                "comments": {"nodes": []},
            },
        }
        self.graphql_fails = False
        self.patched = []
        self.calls = []

    def graphql(self, args):
        if self.graphql_fails:
            return ""
        query = next(a for a in args if a.startswith("query="))
        data, errors = {}, []
        for alias, number in re.findall(
            r"(m\d+): issueOrPullRequest\(number: (\d+)\)", query
        ):
            data[alias] = self.others.get(int(number))
            if data[alias] is None:
                errors.append({"type": "NOT_FOUND", "path": ["repository", alias]})
        return json.dumps({"data": {"repository": data}, "errors": errors})

    def __call__(self, *args, stdin=None, check=True):
        self.calls.append(args)
        if args[:2] == ("repo", "view"):
            return "owner/repo\n"
        if args[:2] == ("api", "graphql"):
            return self.graphql(args)
        if "-X" in args:
            self.patched.append((args[-3], json.loads(stdin)["body"]))
            return "{}"
        path = args[-1]
        if path.endswith("/issues/7"):
            return json.dumps(self.issue)
        if path.endswith("/issues/7/comments"):
            return json.dumps(self.comments)
        if "/issues?" in path:
            return json.dumps(self.listing)
        if path.endswith("/comments"):
            return "[]"
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


def test_open_lists_each_open_issues_open_items_and_names_the_issues_with_none(
    fake, capsys
):
    fake.listing = [
        fake.issue | {"assignees": [{"login": "carver"}]},
        {"number": 8, "title": "Past research", "body": "- [x] done", "assignees": []},
        {
            "number": 9,
            "title": "A pull request",
            "body": "- [ ] not an issue's",
            "assignees": [],
            "pull_request": {},
        },
    ]
    assert tool.main(["open"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:1] == ["#7 Criterion: consistent [carver]"]
    assert "    [ ] The forecast question" in out and "    [ ] Add hub" in out
    assert any("comment 12 robo 2026-10-24" in line for line in out)
    assert not any("A missing control" in line for line in out)  # ticked
    assert not any("#9" in line or "not an issue" in line for line in out)
    assert out[-1] == "no open items: #8"


def test_open_titles_prints_each_open_issue_on_one_line_with_its_open_item_count(
    fake, capsys
):
    fake.listing = [
        fake.issue | {"comments": 2, "assignees": [{"login": "carver"}]},
        {"number": 8, "title": "Past research", "body": "- [x] done", "comments": 0},
        {
            "number": 9,
            "title": "A pull request",
            "body": "- [ ] not an issue's",
            "comments": 0,
            "pull_request": {},
        },
        {"number": 10, "title": "One step", "body": "- [ ] the step", "comments": 0},
    ]

    assert tool.main(["open", "--titles"]) == 0

    # 7: two open in the body (ticked and fenced ones left out), two in comment 12
    assert capsys.readouterr().out.splitlines() == [
        "#7 Criterion: consistent (4 open items)",
        "#8 Past research",
        "#10 One step (1 open item)",
    ]


def test_open_titles_prints_a_long_title_in_full(fake, capsys):
    title = "Site design " + "with a long subtitle " * 8
    fake.listing = [{"number": 8, "title": title, "body": "", "comments": 0}]

    assert tool.main(["open", "--titles"]) == 0

    assert capsys.readouterr().out == f"#8 {title}\n"


@pytest.mark.parametrize("form", [[], ["--titles"]], ids=["open", "open --titles"])
def test_open_reads_comments_only_for_issues_that_have_some(fake, form):
    fake.listing = [
        fake.issue | {"comments": 2},
        {"number": 8, "title": "No comments", "body": "", "comments": 0},
    ]

    assert tool.main(["open", *form, "--repo", "other/thing"]) == 0

    assert [args[-1].split("?")[0] for args in fake.calls] == [
        "repos/other/thing/issues",  # the listing
        "repos/other/thing/issues/7/comments",
    ]


@pytest.mark.parametrize("command", ["show", "read"])
def test_one_issue_with_no_comments_costs_one_call(fake, capsys, command):
    fake.issue["comments"] = 0

    assert tool.main([command, "7", "--repo", "other/thing"]) == 0

    assert [args[-1] for args in fake.calls] == ["repos/other/thing/issues/7"]
    assert "The forecast question" in capsys.readouterr().out


def test_show_cuts_a_long_item_to_one_line(fake, capsys):
    fake.issue["body"] = "- [ ] " + "x" * 300
    tool.main(["show", "7"])
    line = next(line for line in capsys.readouterr().out.splitlines() if "xxx" in line)
    assert line.endswith("…") and len(line) < 120


def test_read_prints_the_body_and_every_comment_with_author_and_date(fake, capsys):
    fake.issue |= {"user": {"login": "jc"}, "created_at": "2026-10-22T10:00:00Z"}

    assert tool.main(["read", "7"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("#7 Criterion: consistent (open)\n")
    assert "body jc 2026-10-22" in out and "The forecast question" in out
    assert "comment 11 alice 2026-10-23\nNo tasks." in out
    assert out.index("No tasks.") < out.index("Replace the forecast measure")


def test_mentions_are_this_repos_issue_numbers_in_order_outside_code():
    text = (
        "First #25, then #46 and #25 again.\n"
        "Not other/repo#9, not `#10`, not a#11.\n"
        "```\n#12 in a fence\n```\n"
        "(#48) and #1."
    )
    assert tool.mentions(text) == [25, 46, 48, 1]


def test_a_stray_backtick_hides_no_mention_past_its_paragraph():
    assert tool.mentions("the ` key, then #5.\n\nLater `x` and #6") == [5, 6]


def test_read_ends_with_each_mentioned_issues_state_items_and_title(fake, capsys):
    fake.issue["body"] = (
        "Order: #25, then #46, #99 (no such issue), #48, and this one, #7."
    )

    assert tool.main(["read", "7"]) == 0

    out = capsys.readouterr().out
    assert out.endswith(
        "--- mentions\n"
        "#25 open, 2 open items: Agents offer to post\n"
        "#46 closed: A beta stamp\n"
        "#48 open: No items\n"
    )
    graphql = [a for a in fake.calls if a[:2] == ("api", "graphql")]
    assert len(graphql) == 1  # one call for every mention


def test_show_ends_with_the_mentions_too(fake, capsys):
    fake.comments[0]["body"] = "Blocked on #46."
    assert tool.main(["show", "7"]) == 0
    assert capsys.readouterr().out.endswith("--- mentions\n#46 closed: A beta stamp\n")


def test_an_issue_that_mentions_none_costs_no_graphql_call(fake, capsys):
    assert tool.main(["read", "7"]) == 0
    assert "mentions" not in capsys.readouterr().out
    assert not [a for a in fake.calls if a[:2] == ("api", "graphql")]


def test_a_failed_mentions_lookup_still_prints_the_issue(fake, capsys):
    fake.issue["body"] = "See #25."
    fake.graphql_fails = True
    assert tool.main(["read", "7"]) == 0
    captured = capsys.readouterr()
    assert "See #25." in captured.out and "--- mentions" not in captured.out
    assert "couldn't look up the issues #7 mentions" in captured.err


def claim_comment(session, kind="claim"):
    return {
        "id": 20 + len(session),
        "user": {"login": "robo"},
        "created_at": "2026-10-25T08:04:00Z",
        "body": f"Claimed.\n<!-- issue-todo {kind} {session} -->",
    }


def test_claim_posts_a_comment_naming_the_session(fake, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcd1234-ffff-0000")

    assert tool.main(["claim", "7"]) == 0

    ((path, body),) = fake.patched
    assert path.endswith("/issues/7/comments")
    assert "abcd1234" in body and "<!-- issue-todo claim abcd1234 -->" in body


def test_claim_refuses_while_another_session_holds_it(fake, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcd1234-ffff-0000")
    fake.comments.append(claim_comment("other999"))

    assert tool.main(["claim", "7"]) == 1

    assert fake.patched == []
    err = capsys.readouterr().err
    assert "other999" in err and "2026-10-25" in err


def test_a_released_claim_can_be_claimed_again(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcd1234-ffff-0000")
    fake.comments += [claim_comment("other999"), claim_comment("other999", "release")]

    assert tool.main(["claim", "7"]) == 0
    assert len(fake.patched) == 1


def test_claiming_your_own_claim_again_posts_nothing(fake, capsys):
    fake.comments.append(claim_comment("mine0001"))

    assert tool.main(["claim", "7", "--as", "mine0001"]) == 0

    assert fake.patched == [] and "already" in capsys.readouterr().out


def test_release_posts_a_release_comment(fake, monkeypatch):
    fake.comments.append(claim_comment("mine0001"))

    assert tool.main(["release", "7", "--as", "mine0001"]) == 0

    ((_, body),) = fake.patched
    assert "<!-- issue-todo release mine0001 -->" in body


def test_claim_without_a_session_or_a_name_says_so(fake, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit) as exited:
        tool.main(["claim", "7"])

    assert exited.value.code == 2
    assert "--as" in capsys.readouterr().err and fake.patched == []


def test_release_without_a_claim_posts_nothing(fake, capsys):
    assert tool.main(["release", "7", "--as", "mine0001"]) == 1

    assert fake.patched == [] and "no claim" in capsys.readouterr().err


def test_a_name_with_a_space_is_refused(fake, capsys):
    with pytest.raises(SystemExit):
        tool.main(["claim", "7", "--as", "jc laptop"])

    assert fake.patched == []


def test_unticked_prints_each_open_item_of_the_body_and_comments(fake, capsys):
    fake.issue["body"] += "\nSee #25."  # a mention, which unticked doesn't look up

    assert tool.main(["unticked", "7"]) == 0

    assert capsys.readouterr().out == (
        "#7 The forecast question\n"
        "#7 a nested step\n"
        "#7 Replace the forecast measure\n"
        "#7 Add hub\n"
    )
    assert not [a for a in fake.calls if a[:2] == ("api", "graphql")]


def test_unticked_prints_nothing_for_an_issue_with_every_item_ticked(fake, capsys):
    fake.issue["body"] = "- [x] done"
    fake.comments[1]["body"] = "- [x] also done"
    assert tool.main(["unticked", "7"]) == 0
    assert capsys.readouterr().out == ""
