"""Find the commits of a landing that only refine code an earlier commit of it added,
so they can be folded into that commit before wt-ship checks each one with the full
suite.

The batch is the commits from upstream's merge base with HEAD, oldest first. For each
commit C after the first, take the lines of C's parent that each hunk of C replaces or
deletes, and, for a hunk that only inserts, the lines either side of it. C is foldable
when at least THRESHOLD of those lines that aren't blank were last changed by an
earlier commit of the batch (git blame). Never judged: the first commit, a merge, a
fixup!/squash!/amend! commit, which folds already, and a commit whose message carries
the trailer `Fold-check: keep`, a split kept on purpose. A commit with no old line to
judge, as one that only adds files, isn't foldable.

C's target is the latest batch commit those lines blame to, and when that one is
foldable too, its own target, so every fold lands on a commit that stays. Folding C
moves it back past the commits between it and its target. `relies_on` lists each name
C's added lines use that a line added between the target and C defines (DEFINITION:
after def, class, fn, function, let, const and the like, or as `NAME =` or a shell
function's `NAME()` at a line's start), that the target's tree doesn't hold, and that
no commit folded into the same target before C adds: folding C would leave the target
using a name that doesn't exist there yet. A word only prose or a string mentions
defines nothing, so it isn't one.
"""

import os
import re
import tempfile
from typing import NamedTuple

from commitcheck import git

# The share of a commit's judged lines that must come from earlier commits of the batch.
THRESHOLD = 0.75
# A commit whose message has this trailer, with this value, is a split kept on purpose.
KEEP_TRAILER = "Fold-check"
KEEP_VALUE = "keep"
FIXES = ("fixup! ", "squash! ", "amend! ")

HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? ", re.MULTILINE)
BLAME_HEADER = re.compile(rb"^([0-9a-f]{40,64}) \d+ \d+")
WORD = re.compile(rb"\w{3,}")
SUBMODULE = b"160000"
DEFINITION = re.compile(
    rb"\b(?:def|class|fn|function|func|struct|enum|trait|interface|type|let|const"
    rb"|var|local|readonly|declare)\s+(?:mut\s+)?(\w{3,})"
    rb"|^\s*(?:export\s+)?(\w{3,})\s*(?:=(?!=)|\(\))"
)


class Fold(NamedTuple):
    sha: str
    subject: str
    share: float
    target: str
    # Words the commit uses that a commit between it and its target added: folding it
    # would leave the target using names that don't exist yet there.
    relies_on: tuple[str, ...]


class Commit(NamedTuple):
    sha: str
    parents: list[str]
    subject: str
    keep: bool


def foldable(upstream: str) -> list[Fold]:
    """The foldable commits of upstream's merge base with HEAD..HEAD, oldest first."""
    base = git("merge-base", upstream, "HEAD").stdout.strip()
    batch = batch_commits(base)
    order = {commit.sha: n for n, commit in enumerate(batch)}
    folds: dict[str, Fold] = {}
    # The names each target gains from the commits folded into it so far.
    gained: dict[str, set[bytes]] = {}
    for n, commit in enumerate(batch):
        if n == 0 or len(commit.parents) != 1 or commit.keep:
            continue
        if commit.subject.startswith(FIXES):
            continue
        parent = commit.parents[0]
        earlier = {c.sha for c in batch[:n]}
        blamed, added, defined = judge(base, parent, commit.sha)
        if not blamed:
            continue
        inside = [sha for sha in blamed if sha in earlier]
        share = len(inside) / len(blamed)
        if share < THRESHOLD:
            continue
        latest = max(set(inside), key=order.__getitem__)
        # A fold left in place, since it relies on a name, is a target that stays.
        stays = latest not in folds or folds[latest].relies_on
        target = latest if stays else folds[latest].target
        if batch[order[target]].subject.startswith(FIXES):
            continue
        folded_in = gained.setdefault(target, set())
        needed = (defined_between(target, parent) & added) - folded_in
        relies = sorted(os.fsdecode(name) for name in needed - words_in(target, needed))
        if not relies:
            folded_in |= defined
        folds[commit.sha] = Fold(
            commit.sha, commit.subject, share, target, tuple(relies)
        )
    return list(folds.values())


def describe(folds: list[Fold]) -> list[str]:
    """A line for each fold: the commit, its share and its target, and the names it
    relies on, if any."""
    lines = []
    for fold in folds:
        line = (
            f"  {fold.sha[:12]} {fold.subject}: {fold.share:.0%} of the lines it"
            f" changes came from {named(fold.target)}"
        )
        if fold.relies_on:
            line += (
                f"; it uses {', '.join(fold.relies_on)}, which a commit since then"
                " defines, so folding it would leave its target using a name it lacks"
            )
        lines.append(line)
    return lines


def named(sha: str) -> str:
    """A commit's short name and subject."""
    return git("log", "-1", "--format=%h %s", sha, check=False).stdout.strip()


def batch_commits(base: str) -> list[Commit]:
    trailer = f"%(trailers:key={KEEP_TRAILER},valueonly,separator=%x2C)"
    log = git(
        "log",
        "--reverse",
        f"--format=%x01%H%x00%P%x00%s%x00{trailer}",
        f"{base}..HEAD",
        text=False,
    ).stdout
    commits = []
    for record in log.split(b"\x01")[1:]:
        sha, parents, subject, keep = record.split(b"\x00")
        values = {value.strip().lower() for value in keep.decode().split(",")}
        commits.append(
            Commit(
                sha.decode(),
                parents.decode().split(),
                subject.decode(errors="replace"),
                KEEP_VALUE in values,
            )
        )
    return commits


def judge(base: str, parent: str, sha: str) -> tuple[list[str], set[bytes], set[bytes]]:
    """The commit each judged, non-blank line of the parent was last changed in (since
    base), and the words of the lines the commit adds and the names they define."""
    blamed: list[str] = []
    added: set[bytes] = set()
    defined: set[bytes] = set()
    for old, new in changed_files(parent, sha):
        diff = git(
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "-M",
            parent,
            sha,
            "--",
            old,
            new,
            text=False,
        ).stdout
        for line in diff.split(b"\n"):
            if line.startswith(b"+") and not line.startswith(b"+++"):
                added.update(WORD.findall(line[1:]))
                defined.update(definitions(line[1:]))
        lines = line_count(
            git("cat-file", "blob", f"{parent}:{old}", text=False).stdout
        )
        ranges = judged_ranges(diff, lines)
        if ranges:
            blamed += blame(base, parent, old, ranges)
    return blamed, added, defined


def changed_files(parent: str, sha: str) -> list[tuple[str, str]]:
    """(path in the parent, path in the commit) for each file the commit changes that
    the parent has as a file: an added file has no old lines to judge, and a
    submodule's entry no lines at all."""
    fields = git(
        "diff", "-z", "--raw", "-M", "--no-ext-diff", parent, sha, text=False
    ).stdout.split(b"\x00")
    files = []
    at = 0
    while at < len(fields) - 1:
        # :OLD_MODE NEW_MODE OLD_SHA NEW_SHA STATUS, then one path, or two for R and C.
        old_mode, new_mode, _, _, status = fields[at].lstrip(b":").split(b" ")
        kind = status[:1]
        if kind in (b"R", b"C"):
            old, new = fields[at + 1], fields[at + 2]
            at += 3
        else:
            old = new = fields[at + 1]
            at += 2
        if kind not in (b"A", b"C") and SUBMODULE not in (old_mode, new_mode):
            files.append((os.fsdecode(old), os.fsdecode(new)))
    return files


def line_count(blob: bytes) -> int:
    return blob.count(b"\n") + (1 if blob and not blob.endswith(b"\n") else 0)


def judged_ranges(diff: bytes, lines: int) -> list[tuple[int, int]]:
    """The parent's lines each hunk replaces, or, for an insertion, the lines either
    side of it, clamped to the file: blame refuses a whole call over one range past
    its end."""
    ranges = []
    for hunk in HUNK.finditer(diff):
        start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
        if count:
            ranges.append((start, start + count - 1))
        else:
            low, high = max(start, 1), min(start + 1, lines)
            if low <= high:
                ranges.append((low, high))
    return ranges


def blame(
    base: str, parent: str, path: str, ranges: list[tuple[int, int]]
) -> list[str]:
    """The commit of each non-blank line in ranges; a line from before base gets the
    boundary commit's name, which is never one of the batch."""
    spans = [arg for low, high in ranges for arg in ("-L", f"{low},{high}")]
    out = git(
        "-c",
        "blame.ignoreRevsFile=",
        "blame",
        "--porcelain",
        *spans,
        f"{base}..{parent}",
        "--",
        path,
        text=False,
    ).stdout
    shas = []
    current = None
    for line in out.split(b"\n"):
        if header := BLAME_HEADER.match(line):
            current = header.group(1).decode()
        elif line.startswith(b"\t") and line[1:].strip():
            shas.append(current)
    return shas


def defined_between(target: str, parent: str) -> set[bytes]:
    """The names the lines added from target to parent define."""
    if target == parent:
        return set()
    diff = git(
        "diff", "-U0", "--no-color", "--no-ext-diff", target, parent, text=False
    ).stdout
    names = set()
    for line in diff.split(b"\n"):
        if line.startswith(b"+") and not line.startswith(b"+++"):
            names |= definitions(line[1:])
    return names


def definitions(line: bytes) -> set[bytes]:
    return {match.group(1) or match.group(2) for match in DEFINITION.finditer(line)}


def words_in(rev: str, words: set[bytes]) -> set[bytes]:
    """Which of the words occur, as whole words, in rev's tree."""
    if not words:
        return set()
    with tempfile.NamedTemporaryFile("wb", suffix=".words") as patterns:
        patterns.write(b"\n".join(sorted(words)) + b"\n")
        patterns.flush()
        found = git(
            "grep",
            "-o",
            "-h",
            "-w",
            "-F",
            "-I",
            "--no-color",
            "-f",
            patterns.name,
            rev,
            check=False,
            text=False,
        ).stdout
    return set(found.split(b"\n")) & words
