"""paperkit draws the page pictures only when asked: a build without them renders none."""

import sys

from paperkit_testing import LIB, write_minimal

sys.path.insert(0, str(LIB))
from paperkit.build import build
from paperkit.typeset import Typeset


def test_a_build_without_pictures_renders_none(tmp_path, monkeypatch):
    def rendered(self):
        raise AssertionError("the page pictures were rendered")

    monkeypatch.setattr(Typeset, "pngs", property(rendered))
    report = build(write_minimal(tmp_path / "paper"), sources={})
    assert report.pages == []
