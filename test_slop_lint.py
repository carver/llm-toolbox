"""slop-lint flags the mechanical AI-writing tells from the unslop skill."""

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path
from typing import NamedTuple

SCRIPT = Path(__file__).resolve().parent / "bin" / "slop-lint"

_loader = importlib.machinery.SourceFileLoader("slop_lint", str(SCRIPT))
_spec = importlib.util.spec_from_loader("slop_lint", _loader)
assert _spec is not None
slop_lint = importlib.util.module_from_spec(_spec)
_loader.exec_module(slop_lint)


class Run(NamedTuple):
    status: int
    lines: list[str]  # each finding, without the path in front
    err: str


def lint(tmp_path, capsys, text: str, *flags: str) -> Run:
    path = tmp_path / "a.md"
    path.write_text(text, encoding="utf-8")
    status = slop_lint.main([*flags, str(path)])
    out, err = capsys.readouterr()
    return Run(
        status, [line.removeprefix(f"{path}:") for line in out.splitlines()], err
    )


def flagged(lines: list[str]) -> list[str]:
    """Each finding without its replacement: line:col: rule: text."""
    return [line.split(" → ")[0] for line in lines]


def test_an_em_dash_is_flagged_at_its_line_and_column(tmp_path, capsys):
    status, lines, _ = lint(
        tmp_path, capsys, "Fine line.\nThe tool \N{EM DASH} it works.\n"
    )

    assert status == 1
    assert len(lines) == 1
    assert lines[0].startswith("2:10: dash: \N{EM DASH}")


def test_an_en_dash_is_flagged_too(tmp_path, capsys):
    status, lines, _ = lint(tmp_path, capsys, "Pages 3\N{EN DASH}5.\n")

    assert status == 1
    assert flagged(lines) == ["1:8: dash: \N{EN DASH}"]


def test_curly_quotes_and_apostrophes_are_flagged_with_the_straight_one(
    tmp_path, capsys
):
    status, lines, _ = lint(
        tmp_path,
        capsys,
        "\N{LEFT DOUBLE QUOTATION MARK}It\N{RIGHT SINGLE QUOTATION MARK}s fine,\N{RIGHT DOUBLE QUOTATION MARK} she said.\n",
    )

    assert status == 1
    assert lines == [
        '1:1: quote: \N{LEFT DOUBLE QUOTATION MARK} → "',
        "1:4: quote: \N{RIGHT SINGLE QUOTATION MARK} → '",
        '1:12: quote: \N{RIGHT DOUBLE QUOTATION MARK} → "',
    ]


def test_clean_prose_passes_silently(tmp_path, capsys):
    status, lines, _ = lint(tmp_path, capsys, "Plain words. It's fine - mostly.\n")

    assert status == 0
    assert lines == []


def test_ai_vocabulary_is_flagged_in_any_case_and_inflection(tmp_path, capsys):
    status, lines, _ = lint(
        tmp_path, capsys, "We delved in. Additionally, it enhanced the work.\n"
    )

    assert status == 1
    assert flagged(lines) == [
        "1:4: ai-word: delved",
        "1:15: ai-word: Additionally",
        "1:32: ai-word: enhanced",
    ]


def test_a_finding_names_the_plain_replacement(tmp_path, capsys):
    _, lines, _ = lint(tmp_path, capsys, "Utilize it in order to win.\n")

    assert lines == [
        "1:1: plain-word: Utilize → use",
        "1:12: filler: in order to → to",
    ]


def test_fixed_phrases_from_each_group_are_flagged(tmp_path, capsys):
    text = (
        "It serves as a guide.\n"
        "A groundbreaking idea.\n"
        "Our north star is speed.\n"
        "Let me know if it breaks.\n"
        "In an evolving landscape we adapt.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == [
        "1:4: fancy-is: serves as",
        "2:3: promo: groundbreaking",
        "3:5: jargon: north star",
        "4:1: chatbot: Let me know if",
        "5:7: puffery: evolving landscape",
    ]


def test_words_that_are_literal_in_technical_text_pass(tmp_path, capsys):
    text = (
        "The steering vector, the eval harness and the API surface.\n"
        "A landscape photo on a tapestry.\n"
        "Names with a leading underscore, or underscores in paths.\n"
        "The model answers honestly. Endurance runs.\n"
    )
    status, lines, _ = lint(tmp_path, capsys, text)

    assert (status, lines) == (0, [])


def test_underscore_as_a_verb_is_flagged(tmp_path, capsys):
    _, lines, _ = lint(
        tmp_path, capsys, "This underscores the need. It was underscored twice.\n"
    )

    assert flagged(lines) == [
        "1:6: ai-word: underscores",
        "1:35: ai-word: underscored",
    ]


def test_honestly_is_flagged_only_opening_a_sentence(tmp_path, capsys):
    _, lines, _ = lint(
        tmp_path,
        capsys,
        "Honestly, it works. It ran. Truthfully, yes. Say it honestly.\n",
    )

    assert flagged(lines) == [
        "1:1: sincerity: Honestly,",
        "1:29: sincerity: Truthfully,",
    ]


def test_fenced_code_is_skipped_and_prose_after_it_checked(tmp_path, capsys):
    text = (
        "```python\n"
        "x = utilize()  # a \N{EM DASH} here\n"
        "```\n"
        "~~~~\n"
        "```\n"
        "leverage\n"
        "~~~~\n"
        "We utilize it.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == ["8:4: plain-word: utilize"]


def test_inline_code_urls_link_targets_and_html_comments_are_skipped(tmp_path, capsys):
    text = (
        "Run `a \N{EM DASH} b` or ``x ` leverage``.\n"
        "See https://example.com/delve and <https://x.org/utilize>.\n"
        "Read [the leverage guide](https://x.org/utilize-it) <!-- numerous -->.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == ["3:11: plain-word: leverage"]


def test_a_line_marked_ignore_is_skipped(tmp_path, capsys):
    text = (
        "He said \N{LEFT DOUBLE QUOTATION MARK}leverage\N{RIGHT DOUBLE QUOTATION MARK}."
        " <!-- slop-lint: ignore -->\n"
        "We leverage it.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == ["2:4: plain-word: leverage"]


COMMIT_MESSAGE = """Add the parser

We utilize a new parser.

Co-Authored-By: Leverage Bot <bot@example.com>
Claude-Session: https://claude.ai/code/session_x
# Please enter the commit message; numerous lines starting with '#' are ignored.
# ------------------------ >8 ------------------------
# Do not modify or remove the line above.
diff --git a/x b/x
+We utilize it.
"""


def test_commit_msg_mode_skips_comments_trailers_and_the_diff(tmp_path, capsys):
    status, lines, err = lint(tmp_path, capsys, COMMIT_MESSAGE, "--commit-msg")

    assert status == 1
    assert lines == ["3:4: plain-word: utilize → use"]
    assert err.rstrip().splitlines()[-1].startswith("COMMIT REFUSED: ")


def test_a_clean_commit_message_passes_silently(tmp_path, capsys):
    message = "Fix the parser\n\nIt read one line too few.\n"

    assert lint(tmp_path, capsys, message, "--commit-msg") == (0, [], "")


def test_an_unreadable_file_exits_2_and_the_others_are_still_checked(tmp_path, capsys):
    (tmp_path / "latin1.md").write_bytes("caf\xe9\n".encode("latin-1"))
    (tmp_path / "b.md").write_text("We utilize it.\n", encoding="utf-8")

    status = slop_lint.main(
        [str(tmp_path / name) for name in ("missing.md", "latin1.md", "b.md")]
    )

    out, err = capsys.readouterr()
    assert status == 2
    assert out == f"{tmp_path / 'b.md'}:1:4: plain-word: utilize → use\n"
    assert "missing.md" in err and "latin1.md" in err


def test_the_script_runs_from_anywhere_and_reads_stdin(tmp_path):
    done = subprocess.run(
        [str(SCRIPT), "-"],
        input="We utilize it.\n",
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert (done.returncode, done.stdout) == (1, "-:1:4: plain-word: utilize → use\n")


def test_the_commit_refusal_prints_after_the_findings(tmp_path):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Fix it\n\nWe utilize it in order to win.\n", encoding="utf-8")
    latin1 = tmp_path / "LATIN1_EDITMSG"
    latin1.write_bytes("Caf\xe9\n".encode("latin-1"))

    for path, status in ((message, 1), (latin1, 2)):
        done = subprocess.run(
            [str(SCRIPT), "--commit-msg", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        assert done.returncode == status
        assert done.stdout.splitlines()[-1].startswith("COMMIT REFUSED: ")
        assert len(done.stdout.splitlines()) == {1: 3, 2: 2}[status]


def test_fences_open_and_close_the_way_commonmark_does(tmp_path, capsys):
    text = (
        "```js``` is inline code, not a fence.\n"
        "We utilize it.\n"
        "````\n"
        "```\n"
        "leverage\n"
        "````\n"
        "```\n"
        "``` text after it, so not a closing fence\n"
        "leverage\n"
        "```\n"
        "1.  Install:\n"
        "\n"
        "    ```bash\n"
        "    pip install x  # utilize \N{EM DASH} cache\n"
        "    ```\n"
        "> ```\n"
        "> x = utilize()\n"
        "> ```\n"
        "We leverage it.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == ["2:4: plain-word: utilize", "19:4: plain-word: leverage"]


def commit_findings(tmp_path, capsys, message: str) -> list[str]:
    return flagged(lint(tmp_path, capsys, message, "--commit-msg").lines)


def test_a_subject_or_prose_shaped_like_a_trailer_is_checked(tmp_path, capsys):
    assert commit_findings(tmp_path, capsys, "feat: leverage the cache\n") == [
        "1:7: plain-word: leverage"
    ]
    assert commit_findings(tmp_path, capsys, "Follow-up: leverage the cache\n") == [
        "1:12: plain-word: leverage"
    ]
    assert commit_findings(
        tmp_path, capsys, "Fix it\n\nWhy: we utilize the cache.\nNote: numerous runs.\n"
    ) == ["3:9: plain-word: utilize", "4:7: plain-word: numerous"]


def test_trailers_mixed_into_prose_are_skipped_and_the_prose_checked(tmp_path, capsys):
    message = (
        "Fix it\n\nThis enhanced the parser.\nCo-Authored-By: Stunning Bot <b@x>\n"
    )

    assert commit_findings(tmp_path, capsys, message) == ["3:6: ai-word: enhanced"]


def test_honestly_opening_a_sentence_is_flagged_wherever_the_sentence_starts(
    tmp_path, capsys
):
    text = (
        "- Honestly, a list item.\n"
        "> Honestly, a quote.\n"
        "It ran.  Honestly, twice.\n"
        '"It ran." Honestly, after a quote.\n'
        "Say it honestly, then.\n"
        "Ask Honestly, our bot, about it.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == [
        "1:3: sincerity: Honestly,",
        "2:3: sincerity: Honestly,",
        "3:10: sincerity: Honestly,",
        "4:11: sincerity: Honestly,",
    ]


def test_underscore_the_noun_passes_and_the_verb_after_its_subject_is_flagged(
    tmp_path, capsys
):
    text = (
        "An underscore that marks it private, spaces into underscores the way"
        " Django does, a double underscore this time.\n"
        "This underscores the need, which underscores it.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == [
        "2:6: ai-word: underscores",
        "2:34: ai-word: underscores",
    ]


def test_code_and_comments_across_lines_urls_and_link_definitions_are_skipped(
    tmp_path, capsys
):
    text = (
        "Pass `--mode=fast\n"
        "leverage` to it.\n"
        "<!--\n"
        "numerous notes\n"
        "-->\n"
        "See https://example.com\N{EM DASH}a fine site.\n"
        "Go to www.example.com/utilize now.\n"
        "[guide]: ./docs/leverage.md\n"
        "A stray `tick\n"
        "\n"
        "We utilize it, and a ` here.\n"
    )
    _, lines, _ = lint(tmp_path, capsys, text)

    assert flagged(lines) == ["6:24: dash: \N{EM DASH}", "11:4: plain-word: utilize"]


def test_technical_uses_of_listed_words_pass(tmp_path, capsys):
    text = (
        "The page is served as text/html, as it stood as of 2024.\n"
        "The Python Enhancement Proposal index. Claude runs on Amazon Bedrock.\n"
    )
    status, lines, _ = lint(tmp_path, capsys, text)

    assert (status, lines) == (0, [])


def test_bedrock_as_jargon_is_flagged(tmp_path, capsys):
    _, lines, _ = lint(tmp_path, capsys, "Speed is our bedrock.\n")

    assert flagged(lines) == ["1:14: jargon: bedrock"]
