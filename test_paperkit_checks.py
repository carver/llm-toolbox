"""The checks paperkit runs on paper.typ's source: a semicolon Typst swallows after a call, a `#` it swallows
before a digit, and wording that rests on the numbers with no #claim behind it; and the citation check: a
source the paper cites must have a row in citations.md saying someone read it in full, while a source it
doesn't cite, or a label that isn't a citation, isn't checked."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from paperkit.checks import (
    Problem,
    probe_url,
    swallowed_hashes,
    swallowed_semicolons,
    unclaimed_wording,
    unread_citations,
)

FIX = 'drops its ";" (write \\; to keep it)'

SOURCE = """= Results
GLM goes from #n("a") to #n("b"); Opus mentions antitrust in #n("c").
#claim("up", ("a", "b"), (x, y) => x < y);
// a comment about #n("d"); left alone
A sentence ends #n("e"). Another escapes it #n("f")\\; fine.
To do: #todo[say why]; then #link("https://x.org/a")[a link]; and a dash #sym.dash; here.
Deep: #claim("d", ("a",), r => calc.max(r, (1)) > 0); a trailing note // #n("g"); not code
"""


def test_a_semicolon_right_after_a_call_is_a_problem_naming_its_line():
    assert swallowed_semicolons(SOURCE) == [
        Problem("semicolon", f'line 2: #n("b"); {FIX}'),
        Problem(
            "semicolon", f'line 3: #claim("up", ("a", "b"), (x, y) => x < y); {FIX}'
        ),
        Problem("semicolon", f"line 6: #todo[say why]; {FIX}"),
        Problem("semicolon", f'line 6: #link("https://x.org/a")[a link]; {FIX}'),
        Problem("semicolon", f"line 6: #sym.dash; {FIX}"),
        Problem(
            "semicolon",
            f'line 7: #claim("d", ("a",), r => calc.max(r, (1)) > 0); {FIX}',
        ),
    ]


WORDING = """Every model reaches it in #n("a").
#claim("every model", ("a",), r => r == 1)
Nearly all answers are at 48 (#n("b")), and none at 36.
Every model agrees, with no number cited.

The strongest lever is the record, #pct("c").
The answers rose from #n("d") to #n("e"), mostly at 30.

Lockstep raises it to #n("f").
And lowers the mentions to #n("g").
#claim("lockstep", ("f", "g"), (f, g) => f > g)
// Every comment is skipped, #n("h").
A key's words don't count: #n("only.most.every"), nor quoted "all" words, nor number-only arms #n("i").
Neither firm deviates at #n("j"). // every trailing comment is skipped

#claim("neither", ("j",), r => r == 0)
"""


def test_wording_that_rests_on_cited_numbers_needs_a_claim_later_in_its_paragraph():
    assert (
        unclaimed_wording(WORDING)
        == [
            "line 3: all, nearly, none, with no #claim after it",
            "line 6: strongest, with no #claim after it",
            "line 7: mostly, rose, with no #claim after it",
            "line 14: neither, with no #claim after it",  # the claim after a blank line is another paragraph's
        ]
    )


HASHES = """The compliance criterion's results (#39).
Escaped (\\#40), quoted #n("key#41"), raw `#42`, and a color #text(fill: rgb("#c00000"))[red].
// A comment names #43.
Two on a line: #7 and #8.
"""


def test_a_hash_before_a_digit_is_a_problem_naming_its_line():
    assert swallowed_hashes(HASHES) == [
        Problem("hash", "line 1: #39 renders as 39 (write \\#39)"),
        Problem("hash", "line 4: #7 renders as 7 (write \\#7)"),
        Problem("hash", "line 4: #8 renders as 8 (write \\#8)"),
    ]


BIB = """@article{read2020,
  title = {Read},
}

@book{skimmed2019,
  title = {Skimmed},
}

@misc{rowless2024,
  title = {No row},
}

@misc{uncited2021,
  title = {Uncited},
}

  @misc{ spaced2022 ,
  title = {Indented, with spaces round its key},
}
"""

TABLE = """# Citations

| key | source | read | used for | where | in the paper |
|---|---|---|---|---|---|
| `read2020` | A (2020) | full text | a fact | intro | yes |
| `skimmed2019` | B (2019) | catalogue | a fact | intro | yes |
| `uncited2021` | C (2021) | not checked | a fact | nowhere | no |

Read only by title, so not cited: D (1999).
"""


def test_a_cited_source_read_in_full_passes():
    assert unread_citations({"read2020", "fig-game"}, BIB, TABLE) == []


def test_a_cited_source_not_read_in_full_is_a_problem_saying_how_much_was_read():
    assert unread_citations({"read2020", "skimmed2019"}, BIB, TABLE) == [
        Problem(
            "citation",
            "@skimmed2019 is cited, but citations.md says only its catalogue was read",
        ),
    ]


def test_a_cited_source_with_no_row_is_a_problem():
    assert unread_citations({"rowless2024"}, BIB, TABLE) == [
        Problem(
            "citation", "@rowless2024 is cited, but citations.md has no row for it"
        ),
    ]


def test_an_entry_indented_or_spaced_round_its_key_is_still_a_bibliography_key():
    assert unread_citations({"spaced2022"}, BIB, TABLE) == [
        Problem("citation", "@spaced2022 is cited, but citations.md has no row for it"),
    ]


def test_an_uncited_source_and_a_label_that_isnt_a_citation_go_unchecked():
    assert unread_citations({"fig-game", "app-pilot"}, BIB, TABLE) == []
    assert unread_citations(set(), BIB, TABLE) == []


def test_a_pipe_line_before_the_table_and_the_note_after_it_are_skipped():
    table = "| an aside in pipes |\n\n" + TABLE
    assert unread_citations({"read2020", "skimmed2019"}, BIB, table) == [
        Problem(
            "citation",
            "@skimmed2019 is cited, but citations.md says only its catalogue was read",
        ),
    ]


def test_no_table_with_key_and_read_columns_is_one_problem():
    assert unread_citations({"read2020", "skimmed2019"}, BIB, "") == [
        Problem("citation", "citations.md has no table with key and read columns"),
    ]
    assert unread_citations(set(), BIB, "") == []


def test_a_row_with_the_wrong_number_of_cells_is_a_problem_naming_its_key():
    table = TABLE.replace(
        "| `read2020` | A (2020) |", "| `read2020` | A \\| B (2020) |"
    )
    assert unread_citations({"read2020"}, BIB, table) == [
        Problem(
            "citation",
            "@read2020 is cited, but its row in citations.md doesn't have the table's columns",
        ),
    ]


def test_a_read_value_the_check_doesnt_know_is_named():
    table = TABLE.replace("| catalogue |", "| skimmed |")
    assert unread_citations({"skimmed2019"}, BIB, table) == [
        Problem(
            "citation",
            "@skimmed2019 is cited, but its read column in citations.md is 'skimmed', not full text",
        ),
    ]


def test_a_doi_link_is_checked_with_the_doi_resolver_not_the_publisher():
    # Publishers' sites often refuse scripts (a 403 challenge page) where a browser gets through.
    assert (
        probe_url("https://doi.org/10.1257/aer.20190623")
        == "https://doi.org/api/handles/10.1257/aer.20190623"
    )
    assert (
        probe_url("https://dx.doi.org/10.1/x") == "https://doi.org/api/handles/10.1/x"
    )
    assert probe_url("https://example.com/doi.org/x") == "https://example.com/doi.org/x"
