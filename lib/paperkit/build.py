"""Builds the paper: runs the data sources into the ledger, compiles paper.typ, and checks the result against
the framework's rules and the ones paper.toml turns on (checks.py).

A draft goes to build/DRAFT-paper.pdf, with a watermark and a footer naming the commit on every page. A final
build checks more (every link resolves, nothing uncommitted, every log it read pushed) and writes the unmarked
<paper dir>/<final_name>.pdf and its ledger only when every check passes. Both write the ledger to
build/numbers.json (which paper.typ reads) and build/numbers.md, and, when asked (`pictures`), a PNG of each
page to build/pages/."""

import fcntl
import json
import os
import shutil
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from paperkit import figures
from paperkit.atomic import write_atomic
from paperkit.checks import (
    Problem,
    body_pages,
    broken_links,
    failed_claims,
    missing_sections,
    rules,
    swallowed_hashes,
    swallowed_semicolons,
    unclaimed_wording,
    unread_citations,
    unreferenced,
    unshipped,
)
from paperkit.config import Config
from paperkit.config import load as load_config
from paperkit.data import Source, registered
from paperkit.fit import OverCap, over_cap
from paperkit.git import Repo
from paperkit.ledger import DuplicateKey, Ledger, Provenance, display
from paperkit.lookup import PAGE_TEXTS
from paperkit.typeset import BuildError, Typeset, typeset

__all__ = [
    "DRAFT_NAME",
    "BuildError",
    "Problem",
    "Report",
    "build",
    "build_lock",
    "lock_holder",
    "stamp",
]

DRAFT_NAME = "DRAFT-paper"
# In build/: the problems of the last draft, and of the last final build, that compiled. One record each,
# since only a final build checks links and the tree
PROBLEMS_BUILT = {False: "problems-built.json", True: "problems-built-final.json"}


@dataclass
class Report:
    config: Config
    pdf: Path | None  # None when a final build fails: no unmarked PDF is written then
    page_count: int
    pages: list[Path]  # the picture of each page, when the build drew them (--pages)
    body_pages: int | None
    abstract: str
    abstract_words: int
    todos: int
    missing: list[str]
    failed_claims: list[str]
    unreferenced: list[str]
    missing_sections: list[str]
    size: int
    changed: list[str]  # the ledger's entries that changed since the last build
    problems: list[Problem] = field(default_factory=list)
    # A draft and a final build both print them
    warnings: list[str] = field(default_factory=list)
    over_cap: OverCap | None = None  # what to cut, when the paper runs past page_cap
    # The problems the last build that compiled didn't have; None when there was no such build
    new: list[Problem] | None = None


_reading: list[set[str]] = []  # the .jsonl files the running data source has opened


def _record_reads(event: str, args: tuple[Any, ...]) -> None:
    if event == "open" and _reading and isinstance(args[0], str | os.PathLike):
        path, mode = os.fspath(args[0]), args[1]
        if str(path).endswith(".jsonl") and (mode is None or "r" in str(mode)):
            _reading[-1].add(os.path.abspath(path))


# Audit hooks can't be removed; this one records only while a source runs
sys.addaudithook(_record_reads)


def fill(
    sources: Mapping[str, Source],
    out: Path,
    repo: Repo | None,
    config: Config,
) -> tuple[Ledger, list[Problem]]:
    """Runs each source into a ledger of its own and merges them, each entry stamped with its source and the
    logs (.jsonl files) it read. A source that fails, or claims a key another has, adds nothing and is a
    problem."""
    ledger, problems = Ledger(out, config.cell), []
    for name, function in sources.items():
        part = Ledger(out, config.cell)
        _reading.append(set())
        try:
            function(part)
        except Exception as error:  # noqa: BLE001  (a source can fail any way; its keys then show missing)
            problems.append(
                Problem("source", f"{name}: {type(error).__name__}: {error}")
            )
            continue
        finally:
            read = _reading.pop()
        opened = tuple(sorted(repo.relative(path) if repo else path for path in read))
        try:
            ledger.merge(
                part, Provenance(function.__module__, function.__qualname__, opened)
            )
        except DuplicateKey as error:
            problems.append(Problem("source", f"{name}: {error}"))
    return ledger, problems


def changes(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """What changed in the ledger, one line per key."""
    lines = [
        f"{key}: new, {display(after[key])}"
        for key in sorted(after.keys() - before.keys())
    ]
    lines += [f"{key}: gone" for key in sorted(before.keys() - after.keys())]
    for key in sorted(before.keys() & after.keys()):
        old, new = before[key], after[key]
        if {**old, "source": None} != {**new, "source": None}:
            shown = (display(old), display(new))
            lines.append(
                f"{key}: {shown[0]} → {shown[1]}"
                if shown[0] != shown[1]
                else f"{key}: changed"
            )
    return lines


def new_problems(record: Path, problems: list[Problem]) -> list[Problem] | None:
    """The problems the last build that compiled, recorded in `record`, didn't have (None with no record),
    and records these for the next build."""
    before = (
        {Problem(*p).identity() for p in json.loads(record.read_text())}
        if record.exists()
        else None
    )
    write_atomic(
        record, json.dumps([[p.check, p.detail] for p in problems], ensure_ascii=False)
    )
    if before is None:
        return None
    return [problem for problem in problems if problem.identity() not in before]


def markdown(entries: dict[str, dict]) -> str:
    lines = [
        "# The ledger",
        "",
        "Every key paper.typ can cite (paperkit's #n, #pct, #ci, #claim, #data-table, #data-figure and",
        "#data-text), from the last build. Generated by build-paper; don't edit.",
        "",
        "| key | kind | value | how | source |",
        "|---|---|---|---|---|",
    ]
    for key, entry in sorted(entries.items()):
        source = entry["source"]
        origin = f"{source['module']}.{source['function']}" + "".join(
            f" {log}" for log in source["logs"]
        )
        lines.append(
            f"| {key} | {entry['kind']} | {display(entry)} | {entry['how']} | {origin} |"
        )
    for key, entry in sorted(entries.items()):
        if entry["kind"] == "table":
            lines += [
                "",
                f"## {key}",
                "",
                _row(entry["header"]),
                "|---" * len(entry["header"]) + "|",
            ]
            lines += [_row(row) for row in entry["rows"]]
    return "\n".join(lines) + "\n"


def _row(cells: list[str]) -> str:
    """A markdown table row, with a | or a line break inside a cell escaped so it can't split the row."""
    return (
        "| "
        + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells)
        + " |"
    )


def stamp(repo: Repo | None) -> str:
    """The draft footer's line: the commit, and when the build ran."""
    built = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if repo is None:
        return f"not in git · built {built}"
    dirty = " with uncommitted changes" if repo.dirty() else ""
    return f"commit {repo.head()[:7]}{dirty} · built {built}"


def write_pages(folder: Path, page: Typeset, pictures: bool) -> list[Path]:
    """Each page's text in <folder>/../pages.json, for `build-paper find`, and with `pictures` a picture of
    each page in `folder`. Without them, the last pictures move to <folder>.old, kept but plainly stale."""
    write_atomic(folder.parent / PAGE_TEXTS, json.dumps(page.texts, ensure_ascii=False))
    if not pictures:
        if folder.exists():
            old = folder.with_name(folder.name + ".old")
            shutil.rmtree(old, ignore_errors=True)
            folder.rename(old)
        return []
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    pages = [folder / f"page-{n:02}.png" for n in range(1, len(page.pngs) + 1)]
    for path, png in zip(pages, page.pngs, strict=True):
        write_atomic(path, png)
    return pages


LOCK = ".lock"  # in build/: held by the build that is running, with its pid inside


@contextmanager
def build_lock(out: Path) -> Iterator[None]:
    """Holds <out>/.lock for a build, after waiting for any other build of the same paper to finish. The OS
    drops the lock when its process ends, so a crashed build leaves none behind."""
    out.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(out / LOCK, os.O_RDWR | os.O_CREAT, 0o644), "r+") as lock:
        if not _try_lock(lock):
            pid = _read_pid(lock)
            holder = f"pid {pid}" if pid else "pid unknown"
            print(
                f"build-paper: waiting for another build of this paper ({holder})",
                file=sys.stderr,
            )
            fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        yield


def lock_holder(out: Path) -> int | None:
    """The pid of the process holding <out>/.lock, or None when no build runs (or, for an instant, when the
    one that just took it hasn't written its pid yet)."""
    if not (out / LOCK).exists():
        return None
    with (out / LOCK).open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_pid(lock)
        fcntl.flock(lock, fcntl.LOCK_UN)
        return None


def _try_lock(lock: IO[str]) -> bool:
    """Takes the lock if it is free. Tries a few times over 50 ms, since lock_holder() takes it for an
    instant to see whether it is free."""
    for _ in range(5):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(0.01)
    return False


def _read_pid(lock: IO[str]) -> int | None:
    lock.seek(0)
    text = lock.read().strip()
    return int(text) if text.isdigit() else None


def build(
    paper_dir: Path,
    sources: Mapping[str, Source] | None = None,
    final: bool = False,
    pictures: bool = False,
) -> Report:
    """Builds <paper_dir>/paper.typ from `sources` (every data source in <paper_dir>/data/ by default), once
    no other build of it runs, under the rules <paper_dir>/paper.toml turns on. Raises BuildError when Typst
    can't compile it, and ConfigError for a paper.toml it can't read; every other problem is in the report."""
    config = load_config(paper_dir)
    with build_lock(paper_dir / "build"):
        return _build(paper_dir, sources, final, config, pictures)


def _build(
    paper_dir: Path,
    sources: Mapping[str, Source] | None,
    final: bool,
    config: Config,
    pictures: bool,
) -> Report:
    out, repo = paper_dir / "build", Repo.of(paper_dir)
    shutil.rmtree(out / "figures", ignore_errors=True)
    figures.use_fonts(paper_dir / "fonts", config.figure_font)
    sources = registered(paper_dir) if sources is None else sources
    ledger, problems = fill(sources, out / "figures", repo, config)
    # built: the last build that compiled
    numbers, built = out / "numbers.json", out / "numbers-built.json"
    out.mkdir(parents=True, exist_ok=True)
    # Every output through write_atomic: a watch, a viewer or another build may be reading it
    ledger_json = json.dumps(ledger.entries, indent=1, ensure_ascii=False) + "\n"
    write_atomic(numbers, ledger_json)
    write_atomic(out / "numbers.md", markdown(ledger.entries))

    inputs = {
        "draft": "false" if final else "true",
        "stamp": stamp(repo),
        "commit": repo.head() if repo else "",
    }
    page = typeset(paper_dir, inputs, config)
    changed = changes(
        json.loads(built.read_text()) if built.exists() else {}, ledger.entries
    )
    write_atomic(
        out / "changes.md",
        "# Changes since the last build\n\n" + "".join(f"- {c}\n" for c in changed),
    )
    write_atomic(built, ledger_json)
    paper_text = (paper_dir / "paper.typ").read_text()
    problems += (
        rules(page, config)
        + swallowed_semicolons(paper_text)
        + swallowed_hashes(paper_text)
    )
    if config.citations_read_check:
        citations = paper_dir / "citations.md"
        # No file: every citation is unread
        table = citations.read_text() if citations.exists() else ""
        bib = "".join(path.read_text() for path in sorted(paper_dir.glob("*.bib")))
        problems += unread_citations(page.cites, bib, table)
    if final:
        read = sorted(
            {
                log
                for entry in ledger.entries.values()
                for log in entry["source"]["logs"]
            }
        )
        problems += broken_links(page) + unshipped(repo, read, config)

    new = new_problems(out / PROBLEMS_BUILT[final], problems)
    pdf = None
    if not final:
        pdf = out / f"{DRAFT_NAME}.pdf"
    elif not problems:
        pdf = paper_dir / f"{config.final_name}.pdf"
        write_atomic(paper_dir / f"{config.final_name}-numbers.json", ledger_json)
    if pdf:
        write_atomic(pdf, page.pdf)
    return Report(
        config=config,
        pdf=pdf,
        page_count=len(page.texts),
        pages=write_pages(out / "pages", page, pictures),
        body_pages=body_pages(page),
        abstract=page.abstract,
        abstract_words=len(page.abstract.split()),
        todos=page.todos,
        missing=page.missing,
        failed_claims=failed_claims(page),
        unreferenced=unreferenced(page),
        missing_sections=missing_sections(page, config),
        size=len(page.pdf),
        changed=changed,
        problems=problems,
        warnings=unclaimed_wording(paper_text),
        over_cap=over_cap(page, paper_text, config.page_cap),
        new=new,
    )
