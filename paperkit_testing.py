"""Helpers for paperkit's tests (lib/paperkit/): lib/ on the import path, and a small fixture paper."""

import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

# The smallest paper paperkit builds: the default template, an abstract and some text.
MINIMAL = '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\nText.\n'


def write_minimal(paper_dir: Path, text: str = MINIMAL) -> Path:
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "paper.typ").write_text(text)
    return paper_dir
