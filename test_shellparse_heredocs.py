"""lib/shellparse: a heredoc closes as bash closes it, on a line that is its delimiter
exactly, or for <<- after the line's leading tabs; a space around it, or a tab under
plain <<, leaves the line in the body."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import shellparse


@pytest.mark.parametrize(
    ("command", "body"),
    [
        ("cat <<EOF\n  EOF\nEOF\n", "  EOF\n"),
        ("cat <<EOF\n\tEOF\nEOF\n", "\tEOF\n"),
        ("cat <<EOF\nEOF \nEOF\n", "EOF \n"),
        ("cat <<-EOF\n\tx\n  EOF\n\tEOF\n", "x\n  EOF\n"),
    ],
    ids=["spaces-before", "tab-under-plain", "space-after", "dash-spaces"],
)
def test_a_line_that_isnt_the_delimiter_exactly_stays_in_the_body(command, body):
    assert shellparse.heredocs(command) == [("EOF", body)]


def test_a_dash_heredoc_inside_a_substitution_closes_and_the_command_goes_on():
    # Else the substitution would swallow the rest, and the gates miss the rm.
    cleaned = shellparse.prepare("x=$(cat <<-EOF\n\tbody\n\tEOF\n)\nrm -rf x\n")[0]

    assert "rm -rf x" in cleaned


def test_a_crlf_command_closes_its_heredoc_on_the_line_with_the_cr():
    command = "x=$(cat <<EOF\r\nbody\r\nEOF\r\n)\r\nrm -rf x\r\n"

    assert "rm -rf x" in shellparse.prepare(command)[0]
    assert shellparse.heredocs("cat <<EOF\r\nbody\r\nEOF\r\necho after\r\n") == [
        ("EOF", "body\r\n")
    ]


def test_a_dash_heredoc_closes_after_its_leading_tabs():
    assert shellparse.heredocs("cat <<-EOF\n\tx\n\t\tEOF\nafter\n") == [("EOF", "x\n")]
