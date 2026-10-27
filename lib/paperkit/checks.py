"""The rules a paper is checked against, on the typeset paper: the framework's own, always on (every key in the
ledger, every claim holding, no TODO, every figure referenced and drawn under its own section), and the ones
paper.toml turns on (config.py). Checks of paper.typ's text (a semicolon or a `#` before a digit that Typst
swallows, and wording with no #claim behind it, which only warns), and the extra checks of a final build: every
link resolves, and the paper and its logs are committed and pushed."""

import http.client
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from paperkit.config import Config
from paperkit.git import Repo
from paperkit.typeset import Typeset

DOI = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
)
USER_AGENT = "Mozilla/5.0 (paper link check; paperkit)"
EMBEDDED = re.compile(
    r"#[A-Za-z_][\w-]*"
)  # the start of an embedded expression: #n, #todo, #sym
FIELD = re.compile(r"\.[A-Za-z_][\w-]*")  # a field or method after it: .dash, .len
CLOSING = {"(": ")", "[": "]"}
CITES = re.compile(r"#(?:n|pct|ci)\(")
STRING = re.compile(
    r'"[^"\n]*"'
)  # a key, or quoted words, whose words aren't the sentence's own
RAW = re.compile(r"`[^`\n]*`")  # raw text, printed as written
HASH_DIGITS = re.compile(r"(?<!\\)#(\d+)")
BIB_KEY = re.compile(
    r"^\s*@\w+\s*\{\s*([^,\s]+)\s*,", re.MULTILINE
)  # a refs.bib entry's key
READ_IN_FULL = (
    "full text"  # citations.md's read column for a source someone has read in full
)
NOT_READ_IN_FULL = {  # the column's other values, as a problem words them
    "abstract": "only its abstract was read",
    "catalogue": "only its catalogue was read",
    "secondhand": "it was read only secondhand",
    "not checked": "no one has checked it",
}
WORDING = re.compile(  # words whose truth rests on the numbers beside them, not inside a hyphenated word
    r"(?<![\w-])(?:every|all|none|no one|nobody|neither|never|always|only|most|mostly|nearly|almost"
    r"|fell|falls|rose|rises|raises|raised|lowers|lowered|higher|lower|more often|less often|fewer"
    r"|strongest|weakest|highest|lowest|largest|smallest)(?![\w-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Problem:
    check: str
    detail: str


def body_pages(page: Typeset) -> int | None:
    """Pages before References: its page counts unless the heading starts it (sits within the top margin's
    height of the page's top, plus a point)."""
    if page.references is None:
        return None
    number, y = page.references
    return number - 1 if y <= page.top + 1 else number


def failed_claims(page: Typeset) -> list[str]:
    return [
        text if ok is False else f"{text} (can't be checked: a key isn't in the ledger)"
        for ok, text in page.claims
        if ok is not True
    ]


def unreferenced(page: Typeset) -> list[str]:
    return [figure for figure in page.figures if figure not in page.refs]


def missing_sections(page: Typeset, config: Config) -> list[str]:
    required = (*config.required_sections, *config.after_references)
    return [section for section in required if section not in page.headings]


def code_lines(text: str) -> list[str]:
    """paper.typ's lines, each cut at a `//` comment outside a string, so a comment counts for nothing."""
    lines = []
    for line in text.split("\n"):
        quoted = False
        for i, char in enumerate(line):
            if char == '"' and line[i - 1 : i] != "\\":
                quoted = not quoted
            elif not quoted and line.startswith("//", i):
                line = line[:i]
                break
        lines.append(line)
    return lines


def _past_group(line: str, start: int) -> int | None:
    """The index just past the bracketed group that opens at `start`, ( or [, skipping strings; None when it
    doesn't close on the line."""
    expected, i, quoted = [CLOSING[line[start]]], start + 1, False
    while i < len(line) and expected:
        char = line[i]
        if quoted:
            i += char == "\\"  # skip the escaped character
            quoted = char != '"'
        elif char == '"':
            quoted = True
        elif char in CLOSING:
            expected.append(CLOSING[char])
        elif char == expected[-1]:
            expected.pop()
        i += 1
    return None if expected else i


def _past_embedded(line: str, start: re.Match[str]) -> int | None:
    """The index just past the embedded expression whose #name is `start`, with its fields, argument lists
    and content blocks; None when a group doesn't close on the line."""
    end = start.end()
    while end < len(line):
        if field := FIELD.match(line, end):
            end = field.end()
        elif line[end] in CLOSING:
            if (end := _past_group(line, end)) is None:
                return None
        else:
            break
    return end


def swallowed_semicolons(text: str) -> list[Problem]:
    """Each embedded expression followed straight by a semicolon, outside comments: Typst ends the expression
    at the semicolon and drops it (`#n("key");`, `#todo[...];`, `#sym.dash;`), so the sentence loses its
    punctuation with no error."""
    found = []
    for number, line in enumerate(code_lines(text), 1):
        for match in EMBEDDED.finditer(line):
            end = _past_embedded(line, match)
            if end is not None and line[end : end + 1] == ";":
                expression = line[match.start() : end + 1]
                found.append(
                    Problem(
                        "semicolon",
                        f'line {number}: {expression} drops its ";" (write \\; to keep it)',
                    )
                )
    return found


def swallowed_hashes(text: str) -> list[Problem]:
    """Each `#` before a digit, outside comments, strings and raw text: Typst reads `#39` as the number 39
    and prints it without the `#`, so "(#39)" becomes "(39)" with no error."""
    found = []
    for number, line in enumerate(code_lines(text), 1):
        for match in HASH_DIGITS.finditer(RAW.sub("", STRING.sub("", line))):
            digits = match.group(1)
            found.append(
                Problem(
                    "hash",
                    f"line {number}: #{digits} renders as {digits} (write \\#{digits})",
                )
            )
    return found


def citation_reads(table: str) -> dict[str, str | None] | None:
    """citations.md's `read` column, by key: how much of each source someone has read, or None for a row
    without the table's columns. None when no table has key and read columns."""
    header: list[str] | None = None
    reads: dict[str, str | None] = {}
    for line in table.splitlines():
        if not line.startswith("|"):
            if reads:
                break  # past the table
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells if {"key", "read"} <= set(cells) else None
        elif not set("".join(cells)) <= set("-: "):  # not the |---| rule
            key = (
                cells[header.index("key")].strip("`")
                if len(cells) > header.index("key")
                else ""
            )
            reads[key] = (
                cells[header.index("read")] if len(cells) == len(header) else None
            )
    return reads if header is not None else None


def unread_citations(cited: set[str], bib: str, table: str) -> list[Problem]:
    """Each source the paper cites that citations.md doesn't say someone read in full; keys in `cited` that
    aren't refs.bib keys aren't checked."""
    keys = sorted(cited & set(BIB_KEY.findall(bib)))
    reads = citation_reads(table)
    if reads is None:
        return (
            [Problem("citation", "citations.md has no table with key and read columns")]
            if keys
            else []
        )
    found = []
    for key in keys:
        if key not in reads:
            why = "citations.md has no row for it"
        elif (read := reads[key]) is None:
            why = "its row in citations.md doesn't have the table's columns"
        elif read == READ_IN_FULL:
            continue
        elif read in NOT_READ_IN_FULL:
            why = f"citations.md says {NOT_READ_IN_FULL[read]}"
        else:
            why = f"its read column in citations.md is {read!r}, not full text"
        found.append(Problem("citation", f"@{key} is cited, but {why}"))
    return found


def _claimed_later(lines: list[str], i: int) -> bool:
    """Whether a #claim follows line i in its paragraph, before the next blank line."""
    for later in lines[i + 1 :]:
        if not later.strip():
            return False
        if later.lstrip().startswith("#claim("):
            return True
    return False


def unclaimed_wording(text: str) -> list[str]:
    """Each sentence (a line, in paper.typ's one sentence per line) that cites the ledger and holds a word
    whose truth rests on the numbers, with no #claim later in its paragraph. A warning, since many such words
    are fine where they stand."""
    lines = code_lines(text)
    found = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#claim") or not CITES.search(line):
            continue
        words = sorted(
            {m.group(0).lower() for m in WORDING.finditer(STRING.sub("", line))}
        )
        if words and not _claimed_later(lines, i):
            found.append(f"line {i + 1}: {', '.join(words)}, with no #claim after it")
    return found


def rules(page: Typeset, config: Config) -> list[Problem]:
    """Every rule the paper breaks. A draft prints them; a final build refuses them."""
    problems = [Problem("typst", warning) for warning in page.warnings]
    problems += [Problem("missing", missing) for missing in page.missing]
    problems += [Problem("claim", claim) for claim in failed_claims(page)]
    if page.todos:
        problems.append(Problem("todo", f"{page.todos} TODO markers"))
    problems += [Problem("unreferenced", figure) for figure in unreferenced(page)]
    problems += [
        Problem(
            "placement", f"{figure} is written under {written} but drawn under {drawn}"
        )
        for figure, written, drawn in page.misplaced
    ]
    problems += [
        Problem("sections", f"no {section} section")
        for section in missing_sections(page, config)
    ]
    headings = page.headings
    for section in config.after_references:
        if {section, "References"} <= set(headings) and headings.index(
            section
        ) < headings.index("References"):
            problems.append(
                Problem(
                    "sections",
                    f"{section} comes before References; it must be an appendix",
                )
            )
    pages = body_pages(page)
    if config.page_cap is not None and pages is None:
        problems.append(
            Problem("page-cap", "no References heading, so the pages can't be counted")
        )
    elif config.page_cap is not None and pages is not None and pages > config.page_cap:
        problems.append(
            Problem(
                "page-cap", f"{pages} pages before References (cap {config.page_cap})"
            )
        )
    if config.abstract_words is not None:
        words, (low, high) = len(page.abstract.split()), config.abstract_words
        if not low <= words <= high:
            problems.append(Problem("abstract", f"{words} words (needs {low}-{high})"))
    if config.max_pdf_mb is not None and len(page.pdf) > config.max_pdf_mb * 1e6:
        problems.append(
            Problem(
                "size", f"{len(page.pdf) / 1e6:.1f} MB (cap {config.max_pdf_mb:g} MB)"
            )
        )
    return problems


def probe_url(url: str) -> str:
    """The URL to request to learn whether `url` resolves. A DOI goes to doi.org's handle API, which answers
    404 for a DOI it doesn't know, rather than to the publisher, whose site often refuses scripts."""
    doi = next(
        (url.removeprefix(prefix) for prefix in DOI if url.startswith(prefix)), None
    )
    return url if doi is None else f"https://doi.org/api/handles/{doi}"


def unreachable(url: str) -> str | None:
    """Why the URL doesn't resolve, or None when it does."""
    request = urllib.request.Request(probe_url(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20):
            return None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (ValueError, http.client.InvalidURL) as error:
        return f"invalid URL ({error})"
    except (urllib.error.URLError, OSError) as error:
        return f"unreachable ({getattr(error, 'reason', error)})"


def broken_links(page: Typeset) -> list[Problem]:
    """Every web link that doesn't resolve. A network failure counts: the link wasn't shown to work."""
    urls = sorted(
        {link for link in page.links if link.startswith(("http://", "https://"))}
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        reasons = pool.map(unreachable, urls)
    return [
        Problem("link", f"{url}: {why}")
        for url, why in zip(urls, reasons, strict=True)
        if why
    ]


def unshipped(repo: Repo | None, logs: list[str], config: Config) -> list[Problem]:
    """What keeps the build from matching a commit anyone can check out: uncommitted changes, and, when
    paper.toml names a logs_dir, the logs under it the numbers came from that aren't committed or pushed to
    its upstream."""
    if repo is None:
        return [Problem("git", "the paper isn't in a git repository")]
    folder = config.logs_dir.rstrip("/") + "/" if config.logs_dir else None
    checked = [log for log in logs if folder and log.startswith(folder)]
    unpushed = repo.unpushed(checked, config.upstream) if checked else []
    return [
        *(Problem("dirty", path) for path in repo.dirty()),
        *(Problem("unpushed", log) for log in unpushed),
    ]
