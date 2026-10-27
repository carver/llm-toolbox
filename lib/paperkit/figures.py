"""Drawing the paper's figures with matplotlib: SVGs with their text left as text, so Typst sets it like the
body, in the paper's font when paper.toml names one (`figure_font`). The same figure always gives the same
file, so a build can tell when one changed. matplotlib is imported when the first figure is drawn: it takes
most of a second, which a paper without figures shouldn't pay."""

import hashlib
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from paperkit.atomic import write_atomic

_pyplot: Any = None  # matplotlib.pyplot, once a figure has been drawn
_fonts: tuple[Path | None, str | None] = (None, None)  # use_fonts' arguments


def use_fonts(fonts: Path | None, family: str | None) -> None:
    """Makes the .ttf and .otf files in `fonts` known to matplotlib, and sets figures in `family`, so labels
    are measured as Typst will set them: at once if matplotlib is loaded, else when it is."""
    global _fonts
    _fonts = (fonts, family)
    if _pyplot is not None:
        _apply_fonts(_pyplot)


def _apply_fonts(plt: Any) -> None:
    from matplotlib import font_manager

    fonts, family = _fonts
    for font in sorted(fonts.glob("*.[ot]tf")) if fonts and fonts.is_dir() else []:
        font_manager.fontManager.addfont(str(font))
    if family:
        plt.rcParams["font.family"] = family


def _plt() -> Any:
    """matplotlib.pyplot, imported and set up on first use."""
    global _pyplot
    if _pyplot is None:
        import matplotlib

        matplotlib.use("svg")
        import matplotlib.pyplot as plt

        plt.rcParams["svg.fonttype"] = "none"  # text stays text
        # Element ids don't change from one run to the next
        plt.rcParams["svg.hashsalt"] = "paper"
        _apply_fonts(plt)
        _pyplot = plt
    return _pyplot


def render[T](
    path: Path, draw: Callable[[Any, T], None], data: T, size: tuple[float, float]
) -> str:
    """Draws `data` with draw(ax, data) on a figure of `size` inches, to the SVG at `path`. Returns the
    file's hash."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=size)
    try:
        draw(ax, data)
        svg = io.BytesIO()
        fig.savefig(svg, format="svg", bbox_inches="tight", metadata={"Date": None})
    finally:
        plt.close(fig)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, svg.getvalue())
    return hashlib.sha256(svg.getvalue()).hexdigest()[:12]
