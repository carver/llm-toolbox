"""build-paper watch: recompiles the draft from the last build's ledger each time the paper's text changes,
and rebuilds it in full each time the repo's HEAD moves or the text cites a key the ledger lacks."""

import argparse
import contextlib
import json
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import typst

from paperkit.atomic import write_atomic
from paperkit.build import DRAFT_NAME, lock_holder, stamp
from paperkit.config import Config
from paperkit.config import load as load_config
from paperkit.git import Repo
from paperkit.typeset import paper_compiler

ABSENT = "not in the ledger"  # lib.typ's reason for a key the ledger has no entry under


@dataclass(frozen=True)
class LiveCompile:
    """One live compile: the keys the text cites and the ledger lacks, or Typst's error (no PDF then)."""

    missing: frozenset[str] = frozenset()
    error: str = ""


class FullBuild(Protocol):
    """Full draft builds in the background, one at a time."""

    def start(self) -> None: ...
    def running(self) -> bool: ...


class Watch:
    """One `poll()` per tick: starts full builds and runs live compiles as HEAD and the sources move."""

    def __init__(
        self,
        head: Callable[[], str],
        sources: Callable[[], Hashable],
        live: Callable[[], LiveCompile],
        full: FullBuild,
        others: Callable[[], int | None],
        now: Callable[[], float],
        settle: float,
        say: Callable[[str], None],
    ) -> None:
        self.head = head
        self.sources = sources
        self.live = live
        self.full = full
        self.others = others
        self.now = now
        self.settle = settle
        self.say = say
        self.commit = ""  # the HEAD the last full build was asked for
        self.wanted = False  # a full build to start once none runs
        self.building = False
        self.other: int | None = (
            None  # the pid of another process's build of the paper, while it runs
        )
        self.built_over: Hashable = (
            None  # the sources when the running full build started
        )
        # the sources the last live compile read; None: compile at the next poll
        self.compiled: Hashable = None
        self.retry = False  # a live compile failed during the running full build
        self.seen: Hashable = None  # the sources at the last poll
        self.edited = 0.0  # when the sources last changed
        self.missing: frozenset[str] = (
            frozenset()
        )  # the keys the last live compile found missing
        self.tried: set[str] = (
            set()
        )  # keys a full build ran over while the text cited them

    def poll(self) -> None:
        commit = self.head()
        if commit and commit != self.commit:
            self.commit, self.wanted = commit, True
            self.tried.clear()
        sources = self.sources()
        if sources != self.seen:
            self.seen, self.edited = sources, self.now()
        if self.building and not self.full.running():
            self.building = False
            self.build_ended(stale=self.retry or sources != self.built_over)
        other = self.others()
        if other is not None and self.other is None:
            self.say(f"paused: another build is running (pid {other})")
        elif other is None and self.other is not None:
            self.say("resumed")
            self.build_ended(
                stale=True
            )  # its PDF may hold text this watch never saw it build
        self.other = other
        if self.missing - self.tried and self.now() - self.edited >= self.settle:
            self.wanted = True
        if self.wanted and not self.building:
            self.wanted, self.building, self.built_over = False, True, sources
            self.tried |= self.missing  # this build tries them
            self.full.start()
        if sources != self.compiled:
            self.compile(sources)

    def build_ended(self, stale: bool) -> None:
        """A full build ended; `stale` when its PDF may show older text than the sources now hold."""
        if stale:
            self.compiled = None
        self.retry = False

    def compile(self, sources: Hashable) -> None:
        self.compiled = sources
        result = self.live()
        busy = self.building or self.other is not None
        if result.error and busy:
            self.retry = True  # the build is rewriting the ledger and figures
            return
        if result.error:
            self.say(
                f"live compile failed; the PDF keeps the last good one:\n{result.error}"
            )
            return
        if self.building:
            self.tried |= (
                result.missing
            )  # the running build tries every key the ledger can hold
        self.missing = result.missing
        shown = f": {', '.join(sorted(result.missing))}" if result.missing else ""
        self.say(f"live: recompiled, {len(result.missing)} missing{shown}")


def live_compile(paper_dir: Path, repo: Repo | None, config: Config) -> LiveCompile:
    """Compiles paper.typ against the ledger and figures the last full build left, and writes the draft."""
    inputs = {
        "draft": "true",
        "stamp": f"{stamp(repo)} · live",
        "commit": repo.head() if repo else "",
    }
    compiler = paper_compiler(paper_dir, inputs, config)
    try:
        pdf = compiler.compile(format="pdf")
        missing = json.loads(compiler.query("<missing>", format="json"))
    except typst.TypstError as error:
        return LiveCompile(error=error.diagnostic or error.message)
    if not isinstance(pdf, bytes):
        return LiveCompile(error="Typst returned no PDF")
    write_atomic(paper_dir / "build" / f"{DRAFT_NAME}.pdf", pdf)
    # Only a key the ledger lacks can come from a build: a #ci on a pooled count is marked too
    return LiveCompile(
        missing=frozenset(
            m["value"]["key"] for m in missing if m["value"]["why"] == ABSENT
        )
    )


class DraftBuilds:
    """Full draft builds, each in a fresh process, so a commit that changes the paper's code takes effect."""

    def __init__(self, repo: Repo, say: Callable[[str], None]) -> None:
        self.repo = repo
        self.say = say
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        subject = self.repo.git("log", "-1", "--format=%h %s").stdout.strip()
        self.say(f"full build at HEAD {subject}")
        self.process = subprocess.Popen(
            [sys.executable, "-P", "-m", "paperkit"], cwd=self.repo.root
        )

    def ours(self, pid: int) -> bool:
        """Whether `pid`, a lock holder's, is this watch's own build."""
        return self.process is not None and pid == self.process.pid

    def running(self) -> bool:
        if self.process is None or self.process.poll() is None:
            return self.process is not None
        if self.process.returncode:
            self.say(
                f"full build failed (exit {self.process.returncode}); still watching"
            )
        self.process = None
        return False


def source_stamps(paper_dir: Path) -> tuple[tuple[str, int], ...]:
    """The files a live compile reads outside build/, with when each last changed."""
    stamps = []
    for file in sorted([*paper_dir.glob("*.typ"), *paper_dir.glob("*.bib")]):
        with contextlib.suppress(
            FileNotFoundError
        ):  # a save can replace a file between glob and stat
            stamps.append((file.name, file.stat().st_mtime_ns))
    return tuple(stamps)


def say(line: str) -> None:
    print(f"=== {time.strftime('%H:%M:%S')} {line}", flush=True)


def main(argv: list[str], paper_dir: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper watch",
        description="Build the draft now, and keep paper/build/DRAFT-paper.pdf up to date. Each save of "
        "paper.typ (or another .typ or .bib in paper/) recompiles it within a second or two, from the "
        "numbers and figures of the last full build. A full build runs again each time the repo's HEAD "
        "moves (a commit, checkout, merge or rebase), and when the text cites a key the ledger lacks, once "
        "the text has been still for --settle seconds. Open the PDF in a viewer that reloads a changed "
        "file (Skim, VS Code, Zathura; macOS Preview doesn't). Ctrl-C stops it.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="how often to look (default 1)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how long the text must be still before a missing key brings a full build (default 5)",
    )
    args = parser.parse_args(argv)
    repo = Repo.of(paper_dir)
    if repo is None:
        print(
            f"build-paper watch: {paper_dir} is not in a git repository",
            file=sys.stderr,
        )
        return 1
    config = load_config(paper_dir)
    builds = DraftBuilds(repo, say)

    def others() -> int | None:
        holder = lock_holder(paper_dir / "build")
        return None if holder is None or builds.ours(holder) else holder

    watch = Watch(
        head=repo.head,
        sources=lambda: source_stamps(paper_dir),
        live=lambda: live_compile(paper_dir, repo, config),
        full=builds,
        others=others,
        now=time.monotonic,
        settle=args.settle,
        say=say,
    )
    try:
        while True:
            # A watcher left running for hours outlives a full disk or a Typst panic
            try:
                watch.poll()
            except Exception:  # noqa: BLE001
                say(f"watch hit an error; still watching:\n{traceback.format_exc()}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
