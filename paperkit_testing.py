"""Helpers for paperkit's tests (lib/paperkit/): lib/ on the import path, a small fixture paper, and a
throwaway project (a git repo with paper/paper.typ, paper/paper.toml and one data source in paper/data/)
to run bin/build-paper in."""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import without_repo_vars

REPO = Path(__file__).resolve().parent
LIB = REPO / "lib"
BUILD_PAPER = REPO / "bin" / "build-paper"
sys.path.insert(0, str(LIB))

# The smallest paper paperkit builds: the default template, an abstract and some text.
MINIMAL = '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\nText.\n'

BIB = """@article{calvano2020,
  title = {Artificial Intelligence, Algorithmic Pricing, and Collusion},
  author = {Calvano, Emilio and Calzolari, Giacomo and Denicol{\\`o}, Vincenzo and Pastorello, Sergio},
  journal = {American Economic Review}, year = {2020}, volume = {110}, number = {10}, pages = {3267--3297},
}
"""

CITATIONS = """| key | source | read |
|---|---|---|
| `calvano2020` | Calvano et al. (2020) | {read} |
"""

# The one data source: Opus's rows are one cell, and Haiku's two more make a pooled count. It reads
# logs/demo.jsonl from the project's root.
DEMO_SOURCE = """import json
from pathlib import Path

from paperkit import Ledger, source

LOG = Path(__file__).resolve().parents[2] / "logs" / "demo.jsonl"


@source("demo")
def demo(ledger: Ledger) -> None:
    rows = [json.loads(line) for line in LOG.read_text().splitlines()]
    opus = [row for row in rows if row["model"] == "opus"]
    hits = sum(row["cartel"] for row in opus)
    ledger.rate("demo.same", opus, lambda row: row["cartel"], how="cartel answers / replies")
    ledger.pooled("demo.pooled", rows, lambda row: row["cartel"], how="both models")
    ledger.table("demo-table", ["arm", "cartel"], [["same", f"{hits}/{len(opus)}"]], how="per arm")
    ledger.figure(
        "demo-figure", lambda ax, d: ax.bar(list(d), list(d.values())), {"same": hits}, how="bars"
    )
"""

# A source that always fails.
BROKEN_SOURCE = """from paperkit import Ledger, source


@source("broken")
def broken(ledger: Ledger) -> None:
    raise RuntimeError("its log is missing")
"""

# Every optional rule on.
STRICT = """page_cap = 8
abstract_words = [150, 250]
required_sections = ["Code and Data", "LLM Usage Statement"]
after_references = ["Limitations"]
citations_read_check = true
logs_dir = "logs"
final_name = "submission"
max_pdf_mb = 25
cell = ["model", "prompt_hash"]
"""


def write_minimal(paper_dir: Path, text: str = MINIMAL) -> Path:
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "paper.typ").write_text(text)
    return paper_dir


def write_log(path: Path, hits: int = 5, replies: int = 10) -> Path:
    """Opus's replies, `hits` of them at the cartel share, then two of Haiku's."""
    rows = [
        {"model": "opus", "prompt_hash": "a1", "cartel": i < hits}
        for i in range(replies)
    ]
    rows += [{"model": "haiku", "prompt_hash": "a1", "cartel": True}] * 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(
        command,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=without_repo_vars(os.environ),
    ).stdout


def make_project(
    root: Path,
    text: str,
    toml: str | None = STRICT,
    url: str = "",
    read: str = "full text",
    origin: bool = True,
    broken: bool = False,
) -> Path:
    """A git repo at `root` holding the paper `text` (with {url} filled in), its paper.toml, the demo
    source (and the broken one) and its log, committed, and pushed to a bare origin beside it."""
    paper = root / "paper"
    (paper / "data").mkdir(parents=True)
    (paper / "paper.typ").write_text(text.format(url=url))
    if toml is not None:
        (paper / "paper.toml").write_text(toml)
    (paper / "data" / "demo.py").write_text(DEMO_SOURCE)
    if broken:
        (paper / "data" / "broken.py").write_text(BROKEN_SOURCE)
    (paper / "refs.bib").write_text(BIB)
    (paper / "citations.md").write_text(CITATIONS.format(read=read))
    write_log(root / "logs" / "demo.jsonl")
    (root / ".gitignore").write_text("paper/build/\n__pycache__/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "paper")
    if origin:
        bare = root.parent / f"{root.name}-origin.git"
        git(root.parent, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
    return root


def build_paper(
    project: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """bin/build-paper ARGS, run in `project`, as no agent unless `env` says so."""
    base = {k: v for k, v in without_repo_vars(os.environ).items() if k != "AI_AGENT"}
    return subprocess.run(
        [str(BUILD_PAPER), *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env=base | (env or {}),
    )


def pdf_text(path: Path) -> str:
    """The PDF's text, its whitespace collapsed."""
    from pypdf import PdfReader

    return " ".join(
        " ".join(page.extract_text() for page in PdfReader(path).pages).split()
    )
