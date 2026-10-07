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

    def __call__(self, method, url, *, params=None):
        self.calls.append({"method": method, "url": url, "params": params})
        for m, frag, responses in self.routes:
            if m == method and frag in url:
                answer = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected request {method} {url}")


def ok(body):
    return paper.Response(200, {}, json.dumps(body).encode())


def status(code, body=None, headers=None):
    return paper.Response(
        code, headers or {}, json.dumps(body or {"error": "x"}).encode()
    )


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
    assert len(http.calls) == 3
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
    assert len(http.calls) == 1
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
