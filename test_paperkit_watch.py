"""build-paper watch. Its schedule goes through `Watch` with a scripted HEAD, sources, clock, live compile
and full build: a full build at the start, after each HEAD move and for a newly missing key once the text
settles; a live compile after each edit, and again after a full build that ran over newer text. The live
compile itself runs Typst on a tiny paper."""

import json
import subprocess
import sys

import pytest

from paperkit_testing import LIB, write_minimal

sys.path.insert(0, str(LIB))
from paperkit.__main__ import main
from paperkit.config import Config
from paperkit.git import Repo
from paperkit.watch import LiveCompile, Watch, live_compile

SETTLE = 5.0


class Script:
    """The world a `Watch` sees: what each poll reads, and what it did."""

    def __init__(self) -> None:
        self.head = "a"
        self.text = 0  # bump it to edit the sources
        self.t = 0.0
        self.result = LiveCompile()
        self.building = False
        self.other: int | None = None  # the pid of a build another process runs
        self.builds = 0
        self.compiles = 0
        self.said: list[str] = []
        self.watch = Watch(
            head=lambda: self.head,
            sources=lambda: self.text,
            live=self.live,
            full=self,
            others=lambda: self.other,
            now=lambda: self.t,
            settle=SETTLE,
            say=self.said.append,
        )

    def live(self) -> LiveCompile:
        self.compiles += 1
        return self.result

    def start(self) -> None:
        assert not self.building, "a full build started while one ran"
        self.builds += 1
        self.building = True

    def running(self) -> bool:
        return self.building

    def poll(self, seconds: float = 1.0) -> None:
        self.t += seconds
        self.watch.poll()

    def finish(self) -> None:
        """The running full build ends; the next poll sees it."""
        self.building = False
        self.poll()


def test_the_start_brings_a_full_build_and_a_live_compile():
    s = Script()
    s.poll()
    assert (s.builds, s.compiles) == (1, 1)


def test_an_edit_brings_a_live_compile_and_quiet_brings_none():
    s = Script()
    s.poll()
    s.finish()
    s.poll()
    assert s.compiles == 1
    s.text += 1
    s.poll()
    s.poll()
    assert (s.builds, s.compiles) == (1, 2)


def test_head_moves_during_a_build_bring_one_more_after_it():
    s = Script()
    s.poll()
    s.head = "b"
    s.poll()
    s.head = "c"
    s.poll()
    assert s.builds == 1
    s.finish()
    assert s.builds == 2
    s.finish()
    s.poll()
    assert s.builds == 2


def test_an_unreadable_head_builds_nothing():
    s = Script()
    s.head = ""
    s.poll()
    assert s.builds == 0
    s.head = "a"
    s.poll()
    s.finish()
    s.head = ""
    s.poll()
    s.head = "a"
    s.poll()
    assert s.builds == 1


def test_an_error_while_idle_is_reported():
    s = Script()
    s.poll()
    s.finish()
    s.result = LiveCompile(error="error: unclosed delimiter\n  paper.typ:12")
    s.text += 1
    s.poll()
    assert any("paper.typ:12" in line for line in s.said)


def test_an_error_during_a_full_build_waits_for_it_and_then_recompiles():
    s = Script()
    s.result = LiveCompile(error="error: file not found (build/figures/x.png)")
    s.poll()
    s.text += 1
    s.poll()
    assert not any("file not found" in line for line in s.said)
    compiles = s.compiles
    s.result = LiveCompile()
    s.finish()
    assert s.compiles == compiles + 1


def test_another_process_s_build_pauses_the_watch_and_its_end_resumes_it():
    s = Script()
    s.poll()
    s.finish()
    s.other = 4242
    s.poll()
    s.poll()
    assert [line for line in s.said if "paused" in line] == [
        "paused: another build is running (pid 4242)"
    ]
    s.other = None
    s.poll()
    assert "resumed" in s.said


def test_an_error_during_another_process_s_build_waits_for_it_and_then_recompiles():
    s = Script()
    s.poll()
    s.finish()
    s.other = 4242
    s.result = LiveCompile(error="error: file not found (build/figures/x.png)")
    s.text += 1
    s.poll()
    assert not any("file not found" in line for line in s.said)
    s.result = LiveCompile()
    compiles = s.compiles
    s.other = None
    s.poll()
    assert s.compiles == compiles + 1


def test_a_key_missing_during_another_process_s_build_still_brings_a_build_of_ours():
    s = Script()
    s.poll()
    s.finish()
    s.other = 4242
    s.result = LiveCompile(missing=frozenset({"new"}))
    s.text += 1
    s.poll()
    s.other = None
    s.poll(SETTLE)
    assert s.builds == 2


def test_the_end_of_another_process_s_build_brings_a_recompile_over_its_pdf():
    s = Script()
    s.poll()
    s.finish()
    s.other = 4242
    s.poll()
    compiles = s.compiles
    s.other = None
    s.poll()
    assert s.compiles == compiles + 1


def test_an_edit_during_a_full_build_is_recompiled_after_it():
    s = Script()
    s.poll()
    s.text += 1
    s.poll()
    compiles = s.compiles
    s.finish()
    assert s.compiles == compiles + 1


def test_a_build_over_unchanged_text_brings_no_live_compile_after_it():
    s = Script()
    s.poll()
    compiles = s.compiles
    s.finish()
    assert s.compiles == compiles


def test_a_newly_missing_key_brings_a_full_build_once_the_text_settles():
    s = Script()
    s.poll()
    s.finish()
    s.result = LiveCompile(missing=frozenset({"new.key"}))
    s.text += 1
    s.poll()
    s.poll(SETTLE - 2)
    assert s.builds == 1
    s.poll(2)
    assert s.builds == 2


def test_an_edit_restarts_the_settle_wait():
    s = Script()
    s.poll()
    s.finish()
    s.result = LiveCompile(missing=frozenset({"new"}))
    s.text += 1
    s.poll()
    s.poll(SETTLE - 1)
    s.text += 1
    s.poll()
    s.poll(SETTLE - 1)
    assert s.builds == 1
    s.poll(1)
    assert s.builds == 2


def test_a_key_missing_during_a_full_build_is_left_to_that_build():
    s = Script()
    s.result = LiveCompile(missing=frozenset({"new"}))
    s.poll()
    s.text += 1
    s.poll()
    s.finish()
    s.text += 1
    s.poll(SETTLE)
    s.poll(SETTLE)
    assert s.builds == 1


def test_a_key_still_settling_when_head_moves_is_left_to_that_build():
    s = Script()
    s.poll()
    s.finish()
    s.result = LiveCompile(missing=frozenset({"new"}))
    s.text += 1
    s.poll()
    s.head = "b"
    s.poll()
    s.finish()
    s.poll(SETTLE)
    assert s.builds == 2


def test_a_key_brings_one_full_build_and_a_head_build_over_it_counts_as_that_one():
    s = Script()
    s.poll()
    s.finish()
    s.result = LiveCompile(missing=frozenset({"gone"}))
    s.text += 1
    s.poll(SETTLE)
    s.poll(SETTLE)
    assert s.builds == 2
    s.finish()
    s.text += 1
    s.poll(SETTLE)
    s.poll(SETTLE)
    assert s.builds == 2
    s.head = "b"
    s.poll()
    s.finish()
    s.text += 1
    s.poll(SETTLE)
    s.poll(SETTLE)
    assert s.builds == 3


HEAD = '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\n'


@pytest.fixture
def paper_dir(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "numbers.json").write_text("{}")
    return tmp_path


def test_a_live_compile_writes_the_draft_and_reads_back_its_missing_keys(paper_dir):
    write_minimal(paper_dir, HEAD + '#n("a.b")\n')
    assert live_compile(paper_dir, None, Config()) == LiveCompile(
        missing=frozenset({"a.b"})
    )
    assert (paper_dir / "build" / "DRAFT-paper.pdf").read_bytes().startswith(b"%PDF")


def test_a_key_the_ledger_holds_as_a_pooled_count_is_not_missing_for_a_build(paper_dir):
    pooled = {
        "kind": "pooled",
        "k": 3,
        "n": 4,
        "rate": 0.75,
        "cells": 2,
        "how": "",
        "source": {},
    }
    (paper_dir / "build" / "numbers.json").write_text(json.dumps({"p": pooled}))
    write_minimal(paper_dir, HEAD + '#ci("p")\n')
    assert live_compile(paper_dir, None, Config()) == LiveCompile()


def test_a_live_compile_that_fails_keeps_the_last_draft(paper_dir):
    (paper_dir / "build" / "DRAFT-paper.pdf").write_bytes(b"the last good one")
    write_minimal(paper_dir, HEAD + "#nonsense\n")
    result = live_compile(paper_dir, None, Config())
    assert "nonsense" in result.error
    assert (
        paper_dir / "build" / "DRAFT-paper.pdf"
    ).read_bytes() == b"the last good one"


def test_a_repo_with_no_commit_yet_reads_as_no_head(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repo = Repo.of(tmp_path)
    assert repo is not None
    assert repo.head() == ""


def test_watch_outside_a_repo_fails_with_why(tmp_path, capsys):
    assert main(["watch"], tmp_path) == 1
    assert "not in a git repository" in capsys.readouterr().err
