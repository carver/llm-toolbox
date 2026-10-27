"""The ledger: every number, table, figure and text the paper cites, under its key, with where it came from.

The data sources in paper/data/ fill it; the build writes it to paper/build/numbers.json, which paperkit's
lib.typ reads, so no number in the paper is typed by hand. Keys lead with the experiment id (`exp1.arm.hits`).

A rate with an interval is one cell's: rows that are independent draws from one distribution. When paper.toml
names the `cell` fields (`cell = ["model", "prompt_hash"]`), `rate` refuses rows that differ in any of them. A
count over several cells is `pooled` and has no interval: its rows come from different distributions.

Each entry is a JSON object with its `kind` (rate, pooled, value, table, figure or text), its fields, `how` it
was computed in a few words, and, once merged into the paper's ledger, its `source`: the data source's module
and function and the log files it read."""

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from paperkit import figures


class DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class Provenance:
    """Where an entry came from: the data source's module and function, and the logs it read."""

    module: str
    function: str
    logs: tuple[str, ...] = ()


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for hits/total (95% by default)."""
    if total <= 0:
        raise ValueError(f"no replies to take a rate of ({hits}/{total})")
    p = hits / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = (
        z
        / (1 + z * z / total)
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    )
    return max(0.0, centre - half), min(1.0, centre + half)


def display(entry: dict[str, Any]) -> str:
    """An entry in a few characters, for the changes a build prints and numbers.md."""
    match entry["kind"]:
        case "rate" | "pooled":
            return f"{entry['k']}/{entry['n']}"
        case "value":
            return str(entry["value"])
        case "table":
            return f"{len(entry['rows'])} rows"
        case "figure":
            return entry["path"]
        case _:
            return f"{len(entry['text'])} characters"


class Ledger:
    def __init__(self, figures: Path, cell: tuple[str, ...] = ()):
        self.entries: dict[str, dict[str, Any]] = {}
        self.figures = figures  # where figure() draws its SVGs
        self.cell = cell  # the fields one cell's rows share (paper.toml's `cell`)

    def rate(
        self, key: str, rows: list[dict], hit: Callable[[dict], bool], how: str
    ) -> None:
        """One cell's rate: how many of `rows` are a `hit`, with its Wilson interval. The rows must share
        every `cell` field; count rows across cells with `pooled`."""
        cells = {tuple(row[field] for field in self.cell) for row in rows}
        if len(cells) > 1:
            raise ValueError(
                f"{key}: its rows span {len(cells)} cells ({', '.join(self.cell)}); count them with pooled()"
            )
        hits = sum(map(hit, rows))
        low, high = wilson(hits, len(rows))
        cell = dict(zip(self.cell, cells.pop(), strict=True))
        self._add(
            key,
            "rate",
            how,
            k=hits,
            n=len(rows),
            rate=hits / len(rows),
            lo=low,
            hi=high,
            cell=cell,
        )

    def pooled(
        self, key: str, rows: list[dict], hit: Callable[[dict], bool], how: str
    ) -> None:
        """A count across cells, with no interval."""
        if not rows:
            raise ValueError(f"{key}: no rows to count")
        hits = sum(map(hit, rows))
        cells = len({tuple(row[field] for field in self.cell) for row in rows})
        self._add(
            key, "pooled", how, k=hits, n=len(rows), rate=hits / len(rows), cells=cells
        )

    def value(self, key: str, value: float | str, how: str) -> None:
        self._add(key, "value", how, value=value)

    def table(
        self, key: str, header: list[str], rows: list[list[str]], how: str
    ) -> None:
        if bad := [row for row in rows if len(row) != len(header)]:
            raise ValueError(
                f"{key}: rows {bad} don't have the header's {len(header)} cells"
            )
        self._add(key, "table", how, header=header, rows=rows)

    def figure[T](
        self,
        key: str,
        draw: Callable[[Any, T], None],
        data: T,
        how: str,
        size: tuple[float, float] = (6.5, 3),
    ) -> None:
        """Draws `data` with draw(ax, data) on a figure of `size` inches, to <figures>/<key>.svg."""
        self._claim(key)  # before drawing over another entry's file
        sha = figures.render(self.figures / f"{key}.svg", draw, data, size)
        self._add(key, "figure", how, path=f"{key}.svg", sha=sha)

    def text(self, key: str, text: str, how: str) -> None:
        self._add(key, "text", how, text=text)

    def merge(self, other: "Ledger", source: Provenance) -> None:
        """Takes in another ledger's entries (one data source's), each stamped with `source`; none of them
        when one of their keys is taken."""
        stamp = asdict(source) | {"logs": list(source.logs)}
        if taken := sorted(self.entries.keys() & other.entries.keys()):
            claimed = {
                "{module}.{function}".format(**self.entries[key]["source"])
                for key in taken
            }
            raise DuplicateKey(
                f"{', '.join(taken)}: also claimed by {', '.join(sorted(claimed))}"
            )
        self.entries |= {
            key: entry | {"source": stamp} for key, entry in other.entries.items()
        }

    def _claim(self, key: str) -> None:
        if key in self.entries:
            raise DuplicateKey(f"{key} is already in the ledger")

    def _add(self, key: str, kind: str, how: str, **fields: Any) -> None:
        self._claim(key)
        self.entries[key] = {"kind": kind, **fields, "how": how}
