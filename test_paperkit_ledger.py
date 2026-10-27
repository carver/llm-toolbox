"""paperkit's ledger: each entry keeps its numbers and where they came from, a key can't be claimed twice,
only one cell's rate carries a Wilson interval, and a figure is drawn to an SVG whose text stays text."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from paperkit.ledger import DuplicateKey, Ledger, Provenance, wilson

CELL = ("model", "prompt_hash")


@pytest.mark.parametrize(
    "hits, total, low, high",
    [
        (5, 10, 0.2366, 0.7634),
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
    ],  # published Wilson 95% intervals
)
def test_wilson_matches_published_intervals(hits, total, low, high):
    assert wilson(hits, total) == pytest.approx((low, high), abs=1e-4)


def test_wilson_refuses_an_empty_cell():
    with pytest.raises(ValueError):
        wilson(0, 0)


def replies(model: str, hits: int, total: int, prompt_hash: str = "a1") -> list[dict]:
    return [
        {"model": model, "prompt_hash": prompt_hash, "cartel": i < hits}
        for i in range(total)
    ]


def cartel(row: dict) -> bool:
    return row["cartel"]


def test_a_rate_keeps_its_counts_interval_and_source(tmp_path):
    part = Ledger(tmp_path)
    part.rate(
        "pilot.same.cartel",
        replies("opus", 5, 10),
        cartel,
        how="cartel-share answers / replies",
    )
    ledger = Ledger(tmp_path)
    ledger.merge(
        part,
        Provenance("paper.data.pilot", "pilot", ("logs/pilot/probe-results.jsonl",)),
    )
    entry = ledger.entries["pilot.same.cartel"]
    assert entry["kind"] == "rate"
    assert (entry["k"], entry["n"], entry["rate"]) == (5, 10, 0.5)
    assert (entry["lo"], entry["hi"]) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert entry["how"] == "cartel-share answers / replies"
    assert entry["source"] == {
        "module": "paper.data.pilot",
        "function": "pilot",
        "logs": ["logs/pilot/probe-results.jsonl"],
    }


@pytest.mark.parametrize(
    "rows",
    [
        replies("opus", 1, 2) + replies("haiku", 1, 2),
        replies("opus", 1, 2) + replies("opus", 1, 2, "b2"),
    ],
    ids=["two models", "two prompts"],
)
def test_a_rate_over_more_than_one_cell_is_refused(tmp_path, rows):
    with pytest.raises(ValueError, match="pooled"):
        Ledger(tmp_path, CELL).rate("x", rows, cartel, how="")


def test_a_rate_records_its_cell(tmp_path):
    ledger = Ledger(tmp_path, CELL)
    ledger.rate("x", replies("opus", 1, 2), cartel, how="")
    assert ledger.entries["x"]["cell"] == {"model": "opus", "prompt_hash": "a1"}


def test_with_no_cell_fields_a_rate_takes_any_rows(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.rate("x", replies("opus", 1, 2) + replies("haiku", 2, 2), cartel, how="")
    assert (ledger.entries["x"]["k"], ledger.entries["x"]["n"]) == (3, 4)


def test_a_count_pooled_across_cells_has_no_interval(tmp_path):
    ledger = Ledger(tmp_path, CELL)
    ledger.pooled(
        "pilot.same.cartel",
        replies("opus", 2, 2) + replies("haiku", 1, 2),
        cartel,
        how="pooled",
    )
    entry = ledger.entries["pilot.same.cartel"]
    assert (entry["kind"], entry["k"], entry["n"], entry["rate"]) == (
        "pooled",
        3,
        4,
        0.75,
    )
    assert entry["cells"] == 2
    assert "lo" not in entry and "hi" not in entry


def test_a_rate_of_no_replies_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path).pooled("x", [], cartel, how="")


def test_a_key_claimed_twice_is_refused_even_across_kinds(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.value("pilot.calls", 1188, how="calls")
    with pytest.raises(DuplicateKey, match="pilot.calls"):
        ledger.text("pilot.calls", "again", how="")


def test_two_sources_cant_claim_one_key(tmp_path):
    ledger, first, second = Ledger(tmp_path), Ledger(tmp_path), Ledger(tmp_path)
    first.value("pilot.calls", 1188, how="calls")
    second.value("pilot.calls", 1190, how="calls")
    ledger.merge(first, Provenance("paper.data.pilot", "pilot"))
    with pytest.raises(DuplicateKey, match="paper.data.pilot"):
        ledger.merge(second, Provenance("paper.data.other", "other"))


def test_a_table_row_of_the_wrong_width_is_refused(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.table("t", ["arm", "rate"], [["same", "43/48"]], how="")
    assert ledger.entries["t"]["rows"] == [["same", "43/48"]]
    with pytest.raises(ValueError):
        ledger.table("u", ["arm", "rate"], [["same"]], how="")


def test_a_figure_is_drawn_to_an_svg_that_keeps_its_text(tmp_path):
    ledger = Ledger(tmp_path)

    def draw(ax, data):
        ax.bar(list(data), list(data.values()))
        ax.set_xlabel("arm label")

    ledger.figure(
        "pilot-ladder", draw, {"base": 1, "same": 43}, how="cartel answers per arm"
    )
    entry = ledger.entries["pilot-ladder"]
    svg = (tmp_path / "pilot-ladder.svg").read_text()
    assert entry["kind"] == "figure"
    assert entry["path"] == "pilot-ladder.svg"
    # Text, not glyph outlines, so Typst sets it in the paper's font
    assert ">arm label</text>" in svg


def pins(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if "==" in line and not line.startswith("#")
    }


def test_the_dev_requirements_pin_paperkit_s_dependencies_as_it_does():
    root = Path(__file__).resolve().parent
    assert pins(root / "lib" / "paperkit" / "requirements.txt") <= pins(
        root / "requirements-dev.txt"
    )
