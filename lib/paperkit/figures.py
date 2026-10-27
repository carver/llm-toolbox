"""Drawing the paper's figures with matplotlib: SVGs with their text left as text, so Typst sets it like the
body, in the paper's font when paper.toml names one (`figure_font`). The same figure always gives the same
file, so a build can tell when one changed."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib
from matplotlib import font_manager

matplotlib.use("svg")
import matplotlib.pyplot as plt

plt.rcParams["svg.fonttype"] = "none"  # text stays text
plt.rcParams["svg.hashsalt"] = (
    "paper"  # element ids don't change from one run to the next
)


def use_fonts(fonts: Path | None, family: str | None) -> None:
    """Makes the .ttf and .otf files in `fonts` known to matplotlib, and sets figures in `family`, so labels
    are measured as Typst will set them."""
    for font in sorted(fonts.glob("*.[ot]tf")) if fonts and fonts.is_dir() else []:
        font_manager.fontManager.addfont(str(font))
    if family:
        plt.rcParams["font.family"] = family


def render[T](
    path: Path, draw: Callable[[Any, T], None], data: T, size: tuple[float, float]
) -> str:
    """Draws `data` with draw(ax, data) on a figure of `size` inches, to the SVG at `path`. Returns the
    file's hash."""
    fig, ax = plt.subplots(figsize=size)
    try:
        draw(ax, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, bbox_inches="tight", metadata={"Date": None})
    finally:
        plt.close(fig)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
