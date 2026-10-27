"""The data sources: each fills a ledger with one experiment's or result's numbers, tables, figures and texts.

A source is a function of a `Ledger`, registered under a name with `@source`. The build imports every module in
the project's paper/data/ folder (as `paper.data.NAME`, with the project root on the import path, so a module
can import the project's own code and its sibling modules), runs each source into a ledger of its own, and
merges them, so two sources can't claim one key."""

import importlib
import pkgutil
import sys
from collections.abc import Callable
from pathlib import Path

from paperkit.ledger import Ledger

Source = Callable[[Ledger], None]

SOURCES: dict[str, Source] = {}


def source(name: str) -> Callable[[Source], Source]:
    """Registers a data source under `name`."""

    def register(function: Source) -> Source:
        if SOURCES.get(name, function) is not function:
            raise ValueError(f"two data sources are named {name}")
        SOURCES[name] = function
        return function

    return register


def registered(paper_dir: Path) -> dict[str, Source]:
    """Every source in <paper_dir>/data/'s modules, by name."""
    data = paper_dir / "data"
    if not data.is_dir():
        return dict(SOURCES)
    root = str(paper_dir.resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    for module in pkgutil.iter_modules([str(data)]):
        importlib.import_module(f"{paper_dir.resolve().name}.data.{module.name}")
    return dict(SOURCES)
