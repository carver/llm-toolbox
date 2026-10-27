"""Fitting the page cap: when a build runs past paper.toml's page_cap, what lands past it, mapped back to
paper.typ's lines, the blank space at the bottom of each page, and the short last lines of paragraphs.

The PDF's lines come from its text (pypdf), and each is found in paper.typ, which keeps one sentence per line:
a PDF line is a piece of one or two sentences, so the paper.typ line holding most of it, letters and digits
only, is where it comes from. A number the build filled in (`#n(...)`) isn't in paper.typ, so a line made
mostly of one is placed by the rest, or not at all."""

import io
import re
import statistics
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Typst, which the status block doesn't need
    from paperkit.typeset import Typeset

DRAFT_MARKS = (
    "DRAFT",
    "not for submission",
)  # lib.typ's watermark lines; its footer starts with both
INK = 160  # a pixel darker than this is ink; the watermark (88% grey) isn't
FOUND = 0.45  # the share of a PDF line a paper.typ line must hold to be its source
SHORT = 0.25  # a paragraph's last line under this share of the page's typical line is a widow
PLACED_ALONE = (
    12  # a PDF line with fewer letters than this is found with the line before it
)
AHEAD = 80  # paper.typ lines past the last one found to look in first
SHOWN = 60  # characters of each PDF line the report shows


@dataclass(frozen=True)
class Placed:
    """A line of the PDF, and the paper.typ line it comes from, when one can be found."""

    page: int
    text: str
    line: int | None
    lines: int = (
        1  # how many PDF lines in a row this stands for, all from that paper.typ line
    )


@dataclass(frozen=True)
class OverCap:
    body_pages: int
    cap: int
    past: list[
        Placed
    ]  # what lands past the cap, a run of lines from one paper.typ line at a time
    blank: list[
        tuple[int, float]
    ]  # each page before References, and the blank space at its bottom (pt)
    widows: list[Placed]  # short last lines of paragraphs


def letters(text: str) -> str:
    """`text`'s letters and digits, lowercased: what a PDF line and its paper.typ line share."""
    return re.sub(r"[\W_]+", "", text.lower())


class TypLines:
    """paper.typ's lines, for finding where a PDF line comes from. PDF lines come in reading order, so
    among the lines that hold one, the first at or after the last one found wins."""

    def __init__(self, typ: str) -> None:
        from paperkit.checks import code_lines

        self.lines = code_lines(typ)
        self.squeezed = [letters(line) for line in self.lines]
        paragraph, self.paragraphs = (
            0,
            [],
        )  # each line's paragraph: blank lines part them
        for line in self.lines:
            paragraph += not line.strip()
            self.paragraphs.append(paragraph)
        self.cursor = 0

    def find(self, text: str) -> int | None:
        """The paper.typ line (1-based) `text` comes from, or None."""
        wanted = letters(text)
        if not wanted:
            return None
        holding = [i for i, line in enumerate(self.squeezed) if wanted in line]
        ahead = range(self.cursor, min(self.cursor + AHEAD, len(self.lines)))
        holding = (
            holding
            or self.closest(wanted, ahead)
            or self.closest(wanted, range(len(self.lines)))
        )
        if not holding:
            return None
        found = next((i for i in holding if i >= self.cursor), holding[0])
        self.cursor = found
        return found + 1

    def find_only(self, text: str) -> int | None:
        """The paper.typ line (1-based) holding `text`, when it is the only one at or after the last line
        found; else None."""
        wanted = letters(text)
        holding = [
            i
            for i in range(self.cursor, len(self.lines))
            if wanted and wanted in self.squeezed[i]
        ]
        if len(holding) != 1:
            return None
        self.cursor = holding[0]
        return holding[0] + 1

    def closest(self, wanted: str, among: range) -> list[int]:
        """The lines among `among` sharing the longest run of letters with `wanted`, when that run is at
        least FOUND of it."""
        scores = []
        for i in among:
            line = self.squeezed[i]
            matcher = SequenceMatcher(None, wanted, line, autojunk=False)
            if matcher.real_quick_ratio() * (len(wanted) + len(line)) / 2 < FOUND * len(
                wanted
            ):
                continue  # too few letters in common for a long enough run
            size = matcher.find_longest_match(0, len(wanted), 0, len(line)).size
            if size >= FOUND * len(wanted):
                scores.append((size, i))
        best = max((size for size, _ in scores), default=0)
        return [i for size, i in scores if size == best]

    def is_prose(self, number: int) -> bool:
        """Whether the line is text, not a heading or a line of code (#show, #figure, ...)."""
        return not self.lines[number - 1].lstrip().startswith(("=", "#"))


def body_lines(text: str) -> list[str]:
    """A page's lines, without blank ones and the draft's marks."""
    return [
        line.strip()
        for line in text.split("\n")
        if line.strip()
        and line.strip() not in DRAFT_MARKS
        and not line.startswith("DRAFT, not for")
    ]


def placed(source: TypLines, page: int, lines: list[str]) -> list[Placed]:
    """Each line with its paper.typ line. A short one is found alone when one paper.typ line ahead holds it,
    else together with the line before it."""
    found = []
    for i, line in enumerate(lines):
        short = len(letters(line)) < PLACED_ALONE and i > 0
        number = source.find_only(line) if short else None
        context = f"{lines[i - 1]} {line}" if short else line
        found.append(Placed(page, line, number or source.find(context)))
    return found


def runs(lines: list[Placed]) -> list[Placed]:
    """Consecutive lines from one paper.typ line, as one."""
    merged: list[Placed] = []
    for line in lines:
        last = merged[-1] if merged else None
        if (
            last
            and line.line is not None
            and (last.page, last.line) == (line.page, line.line)
        ):
            merged[-1] = Placed(last.page, last.text, last.line, last.lines + 1)
        else:
            merged.append(line)
    return merged


def blank_at_bottom(png: bytes, height: float, bottom: float) -> float:
    """The blank space (pt) between the last ink on a page and its bottom margin."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("L")
    scale = image.height / height  # pixels per pt
    body = image.crop((0, 0, image.width, round((height - bottom) * scale)))
    ink = body.point(lambda value: 255 if value < INK else 0).getbbox()
    last = ink[3] if ink else 0
    return (body.height - last) / scale


def widows(source: TypLines, lines: list[Placed]) -> list[Placed]:
    """The short last lines of paragraphs: each a short PDF line that ends its paper.typ line, after a line
    from the same paragraph (with one sentence a line, a sentence ends a PDF line only where a paragraph
    does)."""
    if not lines:
        return []
    typical = statistics.median(len(line.text) for line in lines)
    found = []
    for before, line, after in zip(
        [None, *lines], lines, [*lines[1:], None], strict=False
    ):
        if (
            line.line is not None
            and before is not None
            and before.line is not None
            and len(line.text) < SHORT * typical
            and source.is_prose(line.line)
            and (after is None or after.line != line.line)
            and source.paragraphs[before.line - 1] == source.paragraphs[line.line - 1]
        ):
            found.append(line)
    return found


def over_cap(page: "Typeset", typ: str, cap: int | None) -> OverCap | None:
    """The over-cap report, or None when the paper is within `cap` (or there's none, or no References)."""
    from paperkit.checks import body_pages

    pages = body_pages(page)
    if cap is None or pages is None or pages <= cap or page.references is None:
        return None
    references = page.references[0]
    source = TypLines(typ)
    past: list[Placed] = []
    body: list[Placed] = []
    for number, text in enumerate(page.texts[:references], 1):
        lines = body_lines(text)
        if number == references:
            lines = (
                lines[: lines.index("References")] if "References" in lines else lines
            )
        found = placed(source, number, lines)
        body += found
        if number > cap:
            past += found
    blank = [
        (number, blank_at_bottom(png, page.height, page.bottom))
        for number, png in enumerate(page.pngs[: references - 1], 1)
    ]
    return OverCap(pages, cap, runs(past), blank, widows(source, body))


def quoted(line: Placed) -> str:
    text = line.text if len(line.text) <= SHOWN else line.text[: SHOWN - 1] + "…"
    more = f" (+{line.lines - 1} lines)" if line.lines > 1 else ""
    where = f"paper.typ:{line.line}" if line.line is not None else "paper.typ:?"
    return f'  page {line.page}  {where}  "{text}"{more}'


def report(over: OverCap) -> list[str]:
    """The over-cap report's lines, for the status block."""
    lines = [
        f"Past the page cap ({over.body_pages} pages before References, cap {over.cap}):"
    ]
    lines += [quoted(line) for line in over.past]
    lines += [
        "Blank space at the bottom of each page before References:",
        "  "
        + ", ".join(f"page {number} {space:.0f} pt" for number, space in over.blank),
    ]
    if over.widows:
        lines.append("Short last lines of paragraphs:")
        lines += [quoted(line) for line in over.widows]
    return lines
