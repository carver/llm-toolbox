"""bin/paper: ID parsing, and the CLI end to end with a fake HTTP fetcher and sleep."""

import importlib.machinery
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "paper"
loader = importlib.machinery.SourceFileLoader("paper", str(SCRIPT))
spec = importlib.util.spec_from_loader("paper", loader)
paper = importlib.util.module_from_spec(spec)
loader.exec_module(paper)

S2_ID = "76b411ae47ec3b766ed8a62796e7b3d68bc4ccdf"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2404.00806", paper.ArxivId("2404.00806")),
        ("2404.00806v2", paper.ArxivId("2404.00806v2")),
        ("1501.0001", paper.ArxivId("1501.0001")),
        ("https://arxiv.org/abs/2404.00806", paper.ArxivId("2404.00806")),
        ("https://arxiv.org/abs/2404.00806v3", paper.ArxivId("2404.00806v3")),
        ("http://arxiv.org/pdf/2404.00806", paper.ArxivId("2404.00806")),
        ("https://arxiv.org/pdf/2404.00806v1.pdf", paper.ArxivId("2404.00806v1")),
        ("https://www.arxiv.org/abs/2404.00806", paper.ArxivId("2404.00806")),
        ("10.1145/3442188.3445922", paper.Doi("10.1145/3442188.3445922")),
        (
            "https://doi.org/10.1145/3442188.3445922",
            paper.Doi("10.1145/3442188.3445922"),
        ),
        ("http://dx.doi.org/10.1000/xyz(1)", paper.Doi("10.1000/xyz(1)")),
        (
            "https://doi.org/10.1002/%28SICI%291097-4571%28199806%2949:8%3C693::AID-ASI4%3E3.0.CO;2-0",
            paper.Doi("10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-0"),
        ),
        (S2_ID, paper.S2PaperId(S2_ID)),
        (S2_ID.upper(), paper.S2PaperId(S2_ID)),
        (
            "https://proceedings.mlr.press/v202/x23a/x23a.pdf",
            paper.PdfUrl("https://proceedings.mlr.press/v202/x23a/x23a.pdf"),
        ),
        ("  2404.00806  ", paper.ArxivId("2404.00806")),
    ],
)
def test_parses_every_id_form(text, expected):
    assert paper.parse_id(text) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "hello world",
        "title:Some paper",
        "cs/0112017",
        "2404.008",
        "12345.6789",
        "10.1145",
        "10.1/x",
        S2_ID[:-1],
        S2_ID + "0",
        "https://arxiv.org/abs/",
        "https://arxiv.org/abs/notanid",
        "https://arxiv.org/html/2404.00806v1",
        "https://doi.org/",
        "ftp://example.com/x.pdf",
    ],
)
def test_rejects_what_is_not_an_id(bad):
    with pytest.raises(paper.UsageError):
        paper.parse_id(bad)


ATTENTION = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "year": 2017,
    "venue": "Neural Information Processing Systems",
    "citationCount": 100000,
    "externalIds": {
        "ArXiv": "1706.03762",
        "DOI": "10.48550/arXiv.1706.03762",
        "CorpusId": 13756489,
    },
    "authors": [
        {"authorId": "1", "name": "Ashish Vaswani", "affiliations": ["Google Brain"]},
        {"authorId": "2", "name": "Noam Shazeer", "affiliations": []},
    ],
}
COLLUSION = {
    "paperId": S2_ID,
    "title": "Information theoretic approach to detect collusion in multi-agent games",
    "year": 2022,
    "venue": "UAI",
    "citationCount": 3,
    "externalIds": {"CorpusId": 1},
    "authors": [{"authorId": "3", "name": "Trevor Bonjour"}],
}


class FakeHTTP:
    """Answers requests from a script of (method, url fragment, responses).

    Each response is a paper.Response, or an exception to raise. A route's
    last response repeats once the others are used up."""

    def __init__(self, *routes):
        self.routes = [(m, frag, list(rs)) for m, frag, rs in routes]
        self.calls = []

    def __call__(self, method, url, *, params=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers}
        )
        for m, frag, responses in self.routes:
            if m == method and frag in url:
                answer = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if "export.arxiv.org" in url:
            return arxiv_feed()  # tests that don't route arXiv get no dates
        raise AssertionError(f"unexpected request {method} {url}")


def arxiv_feed(*entries: tuple[str, str, str]) -> "paper.Response":
    """An arXiv API answer with one entry per (id with version, published, updated)."""
    xml = "".join(
        f"<entry><id>http://arxiv.org/abs/{vid}</id>"
        f"<published>{published}T17:57:34Z</published><updated>{updated}T00:41:52Z</updated>"
        "<title>t</title></entry>"
        for vid, published, updated in entries
    )
    body = (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{xml}</feed>'
    )
    return paper.Response(200, {}, body.encode())


def s2_calls(http):
    return [c for c in http.calls if "semanticscholar.org" in c["url"]]


def ok(body):
    return paper.Response(200, {}, json.dumps(body).encode())


def status(code, body=None, headers=None):
    return paper.Response(
        code, headers or {}, json.dumps(body or {"error": "x"}).encode()
    )


@pytest.fixture(autouse=True)
def key_file(monkeypatch, tmp_path):
    """No test reads the real API key: env unset, key file under tmp_path."""
    monkeypatch.delenv("S2_API_KEY", raising=False)
    path = tmp_path / "api-key"
    monkeypatch.setattr(paper, "KEY_FILE", path)
    return path


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def run(capsys, argv, http, extract=None):
    sleeps = []
    code = paper.main(argv, fetch=http, sleep=sleeps.append, extract=extract)
    out, err = capsys.readouterr()
    return code, out, err, sleeps


def test_meta_prints_a_compact_block(cache, capsys):
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    code, out, err, _ = run(capsys, ["meta", "1706.03762v5"], http)
    assert code == 0, err
    assert out == (
        "Attention is All you Need\n"
        "  2017 · Neural Information Processing Systems\n"
        "  authors: Ashish Vaswani (Google Brain), Noam Shazeer\n"
        "  citations: 100000\n"
        "  ids: arXiv:1706.03762 DOI:10.48550/arXiv.1706.03762"
        " S2:204e3073870fae3d05bcbc2f6a8e263d9b72e776\n"
    )
    assert (
        "api.semanticscholar.org/graph/v1/paper/arXiv:1706.03762"
        in (http.calls[0]["url"])
    )


RATE_LIMITED = {"message": "Too Many Requests.", "code": "429"}


@pytest.mark.parametrize(
    "failure",
    [
        status(429, RATE_LIMITED),
        status(500),
        status(503),
        paper.TransportError("ReadTimeout"),
        ok(RATE_LIMITED),
    ],
    ids=["429", "500", "503", "timeout", "error-object-in-200"],
)
def test_meta_backs_off_and_retries(cache, capsys, failure):
    http = FakeHTTP(("GET", "/paper/", [failure, failure, ok(ATTENTION)]))
    code, out, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    assert out.startswith("Attention is All you Need\n")
    assert len(s2_calls(http)) == 3
    assert len(sleeps) == 2
    assert 0 < sleeps[0] < sleeps[1]


def test_backoff_honours_retry_after(cache, capsys):
    limited = status(429, RATE_LIMITED, {"Retry-After": "17"})
    http = FakeHTTP(("GET", "/paper/", [limited, ok(ATTENTION)]))
    code, _, _, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0
    assert sleeps == [17]


@pytest.mark.parametrize(
    "failure",
    [status(429, RATE_LIMITED), paper.TransportError("ReadTimeout"), ok(RATE_LIMITED)],
    ids=["429", "timeout", "error-object-in-200"],
)
def test_gives_up_after_the_retry_budget_and_exits_3(cache, capsys, failure):
    http = FakeHTTP(("GET", "/paper/", [failure]))
    code, out, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert (code, out) == (3, "")
    assert len(http.calls) == 6
    assert sum(sleeps) <= 130
    assert "gave up after 6 attempts" in err
    assert not (cache / "s2").exists() or not any((cache / "s2").iterdir())


def test_a_long_retry_after_is_capped_by_the_budget(cache, capsys):
    limited = status(429, RATE_LIMITED, {"Retry-After": "3600"})
    http = FakeHTTP(("GET", "/paper/", [limited]))
    code, _, _, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 3
    assert sum(sleeps) <= 130


def test_a_client_error_is_not_retried(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [status(400, {"error": "bad field"})]))
    code, _, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 3
    assert sleeps == []
    assert "bad field" in err


def test_meta_prints_a_block_per_id_and_exits_1_for_one_not_found(cache, capsys):
    http = FakeHTTP(
        ("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]),
        (
            "GET",
            "/paper/DOI:10.1000/missing",
            [status(404, {"error": "Paper not found"})],
        ),
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
    )
    code, out, err, _ = run(
        capsys,
        ["meta", S2_ID, "https://doi.org/10.1000/missing", "1706.03762"],
        http,
    )
    assert code == 1
    titles = [line for line in out.splitlines() if not line.startswith(" ")]
    assert titles == [COLLUSION["title"], ATTENTION["title"]]
    assert "  2022 · UAI\n  authors: Trevor Bonjour\n" in out
    assert "  ids: S2:" + S2_ID in out
    assert "not found" in err and "10.1000/missing" in err


def test_meta_answers_a_repeat_from_the_cache(cache, capsys):
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    _, first, _, _ = run(capsys, ["meta", "1706.03762"], http)
    code, again, err, _ = run(capsys, ["meta", "1706.03762v2"], FakeHTTP())
    assert code == 0, err
    assert again == first
    assert len(s2_calls(http)) == 1
    assert any(cache.rglob("*.json"))


def test_meta_rejects_a_pdf_url_before_any_request(cache, capsys):
    http = FakeHTTP()
    code, _, err, _ = run(capsys, ["meta", "https://example.com/a.pdf"], http)
    assert code == 2
    assert "paper text" in err
    assert http.calls == []


def test_a_bad_id_exits_2(cache, capsys):
    code, _, err, _ = run(capsys, ["meta", "nonsense"], FakeHTTP())
    assert code == 2
    assert "not a paper id" in err


CITING = {
    "paperId": "a" * 40,
    "title": "Colluding agents",
    "year": 2024,
    "venue": "",
    "externalIds": {"ArXiv": "2405.00001"},
    "authors": [{"name": n} for n in ["A One", "B Two", "C Three", "D Four", "E Five"]],
}


def test_cites_lists_citing_papers_compactly(cache, capsys):
    http = FakeHTTP(
        (
            "GET",
            "/paper/arXiv:1706.03762/citations",
            [
                ok(
                    {
                        "offset": 0,
                        "data": [{"citingPaper": CITING}, {"citingPaper": ATTENTION}],
                    }
                )
            ],
        )
    )
    code, out, err, _ = run(capsys, ["cites", "1706.03762", "--limit", "2"], http)
    assert code == 0, err
    assert out == (
        "2024 · Colluding agents\n"
        "  authors: A One, B Two, C Three, +2 more\n"
        "  ids: arXiv:2405.00001 S2:" + "a" * 40 + "\n"
        "2017 · Attention is All you Need · Neural Information Processing Systems\n"
        "  authors: Ashish Vaswani (Google Brain), Noam Shazeer\n"
        "  ids: arXiv:1706.03762 DOI:10.48550/arXiv.1706.03762"
        " S2:204e3073870fae3d05bcbc2f6a8e263d9b72e776\n"
    )
    assert http.calls[0]["params"]["limit"] == 2


def test_cites_of_an_unknown_paper_exits_1(cache, capsys):
    http = FakeHTTP(("GET", "/citations", [status(404, {"error": "Paper not found"})]))
    code, out, err, _ = run(capsys, ["cites", "1706.03762"], http)
    assert (code, out) == (1, "")
    assert "not found" in err


def test_search_lists_the_top_matches_with_citations(cache, capsys):
    http = FakeHTTP(("GET", "/paper/search", [ok({"total": 1, "data": [COLLUSION]})]))
    code, out, err, _ = run(
        capsys, ["search", "detect", "collusion", "--limit", "5"], http
    )
    assert code == 0, err
    assert out == (
        "2022 · Information theoretic approach to detect collusion in multi-agent games"
        " · UAI\n"
        "  authors: Trevor Bonjour\n"
        f"  ids: S2:{S2_ID} · citations: 3\n"
    )
    assert http.calls[0]["params"]["query"] == "detect collusion"
    assert http.calls[0]["params"]["limit"] == 5


def test_search_with_no_match_exits_1(cache, capsys):
    http = FakeHTTP(("GET", "/paper/search", [ok({"total": 0, "offset": 0})]))
    code, out, err, _ = run(capsys, ["search", "zzzz"], http)
    assert (code, out) == (1, "")
    assert "no papers match" in err


def tiny_pdf(text):
    """A one-page PDF showing `text`, built by hand with a valid xref table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


def pdf_response(text="Hello collusion"):
    return paper.Response(200, {"Content-Type": "application/pdf"}, tiny_pdf(text))


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="needs pdftotext")
def test_text_downloads_an_arxiv_pdf_extracts_it_and_prints_the_path(cache, capsys):
    http = FakeHTTP(("GET", "https://arxiv.org/pdf/2404.00806v2", [pdf_response()]))
    code, out, err, _ = run(
        capsys, ["text", "https://arxiv.org/abs/2404.00806v2"], http
    )
    assert code == 0, err
    path = Path(out.strip())
    assert path.is_relative_to(cache)
    assert path.suffix == ".txt"
    assert "Hello collusion" in path.read_text()


def fake_extract(pdf, txt):
    txt.write_text("extracted from " + pdf.read_bytes()[:8].decode())


def test_text_takes_a_direct_pdf_url(cache, capsys):
    url = "https://proceedings.mlr.press/v202/x23a/x23a.pdf"
    http = FakeHTTP(("GET", url, [pdf_response()]))
    code, out, err, _ = run(capsys, ["text", url], http, extract=fake_extract)
    assert code == 0, err
    assert Path(out.strip()).read_text() == "extracted from %PDF-1.4"


def test_text_answers_a_repeat_from_the_cache(cache, capsys):
    http = FakeHTTP(("GET", "arxiv.org/pdf/2404.00806", [pdf_response()]))
    _, first, _, _ = run(capsys, ["text", "2404.00806"], http, extract=fake_extract)
    calls = []

    def counting_extract(pdf, txt):
        calls.append(pdf)

    code, again, _, _ = run(
        capsys, ["text", "2404.00806"], FakeHTTP(), extract=counting_extract
    )
    assert (code, again) == (0, first)
    assert calls == []


def test_text_retries_a_busy_arxiv(cache, capsys):
    busy = paper.Response(503, {"Retry-After": "5"}, b"busy")
    http = FakeHTTP(("GET", "arxiv.org/pdf/", [busy, pdf_response()]))
    code, _, err, sleeps = run(
        capsys, ["text", "2404.00806"], http, extract=fake_extract
    )
    assert code == 0, err
    assert sleeps == [5]


def test_text_of_a_page_that_is_not_a_pdf_exits_1_and_caches_nothing(cache, capsys):
    html = paper.Response(200, {"Content-Type": "text/html"}, b"<html>login</html>")
    url = "https://openreview.net/forum?id=abc"
    code, out, err, _ = run(
        capsys, ["text", url], FakeHTTP(("GET", url, [html])), extract=fake_extract
    )
    assert (code, out) == (1, "")
    assert "not a PDF" in err
    assert not any(cache.rglob("*.*"))


def test_text_of_a_missing_pdf_exits_1(cache, capsys):
    http = FakeHTTP(("GET", "arxiv.org/pdf/", [paper.Response(404, {}, b"nope")]))
    code, _, err, _ = run(capsys, ["text", "2404.00806"], http, extract=fake_extract)
    assert code == 1
    assert "not found" in err


def test_text_needs_an_arxiv_id_or_pdf_url(cache, capsys):
    code, _, err, _ = run(capsys, ["text", S2_ID], FakeHTTP(), extract=fake_extract)
    assert code == 2
    assert "arXiv" in err and "PDF URL" in err


def test_help_documents_subcommands_and_exit_codes(capsys):
    with pytest.raises(SystemExit) as exit:
        paper.main(["-h"])
    out = capsys.readouterr().out
    assert exit.value.code == 0
    for word in ("meta", "cites", "text", "search", "PAPER_CACHE", "Exit status"):
        assert word in out


def test_an_error_object_with_extra_fields_is_still_an_error(cache, capsys):
    limited = ok({"message": "Too Many Requests.", "code": "429", "detail": "x"})
    http = FakeHTTP(("GET", "/paper/", [limited, ok(ATTENTION)]))
    code, _, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    "body",
    [b"<html>oops</html>", b"[1, 2]", b'"just a string"', b"{}"],
    ids=["not-json", "list", "string", "dict-without-paper"],
)
def test_meta_with_a_malformed_200_exits_3_with_a_message(cache, capsys, body):
    http = FakeHTTP(("GET", "/paper/", [paper.Response(200, {}, body)]))
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert (code, out) == (3, "")
    assert "Semantic Scholar" in err and "unexpected" in err
    assert not any(cache.rglob("*.json"))


def test_cites_and_search_with_a_malformed_200_exit_3(cache, capsys):
    http = FakeHTTP(
        ("GET", "/citations", [paper.Response(200, {}, b"not json")]),
        ("GET", "/paper/search", [paper.Response(200, {}, b"[]")]),
    )
    assert run(capsys, ["cites", "1706.03762"], http)[0] == 3
    assert run(capsys, ["search", "x"], http)[0] == 3


def test_cites_with_no_citing_papers_says_so_and_exits_0(cache, capsys):
    http = FakeHTTP(("GET", "/citations", [ok({"offset": 0, "data": []})]))
    code, out, err, _ = run(capsys, ["cites", "1706.03762"], http)
    assert code == 0, err
    assert out == "no papers cite 1706.03762\n"


def test_cites_not_found_names_the_id(cache, capsys):
    http = FakeHTTP(("GET", "/citations", [status(404, {"error": "Paper not found"})]))
    _, _, err, _ = run(capsys, ["cites", "1706.03762v4"], http)
    assert "not found: 1706.03762v4" in err


def test_help_says_how_to_refresh_and_what_search_misses_exit_with(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Delete the s2 folder" in out
    assert "search with no hits exits 1" in out


KEY = "s2-secret-key-0123456789"


def sent_key(call):
    return (call["headers"] or {}).get("x-api-key")


def test_without_a_key_requests_go_anonymous(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [ok(ATTENTION)]))
    assert run(capsys, ["meta", "1706.03762"], http)[0] == 0
    assert sent_key(http.calls[0]) is None


def test_the_key_file_is_sent_to_semantic_scholar(cache, capsys, key_file):
    key_file.write_text(KEY + "\n")
    http = FakeHTTP(("GET", "/paper/", [ok(ATTENTION)]))
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0
    assert sent_key(http.calls[0]) == KEY
    assert KEY not in out + err


def test_the_env_var_wins_over_the_key_file(cache, capsys, key_file, monkeypatch):
    key_file.write_text("from-file")
    monkeypatch.setenv("S2_API_KEY", " " + KEY + " ")
    http = FakeHTTP(("GET", "/paper/search", [ok({"total": 1, "data": [COLLUSION]})]))
    run(capsys, ["search", "collusion"], http)
    assert sent_key(http.calls[0]) == KEY


def test_a_blank_env_var_falls_back_to_the_file(cache, capsys, key_file, monkeypatch):
    key_file.write_text(KEY)
    monkeypatch.setenv("S2_API_KEY", "  ")
    http = FakeHTTP(("GET", "/citations", [ok({"offset": 0, "data": []})]))
    run(capsys, ["cites", "1706.03762"], http)
    assert sent_key(http.calls[0]) == KEY


def test_a_blank_key_file_means_no_key(cache, capsys, key_file):
    key_file.write_text("\n")
    http = FakeHTTP(("GET", "/paper/", [ok(ATTENTION)]))
    run(capsys, ["meta", "1706.03762"], http)
    assert sent_key(http.calls[0]) is None


def test_pdf_downloads_never_get_the_key(cache, capsys, key_file):
    key_file.write_text(KEY)
    url = "https://example.com/paper.pdf"
    http = FakeHTTP(("GET", url, [pdf_response()]))
    assert run(capsys, ["text", url], http, extract=fake_extract)[0] == 0
    assert sent_key(http.calls[0]) is None


def test_the_key_stays_out_of_the_cache(cache, capsys, key_file):
    key_file.write_text(KEY)
    run(capsys, ["meta", "1706.03762"], FakeHTTP(("GET", "/paper/", [ok(ATTENTION)])))
    for f in cache.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text() and KEY not in f.name
    key_file.write_text("another-key")
    code, _, _, _ = run(capsys, ["meta", "1706.03762"], FakeHTTP())
    assert code == 0


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_key_says_where_it_came_from(cache, capsys, key_file, code):
    key_file.write_text(KEY)
    http = FakeHTTP(("GET", "/paper/", [status(code, {"message": "Forbidden"})]))
    exit_code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert exit_code == 3
    assert str(key_file) in err and "may be invalid" in err
    assert KEY not in out + err


def test_a_rejected_env_key_names_the_env_var(cache, capsys, monkeypatch):
    monkeypatch.setenv("S2_API_KEY", KEY)
    http = FakeHTTP(("GET", "/paper/", [status(403, {"message": "Forbidden"})]))
    _, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert "S2_API_KEY" in err and "may be invalid" in err
    assert KEY not in out + err


def test_a_403_without_a_key_has_no_key_hint(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [status(403, {"message": "Forbidden"})]))
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 3 and "HTTP 403: Forbidden" in err and "key" not in err


def test_help_says_where_the_api_key_goes(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = capsys.readouterr().out
    assert "S2_API_KEY" in out and ".semantic-scholar/api-key" in out


def test_an_unreadable_key_file_exits_2_naming_it(cache, capsys, key_file):
    key_file.mkdir()
    http = FakeHTTP(("GET", "/paper/", [ok(ATTENTION)]))
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 2 and str(key_file) in err and not http.calls


def test_pdf_downloads_ignore_an_unreadable_key_file(cache, capsys, key_file):
    key_file.mkdir()
    url = "https://example.com/paper.pdf"
    http = FakeHTTP(("GET", url, [pdf_response()]))
    assert run(capsys, ["text", url], http, extract=fake_extract)[0] == 0


def test_a_key_file_that_is_not_utf8_exits_2_without_echoing_it(
    cache, capsys, key_file
):
    key_file.write_bytes(b"\xffsecret")
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], FakeHTTP())
    assert code == 2 and "UTF-8" in err and "secret" not in out + err


ATTENTION_FEED = arxiv_feed(("1706.03762v7", "2017-06-12", "2023-08-02"))


def arxiv_calls(http):
    return [c for c in http.calls if "export.arxiv.org" in c["url"]]


def test_meta_adds_arxiv_version_and_dates(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    assert (
        out.splitlines()[-1] == "  arXiv: v7, submitted 2017-06-12, updated 2023-08-02"
    )
    (call,) = arxiv_calls(http)
    assert call["params"]["id_list"] == "1706.03762"


def test_meta_asks_arxiv_once_for_every_paper_in_the_call(cache, capsys):
    other = dict(ATTENTION, paperId="f" * 40, externalIds={"ArXiv": "2404.00806"})
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "/paper/arXiv:2404.00806", [ok(other)]),
        (
            "GET",
            "export.arxiv.org",
            [
                arxiv_feed(
                    ("1706.03762v7", "2017-06-12", "2023-08-02"),
                    ("2404.00806v6", "2024-03-31", "2026-08-15"),
                )
            ],
        ),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "2404.00806"], http)
    assert code == 0, err
    (call,) = arxiv_calls(http)
    assert set(call["params"]["id_list"].split(",")) == {"1706.03762", "2404.00806"}
    assert "  arXiv: v6, submitted 2024-03-31, updated 2026-08-15" in out


def test_meta_finds_the_arxiv_id_of_a_paper_given_by_doi(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/DOI:10.48550/arXiv.1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
    )
    code, out, _, _ = run(capsys, ["meta", "10.48550/arXiv.1706.03762"], http)
    assert code == 0
    assert "  arXiv: v7, submitted 2017-06-12" in out


def test_a_paper_without_an_arxiv_id_asks_arxiv_nothing(cache, capsys):
    http = FakeHTTP(("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]))
    code, out, _, _ = run(capsys, ["meta", S2_ID], http)
    assert code == 0
    assert "arXiv:" not in out.replace("ids: arXiv:", "")
    assert arxiv_calls(http) == []


def test_arxiv_dates_are_cached_for_a_day(cache, capsys):
    first = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
    )
    run(capsys, ["meta", "1706.03762"], first)
    again = FakeHTTP()
    code, out, _, _ = run(capsys, ["meta", "1706.03762"], again)
    assert code == 0
    assert "  arXiv: v7" in out
    assert again.calls == []


def test_stale_arxiv_dates_are_fetched_again(cache, capsys):
    import os
    import time

    first = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
    )
    run(capsys, ["meta", "1706.03762"], first)
    two_days_ago = time.time() - 2 * 86400
    for f in (cache / "arxiv").iterdir():
        os.utime(f, (two_days_ago, two_days_ago))
    newer = arxiv_feed(("1706.03762v8", "2017-06-12", "2026-10-01"))
    again = FakeHTTP(("GET", "export.arxiv.org", [newer]))
    code, out, _, _ = run(capsys, ["meta", "1706.03762"], again)
    assert code == 0
    assert "  arXiv: v8, submitted 2017-06-12, updated 2026-10-01" in out
    assert len(arxiv_calls(again)) == 1


def test_an_arxiv_failure_leaves_the_rest_and_warns(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [status(503)]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0
    assert out.startswith("Attention is All you Need\n")
    assert "arXiv: v" not in out
    assert "arXiv dates unavailable" in err
