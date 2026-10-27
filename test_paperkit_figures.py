"""paperkit's figures: matplotlib is loaded only once a figure is drawn, and figures are set in the font
use_fonts names, also when a later build names another."""

import subprocess
import sys

from paperkit_testing import LIB

sys.path.insert(0, str(LIB))
from paperkit import figures


def test_building_loads_no_matplotlib_until_a_figure_is_drawn():
    """It takes most of a second to import, which a paper without figures shouldn't pay."""
    loaded = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import sys, paperkit.build; print('matplotlib' in sys.modules)",
        ],
        env={"PYTHONPATH": str(LIB), "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert loaded == "False\n"


def draw(ax, label):
    ax.set_title(label)


def test_the_first_figure_is_set_in_the_family_use_fonts_named_before_it(tmp_path):
    """In a fresh process, so the figure is the one that loads matplotlib, whatever ran before it."""
    script = (
        "import sys; from pathlib import Path; from paperkit import figures\n"
        "figures.use_fonts(None, 'DejaVu Serif')\n"
        "figures.render(Path(sys.argv[1]), lambda ax, label: ax.set_title(label), 'a title', (2, 1))\n"
    )
    subprocess.run(
        [sys.executable, "-P", "-c", script, str(tmp_path / "a.svg")],
        env={"PYTHONPATH": str(LIB), "PATH": "", "MPLCONFIGDIR": str(tmp_path / "mpl")},
        check=True,
    )
    assert "DejaVu Serif" in (tmp_path / "a.svg").read_text()


def test_a_later_use_fonts_applies_to_the_next_figure(tmp_path):
    figures.use_fonts(None, "DejaVu Serif")
    figures.render(tmp_path / "a.svg", draw, "first", (2, 1))
    figures.use_fonts(None, "DejaVu Sans Mono")
    figures.render(tmp_path / "b.svg", draw, "second", (2, 1))
    assert "DejaVu Sans Mono" in (tmp_path / "b.svg").read_text()
