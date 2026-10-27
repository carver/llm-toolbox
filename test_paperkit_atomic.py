"""Every output a build writes goes through a temporary file renamed into place (os.replace), so a
concurrent reader (a PDF viewer, `build-paper watch`, another build) never sees half of one."""

import os
import sys

from paperkit_testing import LIB, write_minimal

sys.path.insert(0, str(LIB))
from paperkit.build import build

PAPER = (
    '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\n'
    '#n("demo.calls") #figure(data-figure("demo-figure"), caption: [A.]) <fig>\nSee @fig.\n'
)


def demo(ledger) -> None:
    ledger.value("demo.calls", 3, how="calls")
    ledger.figure(
        "demo-figure",
        lambda ax, d: ax.bar(list(d), list(d.values())),
        {"a": 1},
        how="bars",
    )


def test_every_output_of_a_build_is_renamed_into_place(tmp_path, monkeypatch):
    replaced: set[str] = set()
    real = os.replace

    def recording(source, target, *args, **kwargs):
        replaced.add(os.path.relpath(target, tmp_path))
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording)
    paper_dir = write_minimal(tmp_path / "paper", PAPER)
    build(paper_dir, sources={"demo": demo})
    assert {
        "paper/build/numbers.json",
        "paper/build/numbers.md",
        "paper/build/numbers-built.json",
        "paper/build/changes.md",
        "paper/build/DRAFT-paper.pdf",
        "paper/build/pages/page-01.png",
        "paper/build/figures/demo-figure.svg",
        "paper/build/pages.json",
    } <= replaced
