"""procmatch.shortened keeps a listed command line short and the match in view."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from procmatch import LISTED_CHARS, shortened

MAX = LISTED_CHARS + 40  # room for the ellipses and the length note


def test_a_short_line_is_left_whole() -> None:
    assert shortened("sleep 30", "sleep", True) == "sleep 30"


def test_a_match_far_in_shows_with_the_start_and_the_full_length() -> None:
    line = "bash -c " + "x" * 5000 + " marker tail"
    out = shortened(line, "marker", True)
    assert out.startswith("bash -c ") and "marker" in out
    assert out.endswith(f"[{len(line)} chars]") and len(out) <= MAX


def test_a_match_as_wide_as_the_line_is_still_cut() -> None:
    line = "bash -c " + "x" * 7000 + " marker"
    assert len(shortened(line, "bash.*marker", True)) <= MAX


def test_a_name_match_shows_the_start() -> None:
    line = "python3 " + "y" * 5000
    out = shortened(line, "python3", False)
    assert out.startswith("python3 ") and out.endswith(f"[{len(line)} chars]")
    assert len(out) <= MAX


def test_no_gap_marker_when_the_match_follows_the_start() -> None:
    line = "z" * 90 + "marker" + "w" * 5000
    out = shortened(line, "marker", True)
    assert "z" * 90 + "marker" in out
