"""lib/refused.py, which saves a command a gate refused, and lib/shellparse.py's
heredocs(), which finds the heredoc bodies it saves beside it. Imported, as lib/
modules may be; each test's temp root comes from conftest's private_scratch_root."""

import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
import refused
from shellparse import NOTHING_RAN, heredocs

SESSION = "0faf005f-29ec-4d19-a39e-296955a78d9b"
PROJECT = "-home-u-code-repo"
DAY = 24 * 3600


def claude_dir():
    return Path(os.environ["REFUSED_SCRATCH_ROOT"]) / f"claude-{os.getuid()}"


def session_dir(project=PROJECT, session=SESSION):
    folder = claude_dir() / project / session
    folder.mkdir(parents=True)
    return folder


def event(command, session=SESSION, project=PROJECT):
    return {
        "session_id": session,
        "transcript_path": f"/home/u/.claude/projects/{project}/{session}.jsonl",
        "cwd": "/home/u/code/repo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def saved_paths(ending):
    """The files the ending's sentence names, in order: the command's by its absolute
    path, then each heredoc body's by its name in the same folder."""
    names = re.findall(r"(\S+?\.txt)\b", ending)
    if not names:
        return []
    command = Path(names[0])
    return [command] + [command.parent / name for name in names[1:]]


# heredocs()


def test_a_heredoc_body_keeps_its_final_newline():
    assert heredocs("cat <<EOF\na\nb\nEOF\necho done") == [("EOF", "a\nb\n")]


def test_quotes_around_the_delimiter_are_removed():
    found = heredocs("cat <<'END' >f\nx $y\nEND\n")
    assert found == [("END", "x $y\n")]


def test_a_delimiter_with_characters_past_a_name_is_read_whole():
    command = "cat <<EOF-1\nbody\nEOF-1\necho after\n"
    assert heredocs(command) == [("EOF-1", "body\n")]
    assert heredocs("cat <<'END.X'\nbody\nEND.X\n") == [("END.X", "body\n")]


def test_a_dash_heredoc_has_its_leading_tabs_stripped():
    assert heredocs("cat <<-EOF\n\tone\n\t\ttwo\n\tEOF\n") == [("EOF", "one\ntwo\n")]


def test_two_heredocs_on_one_line_each_keep_their_own_delimiter():
    command = "paste <<A 3<<B\nfirst\nA\nsecond\nB\n"
    assert heredocs(command) == [("A", "first\n"), ("B", "second\n")]


def test_heredocs_on_separate_lines_come_in_order():
    command = "cat <<ONE\n1\nONE\ncat <<TWO\n2\nTWO\n"
    assert heredocs(command) == [("ONE", "1\n"), ("TWO", "2\n")]


def test_one_left_open_or_empty_is_left_out():
    assert heredocs("cat <<EOF\nnever closed\n") == []
    assert heredocs("cat <<EOF\nEOF\n") == []


def test_a_double_angle_inside_quotes_opens_no_heredoc():
    assert heredocs('echo "a<<b"\necho b\n') == []
    assert heredocs("echo $((1<<2))\n") == []


# ending()


def test_without_a_session_folder_the_ending_is_nothing_ran_alone():
    assert refused.ending(event("make")) == NOTHING_RAN
    assert not claude_dir().exists() or not any(claude_dir().rglob("*.txt"))


def test_the_command_is_saved_in_the_sessions_scratchpad_and_named():
    folder = session_dir()
    command = "python3 - <<'PY'\nprint(1)\nPY\ngit commit -m x"
    ending = refused.ending(event(command))
    assert ending.startswith(NOTHING_RAN + " ")
    paths = saved_paths(ending)
    assert paths[0].parent == folder / "scratchpad" / "refused"
    assert paths[0].read_text() == command
    assert re.fullmatch(r"[0-9a-f]{12}\.txt", paths[0].name)


def test_each_heredoc_body_is_saved_beside_it_and_named():
    session_dir()
    command = "apply-edits <<'EOF'\n--- a\nold\nEOF\ncat <<-PY\n\tprint(2)\n\tPY\n"
    paths = saved_paths(refused.ending(event(command)))
    assert len(paths) == 3
    stem = paths[0].stem
    assert paths[1].name == f"{stem}-1-EOF.txt"
    assert paths[1].read_text() == "--- a\nold\n"
    assert paths[2].name == f"{stem}-2-PY.txt"
    assert paths[2].read_text() == "print(2)\n"


def test_a_delimiter_outside_name_characters_is_made_safe_in_the_file_name():
    session_dir()
    paths = saved_paths(refused.ending(event("cat <<'a b.c'\nx\na b.c\n")))
    assert paths[1].name.endswith("-1-a_b_c.txt")
    assert paths[1].read_text() == "x\n"


def test_the_files_are_private_to_the_user():
    folder = session_dir()
    paths = saved_paths(refused.ending(event("cat <<EOF\nsecret\nEOF\n")))
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for made in (folder / "scratchpad", folder / "scratchpad" / "refused"):
        assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_the_same_command_refused_twice_is_saved_once_under_one_name():
    session_dir()
    first = saved_paths(refused.ending(event("make all")))
    second = saved_paths(refused.ending(event("make all")))
    assert first == second
    assert len(list(first[0].parent.iterdir())) == 1


def test_the_folder_named_like_the_transcripts_wins_over_another_with_the_id():
    session_dir(project="-elsewhere")
    folder = session_dir()
    paths = saved_paths(refused.ending(event("make")))
    assert paths[0].parent == folder / "scratchpad" / "refused"


def test_without_that_folder_the_one_folder_holding_the_id_is_used():
    folder = session_dir(project="-cut-and-hashed-name")
    paths = saved_paths(refused.ending(event("make")))
    assert paths[0].parent == folder / "scratchpad" / "refused"


def test_two_other_folders_holding_the_id_save_nothing():
    session_dir(project="-one")
    session_dir(project="-two")
    assert refused.ending(event("make")) == NOTHING_RAN


@pytest.mark.parametrize("session", ["../x", "abc/../x", "", None, 7])
def test_a_session_id_that_isnt_a_plain_name_saves_nothing(session):
    session_dir(session="x")
    claude_dir().joinpath("x").mkdir()
    assert refused.ending(event("make", session=session)) == NOTHING_RAN
    assert not list(claude_dir().rglob("*.txt"))


def test_a_save_that_fails_still_ends_with_nothing_ran():
    folder = session_dir()
    (folder / "scratchpad").write_text("a file where the folder would go\n")
    assert refused.ending(event("make")) == NOTHING_RAN


def test_an_event_missing_its_fields_ends_with_nothing_ran():
    assert refused.ending({}) == NOTHING_RAN
    assert refused.ending({"session_id": SESSION, "tool_input": None}) == NOTHING_RAN


def old(path, days):
    then = time.time() - days * DAY
    os.utime(path, (then, then), follow_symlinks=False)


def refused_folder(session):
    folder = session_dir(project="-other", session=session) / "scratchpad" / "refused"
    folder.mkdir(parents=True)
    return folder


def test_a_save_prunes_saved_files_past_30_days_in_every_session():
    session_dir()
    other = refused_folder("1111")
    stale = other / "0123456789ab.txt"
    stale_body = other / "0123456789ab-1-EOF.txt"
    stale_temp = other / "0123456789ab.txt.k2j3h4.tmp"
    fresh = other / "ba9876543210.txt"
    for path in (stale, stale_body, stale_temp, fresh):
        path.write_text("x\n")
    for path in (stale, stale_body, stale_temp):
        old(path, 31)
    old(fresh, 29)
    refused.ending(event("make"))
    assert not stale.exists()
    assert not stale_body.exists()
    assert not stale_temp.exists()
    assert fresh.exists()


def test_the_prune_leaves_files_it_didnt_name_alone():
    session_dir()
    other = refused_folder("2222")
    theirs = other / "notes.txt"
    theirs.write_text("an agent's own file\n")
    old(theirs, 90)
    elsewhere = claude_dir() / "-other" / "2222" / "scratchpad" / "0123456789ab.txt"
    elsewhere.write_text("x\n")
    old(elsewhere, 90)
    refused.ending(event("make"))
    assert theirs.exists()
    assert elsewhere.exists()


def test_three_heredoc_bodies_are_listed_with_an_and():
    session_dir()
    command = "cat <<A\n1\nA\ncat <<B\n2\nB\ncat <<C\n3\nC\n"
    ending = refused.ending(event(command))
    assert re.search(r"-1-A\.txt, \S+-2-B\.txt and \S+-3-C\.txt beside it\.$", ending)


def test_a_body_that_cant_be_written_still_names_the_saved_command(monkeypatch):
    session_dir()
    real = refused.tempfile.mkstemp
    calls = []

    def failing_after_the_first(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(refused.tempfile, "mkstemp", failing_after_the_first)
    command = "cat <<EOF\nbody\nEOF\n"
    paths = saved_paths(refused.ending(event(command)))
    assert len(paths) == 1
    assert paths[0].read_text() == command
    assert not [p for p in paths[0].parent.iterdir() if p.suffix == ".tmp"]


@pytest.mark.parametrize("linked", ["scratchpad", "session"])
def test_the_prune_follows_no_linked_scratchpad_or_session(tmp_path, linked):
    session_dir()
    target = tmp_path / "linked"
    (target / "scratchpad" / "refused").mkdir(parents=True)
    kept = target / "scratchpad" / "refused" / "0123456789ab.txt"
    kept.write_text("x\n")
    old(kept, 90)
    project = claude_dir() / "-other"
    project.mkdir()
    if linked == "session":
        (project / "4444").symlink_to(target)
    else:
        (project / "4444").mkdir()
        (project / "4444" / "scratchpad").symlink_to(target / "scratchpad")
    refused.ending(event("make"))
    assert kept.exists()


def test_the_prune_follows_no_link_named_refused(tmp_path):
    session_dir()
    target = tmp_path / "linked"
    target.mkdir()
    kept = target / "0123456789ab.txt"
    kept.write_text("x\n")
    old(kept, 90)
    scratchpad = session_dir(project="-other", session="3333") / "scratchpad"
    scratchpad.mkdir()
    (scratchpad / "refused").symlink_to(target)
    refused.ending(event("make"))
    assert kept.exists()
