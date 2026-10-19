"""bin/web-shot: action parsing, plus a real run when a Chromium is cached."""

import http.server
import importlib.machinery
import importlib.util
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "web-shot"
loader = importlib.machinery.SourceFileLoader("web_shot", str(SCRIPT))
spec = importlib.util.spec_from_loader("web_shot", loader)
web_shot = importlib.util.module_from_spec(spec)
loader.exec_module(web_shot)


def test_parses_actions():
    assert web_shot.parse_action(
        "click #legend button[data-series=human]"
    ) == web_shot.Action("click", ("#legend button[data-series=human]",))
    assert web_shot.parse_action("shot chart #trajectory .card").args == (
        "chart",
        "#trajectory .card",
    )
    assert web_shot.parse_action("shot whole").args == ("whole",)
    assert web_shot.parse_action("  wait 500 ").args == ("500",)
    assert web_shot.parse_action("eval document.body.remove()").args == (
        "document.body.remove()",
    )
    assert web_shot.parse_action("text").args == ()
    assert web_shot.parse_action("text main .form").args == ("main .form",)
    assert web_shot.parse_action("links apply|airtable").args == ("apply|airtable",)
    assert web_shot.parse_action("scroll-all").args == ()


@pytest.mark.parametrize(
    "bad", ["jump #x", "click", "wait", "wait soon", "shot", "eval", "scroll-all now"]
)
def test_rejects_bad_actions(bad):
    with pytest.raises(web_shot.UsageError):
        web_shot.parse_action(bad)


def test_a_local_path_becomes_a_file_url(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<p>")
    assert web_shot.page_url(str(page)) == page.resolve().as_uri()
    assert web_shot.page_url("https://example.com/a") == "https://example.com/a"


@pytest.mark.skipif(
    not web_shot.cached_chromium(), reason="no Chromium in Playwright's cache"
)
def test_clicks_shoots_and_reports_page_errors(tmp_path):
    page = tmp_path / "p.html"
    page.write_text(
        "<button id=\"b\" onclick=\"document.getElementById('t').textContent='on'\">x</button>"
        '<p id="t">off</p><script>setTimeout(() => { throw new Error("boom") }, 0)</script>'
    )
    run = subprocess.run(
        [
            str(SCRIPT),
            "--out",
            str(tmp_path / "out"),
            "--do",
            "click #b",
            "--do",
            "wait 50",
            "--do",
            "shot text #t",
            str(page),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert run.returncode == 3, run.stderr
    assert "boom" in run.stderr
    shots = run.stdout.split()
    assert [Path(s).name for s in shots] == ["text.png", "final.png"]
    assert all(Path(s).stat().st_size > 0 for s in shots)


def test_repeated_errors_collapse_to_one_line_with_a_count():
    assert web_shot.collapse(["a", "b", "a", "a"]) == ["a (x3)", "b"]


@pytest.mark.skipif(
    not web_shot.cached_chromium(), reason="no Chromium in Playwright's cache"
)
def test_prints_text_and_links_after_loading_an_endless_page(tmp_path):
    page = tmp_path / "p.html"
    page.write_text(
        '<main><h1>Form</h1><a href="https://airtable.com/f">Apply now</a><a href="/about">About</a></main>'
        '<div id="feed"></div><script>'
        'let n = 0; function more() { if (n >= 30) return; const d = document.createElement("p");'
        ' d.style.height = "400px"; d.textContent = "item " + (++n); feed.appendChild(d); }'
        "for (let i = 0; i < 3; i++) more();"
        'addEventListener("scroll", () => setTimeout(() => { more(); more(); }, 50));'
        'for (let i = 0; i < 5; i++) console.error("same noise");'
        "</script>"
    )
    run = subprocess.run(
        [
            str(SCRIPT),
            "--out",
            str(tmp_path / "out"),
            "--do",
            "scroll-all",
            "--do",
            "text main",
            "--do",
            "links airtable",
            "--do",
            "text #feed",
            str(page),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert run.returncode == 3, run.stderr
    lines = run.stdout.splitlines()
    assert lines[0] == "Form"
    assert "Apply now -> https://airtable.com/f" in lines
    assert "About -> " not in run.stdout
    assert "item 30" in run.stdout
    assert run.stderr.strip().splitlines() == ["console error: same noise (x5)"]
    assert lines[-1].endswith("final.png")


def test_cookies_are_sent_with_the_page(tmp_path):
    """For apps that keep who you are in a cookie, so web-shot can skip their login page."""

    class EchoCookie(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"<p id=c>{self.headers.get('Cookie', 'none')}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), EchoCookie)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run = subprocess.run(
            [
                str(SCRIPT),
                "--out",
                str(tmp_path / "out"),
                "--cookie",
                "user_id=2",
                "--cookie",
                "tz=UTC",
                "--do",
                "text #c",
                f"http://127.0.0.1:{server.server_port}/items/1",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        server.shutdown()
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[0] == "user_id=2; tz=UTC"


@pytest.mark.parametrize("cookie", ["user_id", "=2"])
def test_rejects_a_cookie_that_isnt_name_equals_value(tmp_path, cookie):
    run = subprocess.run(
        [str(SCRIPT), "--cookie", cookie, "http://127.0.0.1:9/"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 2
    assert "NAME=VALUE" in run.stderr


def test_cookies_need_an_http_page(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<p>hi</p>")
    run = subprocess.run(
        [str(SCRIPT), "--out", str(tmp_path / "out"), "--cookie", "a=1", str(page)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert run.returncode == 2
    assert "http(s)" in run.stderr
