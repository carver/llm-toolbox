"""bin/web-shot: action parsing, plus a real run when a Chromium is cached."""

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "web-shot"
loader = importlib.machinery.SourceFileLoader("web_shot", str(SCRIPT))
spec = importlib.util.spec_from_loader("web_shot", loader)
web_shot = importlib.util.module_from_spec(spec)
loader.exec_module(web_shot)


def test_parses_actions():
    assert web_shot.parse_action("click #legend button[data-series=human]") == web_shot.Action(
        "click", ("#legend button[data-series=human]",))
    assert web_shot.parse_action("shot chart #trajectory .card").args == ("chart", "#trajectory .card")
    assert web_shot.parse_action("shot whole").args == ("whole",)
    assert web_shot.parse_action("  wait 500 ").args == ("500",)
    assert web_shot.parse_action("eval document.body.remove()").args == ("document.body.remove()",)


@pytest.mark.parametrize("bad", ["jump #x", "click", "wait", "wait soon", "shot", "eval"])
def test_rejects_bad_actions(bad):
    with pytest.raises(web_shot.UsageError):
        web_shot.parse_action(bad)


def test_a_local_path_becomes_a_file_url(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<p>")
    assert web_shot.page_url(str(page)) == page.resolve().as_uri()
    assert web_shot.page_url("https://example.com/a") == "https://example.com/a"


@pytest.mark.skipif(not web_shot.cached_chromium(), reason="no Chromium in Playwright's cache")
def test_clicks_shoots_and_reports_page_errors(tmp_path):
    page = tmp_path / "p.html"
    page.write_text('<button id="b" onclick="document.getElementById(\'t\').textContent=\'on\'">x</button>'
                    '<p id="t">off</p><script>setTimeout(() => { throw new Error("boom") }, 0)</script>')
    run = subprocess.run([str(SCRIPT), "--out", str(tmp_path / "out"), "--do", "click #b", "--do", "wait 50",
                          "--do", "shot text #t", str(page)], capture_output=True, text=True, timeout=180)
    assert run.returncode == 3, run.stderr
    assert "boom" in run.stderr
    shots = run.stdout.split()
    assert [Path(s).name for s in shots] == ["text.png", "final.png"]
    assert all(Path(s).stat().st_size > 0 for s in shots)
