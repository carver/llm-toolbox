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


class Request:
    def __init__(self, resource_type="fetch"):
        self.resource_type = resource_type


class Feed:
    """A stand-in for a tab: a feed whose scroll handler fetches the next page, `pages` times, each fetch
    starting `delay_ms` after the scroll and taking `fetch_ms`. Its clock moves only in wait_for_timeout;
    with `polls` set, a request is always in flight."""

    def __init__(self, pages=0, fetch_ms=200, delay_ms=0, polls=False):
        self.now, self.height, self.left = 0, 1000, pages
        self.fetch_ms, self.delay_ms = fetch_ms, delay_ms
        self.network = web_shot.Network(clock=lambda: self.now / 1000)
        self.fetch = None  # (request, when it ends)
        self.starts = None  # when a scroll's delayed fetch starts
        if polls:
            self.network.started(Request())

    def evaluate(self, js):
        if js.startswith("window.scrollTo"):
            if self.left and self.fetch is None and self.starts is None:
                self.starts = self.now + self.delay_ms
                self.start_due()
            return None
        return self.height

    def start_due(self):
        if self.starts is not None and self.now >= self.starts:
            self.starts, self.fetch = None, (Request(), self.now + self.fetch_ms)
            self.network.started(self.fetch[0])

    def wait_for_timeout(self, ms):
        self.now += ms
        self.start_due()
        if self.fetch and self.now >= self.fetch[1]:
            self.network.ended(self.fetch[0])
            self.fetch, self.left, self.height = None, self.left - 1, self.height + 400


def test_scroll_all_ends_within_three_seconds_on_a_page_that_is_done():
    feed = Feed()
    web_shot.scroll_all(feed, feed.network)
    assert feed.now <= 3000


def test_scroll_all_waits_out_a_fetch_slower_than_its_quiet_window():
    feed = Feed(pages=2, fetch_ms=5000)
    web_shot.scroll_all(feed, feed.network)
    assert feed.left == 0


def test_scroll_all_waits_for_a_loader_that_starts_its_fetch_late():
    """A debounced or throttled scroll handler fetches the next page a while after the scroll."""
    feed = Feed(pages=2, delay_ms=1500)
    web_shot.scroll_all(feed, feed.network)
    assert feed.left == 0


def test_an_open_event_stream_does_not_hold_up_scroll_all():
    feed = Feed()
    feed.network.started(Request("eventsource"))
    web_shot.scroll_all(feed, feed.network)
    assert feed.now <= 3000


def test_scroll_all_ends_on_a_page_that_never_stops_polling():
    feed = Feed(polls=True)
    web_shot.scroll_all(feed, feed.network)
    assert feed.now <= 7000


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


def cli(*args):
    return subprocess.run(
        [str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.skipif(
    not web_shot.cached_chromium(), reason="no Chromium in Playwright's cache"
)
def test_eval_prints_its_value_as_json_where_it_runs(tmp_path):
    """A doctype after a comment still counts, so this page loads as it is."""
    page = tmp_path / "p.html"
    page.write_text(
        "<!-- a mockup -->\n<!DOCTYPE html><meta charset=utf-8><title>Tëst</title><p id=t>hello</p>",
        encoding="utf-8",
    )
    run = cli(
        "--out",
        tmp_path / "out",
        "--do",
        "eval document.title",
        "--do",
        "text #t",
        "--do",
        "eval [1, {a: null}, document.compatMode]",
        "--do",
        "eval console.log('ignored')",
        "--do",
        "eval Promise.resolve(2)",
        page,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        '"Tëst"',
        "hello",
        '[1, {"a": null}, "CSS1Compat"]',
        "undefined",
        "2",
        str(tmp_path / "out" / "final.png"),
    ]
    assert run.stderr == ""


def png_size(path):
    """(width, height) from a PNG's header."""
    header = Path(path).read_bytes()[16:24]
    return int.from_bytes(header[:4], "big"), int.from_bytes(header[4:], "big")


@pytest.mark.skipif(
    not web_shot.cached_chromium(), reason="no Chromium in Playwright's cache"
)
def test_a_shot_taller_than_1500px_is_also_saved_in_pieces(tmp_path):
    """The fewest pieces of at most 1400px, of equal height but for the last. Pieces an earlier run
    left in the folder go."""
    page = tmp_path / "p.html"
    page.write_text(
        "<!doctype html><style>body { margin: 0 } div { background: teal }</style>"
        '<div id="tall" style="height: 1501px"></div><div id="edge" style="height: 1500px"></div>'
    )
    out = tmp_path / "out"
    out.mkdir()
    for stale in ("edge.part1.png", "final.part4.png"):
        (out / stale).write_bytes(b"")
    run = cli(
        "--out",
        out,
        "--size",
        "200x300",
        "--do",
        "shot tall #tall",
        "--do",
        "shot edge #edge",
        page,
    )
    assert run.returncode == 0, run.stderr
    names = ["tall", "tall.part1", "tall.part2", "edge", "final"]
    names += [f"final.part{n}" for n in (1, 2, 3)]
    assert run.stdout.splitlines() == [str(out / f"{name}.png") for name in names]
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{n}.png" for n in names)
    sizes = {name: png_size(out / f"{name}.png") for name in names}
    assert sizes["tall"] == (200, 1501)
    assert [sizes[f"tall.part{n}"] for n in (1, 2)] == [(200, 751), (200, 750)]
    assert sizes["final"] == (200, 3001)
    assert [sizes[f"final.part{n}"] for n in (1, 2, 3)] == [
        (200, 1001),
        (200, 1001),
        (200, 999),
    ]


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
    assert lines[lines.index("item 30") + 1].endswith("/final.png")


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
    # A 10 ms shutdown poll, not the default half second
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
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
