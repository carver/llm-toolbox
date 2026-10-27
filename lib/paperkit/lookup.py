"""build-paper find and keys: answers about the last build, read from its outputs, without building."""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from paperkit.ledger import display

# In build/: each page's text, from the last build that compiled
PAGE_TEXTS = "pages.json"


def squeezed(text: str) -> str:
    """`text` lowercased with all its whitespace gone, so a phrase matches across the PDF's line breaks."""
    return "".join(text.lower().split())


def pages_holding(texts: list[str], text: str) -> list[int]:
    """The numbers of the pages whose text holds `text`, ignoring case and whitespace. A word the PDF
    hyphenates at a line's end matches too."""
    wanted = squeezed(text)
    found = []
    for number, page in enumerate(texts, 1):
        whole = squeezed(page)
        joined = squeezed("".join(line.removesuffix("-") for line in page.split("\n")))
        if wanted in whole or wanted in joined:
            found.append(number)
    return found


def shown(path: Path) -> str:
    """`path` from the working directory when it is under it."""
    return (
        str(path.relative_to(Path.cwd()))
        if path.is_relative_to(Path.cwd())
        else str(path)
    )


def find_main(argv: list[str], paper_dir: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper find",
        description="List each page of the last build whose text holds TEXT, ignoring case and whitespace "
        "(so a phrase matches across line breaks): its number, and the PNG of it in paper/build/pages/.",
    )
    parser.add_argument("text", metavar="TEXT")
    args = parser.parse_args(argv)
    texts_file = paper_dir / "build" / PAGE_TEXTS
    if not texts_file.exists():
        print("build-paper find: no build yet; run build-paper first", file=sys.stderr)
        return 1
    numbers = pages_holding(json.loads(texts_file.read_text()), args.text)
    if not numbers:
        print(f"build-paper find: no page holds {args.text!r}", file=sys.stderr)
        return 1
    for number in numbers:
        print(
            f"page {number}  {shown(paper_dir / 'build' / 'pages' / f'page-{number:02}.png')}"
        )
    return 0


def percent(x: float) -> str:
    """A share as a whole percent, rounded half up, as lib.typ shows it."""
    return f"{math.floor(x * 100 + 0.5)}%"


def rate(entry: dict[str, Any]) -> str:
    """An entry's rate as the paper shows it: with its interval for one cell's, none for anything else."""
    match entry["kind"]:
        case "rate":
            low, high = percent(entry["lo"]).rstrip("%"), percent(entry["hi"])
            return f"{percent(entry['rate'])} ({low}–{high})"
        case "pooled":
            return percent(entry["rate"])
        case _:
            return ""


def how(entry: dict[str, Any]) -> str:
    source = entry.get("source", {})
    origin = f"{source.get('module')}.{source.get('function')}" if source else "unknown"
    logs = "".join(f" {log}" for log in source.get("logs", []))
    return f"how: {entry['how']}; from {origin}{logs}"


def keys_main(argv: list[str], paper_dir: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper keys",
        description="Print each ledger key of the last build that starts with a PREFIX, one line each: the "
        "key, its value (a count, a value, a table's rows) and, for a count, its rate as the paper shows it "
        "(with its 95%% interval for one cell's rate). From paper/build/numbers.json.",
    )
    parser.add_argument("prefixes", nargs="+", metavar="PREFIX")
    parser.add_argument(
        "--how",
        action="store_true",
        help="add how each was computed, its source and the logs it read",
    )
    args = parser.parse_args(argv)
    numbers = paper_dir / "build" / "numbers.json"
    if not numbers.exists():
        print("build-paper keys: no build yet; run build-paper first", file=sys.stderr)
        return 1
    entries: dict[str, dict[str, Any]] = json.loads(numbers.read_text())
    keys = sorted(key for key in entries if key.startswith(tuple(args.prefixes)))
    if not keys:
        print(
            f"build-paper keys: no key starts with {' or '.join(args.prefixes)}",
            file=sys.stderr,
        )
        return 1
    rows = [[key, display(entries[key]), rate(entries[key])] for key in keys]
    if args.how:
        for row, key in zip(rows, keys, strict=True):
            row.append(how(entries[key]))
    # Aligned columns; one empty in every row (no key has a rate) is left out
    columns = [c for c in range(len(rows[0])) if any(row[c] for row in rows)]
    widths = {c: max(len(row[c]) for row in rows) for c in columns[:-1]}
    for row in rows:
        print("  ".join(row[c].ljust(widths.get(c, 0)) for c in columns).rstrip())
    return 0
