"""bin/paper: ID parsing, and the CLI end to end with a fake HTTP fetcher and sleep."""

import importlib.machinery
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
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

    Each response is a paper.Response, an exception to raise, or a function
    that makes the Response from the request's call record. A route's last
    response repeats once the others are used up."""

    def __init__(self, *routes):
        self.routes = [(m, frag, list(rs)) for m, frag, rs in routes]
        self.calls = []

    def __call__(self, method, url, *, params=None, headers=None, json=None):
        call = {
            "method": method,
            "url": url,
            "params": params,
            "headers": headers,
            "json": json,
        }
        self.calls.append(call)
        for m, frag, responses in self.routes:
            if m == method and frag in url:
                answer = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer(call) if callable(answer) else answer
        if "export.arxiv.org" in url:
            return arxiv_feed()  # tests that don't route arXiv get no dates
        raise AssertionError(f"unexpected request {method} {url}")


def arxiv_feed(*entries: tuple) -> "paper.Response":
    """An arXiv API answer with one entry per (id with version, published, updated),
    each optionally followed by its title and its authors' names."""
    xml = ""
    for vid, published, updated, *rest in entries:
        title, authors = rest or ("t", ())
        xml += (
            f"<entry><id>http://arxiv.org/abs/{vid}</id>"
            f"<published>{published}T17:57:34Z</published>"
            f"<updated>{updated}T00:41:52Z</updated><title>{title}</title>"
            + "".join(f"<author><name>{a}</name></author>" for a in authors)
            + "</entry>"
        )
    body = (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{xml}</feed>'
    )
    return paper.Response(200, {}, body.encode())


def ok(body):
    return paper.Response(200, {}, json.dumps(body).encode())


def status(code, body=None, headers=None):
    return paper.Response(
        code, headers or {}, json.dumps(body or {"error": "x"}).encode()
    )


@pytest.fixture(autouse=True)
def key_file(monkeypatch, tmp_path):
    """No test reads a real API key: env vars unset, key files under tmp_path."""
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setattr(paper, "OPENALEX_KEY_FILE", tmp_path / "openalex-api-key")
    path = tmp_path / "api-key"
    monkeypatch.setattr(paper, "KEY_FILE", path)
    return path


@pytest.fixture
def openalex_key_file(tmp_path):
    return tmp_path / "openalex-api-key"


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def run(capsys, argv, http, extract=None, paces=None):
    """Runs the CLI with no real pacing; `paces` collects the service of each paced request."""
    sleeps = []
    pace = paces.append if paces is not None else (lambda service: None)
    code = paper.main(argv, fetch=http, sleep=sleeps.append, extract=extract, pace=pace)
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
    http = FakeHTTP(("GET", "/paper/", [failure]), OPENALEX_HAS_NOTHING)
    code, out, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert (code, out) == (3, "")
    assert len(s2_calls(http)) == 6
    assert sum(sleeps) <= 130
    assert "gave up after 6 attempts" in err
    assert not (cache / "s2").exists() or not any((cache / "s2").iterdir())


def test_a_long_retry_after_is_capped_by_the_budget(cache, capsys):
    limited = status(429, RATE_LIMITED, {"Retry-After": "3600"})
    http = FakeHTTP(("GET", "/paper/", [limited]), OPENALEX_HAS_NOTHING)
    code, _, _, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 3
    assert sum(sleeps) <= 130


OPENALEX_HAS_NOTHING = ("GET", "api.openalex.org", [paper.Response(404, {}, b"<html>")])
CROSSREF_HAS_NOTHING = (
    "GET",
    "api.crossref.org",
    [paper.Response(404, {"Content-Type": "text/plain"}, b"Resource not found.")],
)


def test_a_client_error_is_not_retried(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [status(400, {"error": "bad field"})]))
    code, _, err, sleeps = run(capsys, ["meta", "1706.03762"], http)
    assert code == 3
    assert sleeps == []
    assert "bad field" in err


def test_meta_prints_a_block_per_id_and_exits_1_for_one_not_found(cache, capsys):
    http = FakeHTTP(
        ("POST", "/paper/batch", [ok([COLLUSION, None, ATTENTION])]),
        OPENALEX_HAS_NOTHING,
        CROSSREF_HAS_NOTHING,
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


@pytest.fixture
def no_paper_cache(monkeypatch, tmp_path):
    """$PAPER_CACHE unset, the default cache and $HOME under tmp_path; returns both caches."""
    monkeypatch.delenv("PAPER_CACHE", raising=False)
    default = tmp_path / "claudedev" / "cache" / "paper"
    monkeypatch.setattr(paper, "DEFAULT_CACHE", str(default))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return default, tmp_path / "home" / ".cache" / "paper"


def test_meta_caches_in_the_default_cache_when_its_parent_exists(
    no_paper_cache, capsys
):
    default, home_cache = no_paper_cache
    default.parent.mkdir(parents=True)
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    assert any(default.rglob("*.json"))
    assert not home_cache.exists()


def test_meta_caches_in_home_cache_where_the_default_has_no_parent(
    no_paper_cache, capsys
):
    default, home_cache = no_paper_cache
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    assert any(home_cache.rglob("*.json"))
    assert not default.parent.exists()


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
    assert [p.name for p in cache.rglob("*.*")] == ["requests.jsonl"]


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


def calls_to(http, host):
    return [c for c in http.calls if host in c["url"]]


def s2_calls(http):
    return calls_to(http, "semanticscholar.org")


def arxiv_calls(http):
    return calls_to(http, "export.arxiv.org")


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
        ("POST", "/paper/batch", [ok([ATTENTION, other])]),
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
    assert "arXiv's latest versions unavailable" in err


def test_a_later_semantic_scholar_failure_keeps_the_blocks_already_fetched(
    cache, capsys
):
    first = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    run(
        capsys, ["meta", "1706.03762"], first
    )  # cached, so only the second is asked for
    http = FakeHTTP(
        ("GET", "/paper/arXiv:2404.00806", [status(403, {"error": "forbidden"})]),
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "2404.00806"], http)
    assert code == 3
    assert out.startswith("Attention is All you Need\n")
    assert "  arXiv: v7" in out
    assert "forbidden" in err


def test_an_id_missing_from_the_arxiv_answer_warns_and_is_asked_again(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", "export.arxiv.org", [arxiv_feed()]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0
    assert "arXiv: v" not in out
    assert "arXiv has no entry for 1706.03762" in err
    again = FakeHTTP(("GET", "export.arxiv.org", [ATTENTION_FEED]))
    _, out, _, _ = run(capsys, ["meta", "1706.03762"], again)
    assert "  arXiv: v7" in out
    assert len(arxiv_calls(again)) == 1


def test_old_style_arxiv_ids_get_dates_too(cache, capsys):
    teleport = dict(
        ATTENTION,
        paperId="e" * 40,
        title="Experimental quantum teleportation",
        externalIds={"ArXiv": "quant-ph/9801069", "DOI": "10.1038/37539"},
    )
    http = FakeHTTP(
        ("GET", "/paper/DOI:10.1038/37539", [ok(teleport)]),
        (
            "GET",
            "export.arxiv.org",
            [arxiv_feed(("quant-ph/9801069v2", "1998-01-30", "1998-02-04"))],
        ),
    )
    code, out, err, _ = run(capsys, ["meta", "10.1038/37539"], http)
    assert code == 0, err
    assert "  arXiv: v2, submitted 1998-01-30, updated 1998-02-04" in out
    assert all(f.is_file() for f in (cache / "arxiv").iterdir())


# outline and grep read the cached text, fetching it first like `text`.

PAPER_TEXT = """\
Collusion Among Trading Agents
                          Abstract
We study collusion between agents.
1 Introduction                              Agents trade in markets.
Some body text from 2024 with numbers 12 34.
2 Related Work
3 Method
3.1 Setup
Table 1: Results 0.45 0.33 0.21
4 Results
The leak rate was 21.9% with a hypothesis
and 3.5% without one.
A Appendix
References
5    Wide Gap Heading                       right column text
1. Unrelated Data: No relevance to the task do-
C ON V ERSE: Benchmarking Contextual Safety in
14 Kensington Gardens, London,
4  My client's full residential address is:
"""


def text_extract(pdf, txt):
    txt.write_text(PAPER_TEXT)


def paper_run(capsys, *argv):
    """Runs paper on 2404.00806, whose PDF the fake server holds and whose text is PAPER_TEXT."""
    http = FakeHTTP(("GET", "arxiv.org/pdf/2404.00806", [pdf_response()]))
    return run(capsys, list(argv), http, extract=text_extract)


def test_outline_lists_headings_with_line_numbers(cache, capsys):
    code, out, err, _ = paper_run(capsys, "outline", "2404.00806")
    assert code == 0, err
    assert [line.split(None, 1) for line in out.splitlines()] == [
        ["2", "Abstract"],
        ["4", "1 Introduction"],
        ["6", "2 Related Work"],
        ["7", "3 Method"],
        ["8", "3.1 Setup"],
        ["10", "4 Results"],
        ["13", "A Appendix"],
        ["14", "References"],
        ["15", "5 Wide Gap Heading"],
    ]


def test_line_numbers_count_newlines_only_not_page_breaks(cache, capsys):
    # pdftotext puts a form feed between pages; str.splitlines() would count it as a line.
    def paged(pdf, txt):
        txt.write_text("page one\n\f2 Method\nthe leak rate\n")

    http = FakeHTTP(("GET", "arxiv.org/pdf/2404.00806", [pdf_response()]))
    _, out, _, _ = run(capsys, ["outline", "2404.00806"], http, extract=paged)
    assert out.split() == ["2", "2", "Method"]
    _, out, _, _ = run(capsys, ["grep", "2404.00806", "leak", "-C", "0"], FakeHTTP())
    assert out == "3:the leak rate\n"


@pytest.mark.parametrize(
    "line",
    [
        "Appendix J describes its architecture and coverage.",
        "C. Xie, C. Yang, and B. Li. 2024. “Guardagent: Safeguarding agents,”",
        "85 Emergent misaligned",
        "2.48 Pile/Chat",
    ],
)
def test_outline_skips_prose_references_and_table_cells(cache, capsys, line):
    def text(pdf, txt):
        txt.write_text(f"1 Introduction\n{line}\n")

    http = FakeHTTP(("GET", "arxiv.org/pdf/2404.00806", [pdf_response()]))
    _, out, _, _ = run(capsys, ["outline", "2404.00806"], http, extract=text)
    assert out.split(None, 1) == ["1", "1 Introduction\n"]


def test_outline_and_grep_name_themselves_when_given_an_s2_paper_id(cache, capsys):
    for argv in (["outline", S2_ID], ["grep", S2_ID, "x"]):
        code, _, err, _ = run(capsys, argv, FakeHTTP())
        assert code == 2
        assert f"paper {argv[0]}" in err


def test_outline_uses_the_cached_text(cache, capsys):
    paper_run(capsys, "text", "2404.00806")
    code, out, _, _ = run(capsys, ["outline", "2404.00806"], FakeHTTP())
    assert code == 0
    assert "3 Method" in out


def test_outline_of_a_text_without_headings_exits_1(cache, capsys):
    def plain(pdf, txt):
        txt.write_text("just a paragraph\nof plain text\n")

    http = FakeHTTP(("GET", "arxiv.org/pdf/2404.00806", [pdf_response()]))
    code, out, err, _ = run(capsys, ["outline", "2404.00806"], http, extract=plain)
    assert (code, out) == (1, "")
    assert "grep" in err


def test_grep_prints_matches_with_line_numbers_and_context(cache, capsys):
    code, out, err, _ = paper_run(capsys, "grep", "2404.00806", "leak rate", "-C", "1")
    assert code == 0, err
    assert out.splitlines() == [
        "10-4 Results",
        "11:The leak rate was 21.9% with a hypothesis",
        "12-and 3.5% without one.",
    ]


def test_grep_merges_close_matches_and_separates_far_ones(cache, capsys):
    code, out, _, _ = paper_run(capsys, "grep", "2404.00806", "^3 |^A ", "-C", "1")
    assert code == 0
    assert out.splitlines() == [
        "6-2 Related Work",
        "7:3 Method",
        "8-3.1 Setup",
        "--",
        "12-and 3.5% without one.",
        "13:A Appendix",
        "14-References",
    ]


def test_grep_without_context_prints_no_separators_like_grep(cache, capsys):
    code, out, _, _ = paper_run(capsys, "grep", "2404.00806", "^3|^A ", "-C", "0")
    assert code == 0
    assert out.splitlines() == ["7:3 Method", "8:3.1 Setup", "13:A Appendix"]


def test_grep_with_negative_context_exits_2(cache, capsys):
    with pytest.raises(SystemExit) as exit_:
        paper_run(capsys, "grep", "2404.00806", "leak", "-C", "-1")
    assert exit_.value.code == 2


def test_grep_ignores_case_with_i(cache, capsys):
    code, out, _, _ = paper_run(capsys, "grep", "2404.00806", "LEAK", "-i", "-C", "0")
    assert (code, out) == (0, "11:The leak rate was 21.9% with a hypothesis\n")


def test_grep_with_no_match_exits_1(cache, capsys):
    code, out, _, _ = paper_run(capsys, "grep", "2404.00806", "kangaroo")
    assert (code, out) == (1, "")


def test_grep_with_a_bad_pattern_exits_2(cache, capsys):
    code, _, err, _ = paper_run(capsys, "grep", "2404.00806", "(")
    assert code == 2
    assert "pattern" in err


# Pacing: requests to each service stay a minimum interval apart, across processes.


class FakeClock:
    """time.time and time.sleep for a Pacer: sleeping moves the clock on."""

    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def pacer(tmp_path, clock, interval=1.1):
    return paper.Pacer(tmp_path / "pace", interval, clock=clock, sleep=clock.sleep)


def test_the_first_request_does_not_wait_and_records_its_time(tmp_path):
    clock = FakeClock()
    pacer(tmp_path, clock).wait()
    assert clock.sleeps == []
    assert float((tmp_path / "pace").read_text()) == 1000.0


def test_a_request_right_after_another_waits_the_interval(tmp_path):
    clock = FakeClock()
    p = pacer(tmp_path, clock)
    p.wait()
    p.wait()
    assert clock.sleeps == [pytest.approx(1.1)]


def test_a_request_waits_only_what_is_left_of_the_interval(tmp_path):
    clock = FakeClock()
    p = pacer(tmp_path, clock)
    p.wait()
    clock.now += 0.4
    p.wait()
    assert clock.sleeps == [pytest.approx(0.7)]


def test_a_request_after_the_interval_does_not_wait(tmp_path):
    clock = FakeClock()
    p = pacer(tmp_path, clock)
    p.wait()
    clock.now += 5
    p.wait()
    assert clock.sleeps == []
    assert float((tmp_path / "pace").read_text()) == 1005.0


def test_pacers_on_one_file_share_the_interval(tmp_path):
    clock = FakeClock()
    pacer(tmp_path, clock).wait()
    pacer(tmp_path, clock).wait()  # another process, same file
    assert clock.sleeps == [pytest.approx(1.1)]


def test_a_clock_set_back_waits_at_most_one_interval(tmp_path):
    clock = FakeClock(now=5000.0)
    p = pacer(tmp_path, clock)
    p.wait()
    clock.now = 1000.0
    p.wait()
    assert clock.sleeps == [pytest.approx(1.1)]


def test_an_unreadable_timestamp_does_not_wait_and_is_replaced(tmp_path):
    (tmp_path / "pace").write_text("garbage")
    clock = FakeClock()
    pacer(tmp_path, clock).wait()
    assert clock.sleeps == []
    assert float((tmp_path / "pace").read_text()) == 1000.0


PACE_WORKER = """
import importlib.machinery, importlib.util, sys, time
loader = importlib.machinery.SourceFileLoader("paper", sys.argv[1])
spec = importlib.util.spec_from_loader("paper", loader)
paper = importlib.util.module_from_spec(spec)
loader.exec_module(paper)
p = paper.Pacer(__import__("pathlib").Path(sys.argv[2]), 0.2)
with open(sys.argv[3], "a") as out:
    for _ in range(3):
        out.write(f"{p.wait()}\\n")  # the time the pacer recorded, under its lock
        out.flush()
"""


def test_processes_sharing_a_file_stay_spaced(tmp_path):
    log = tmp_path / "times"
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                PACE_WORKER,
                str(SCRIPT),
                str(tmp_path / "pace"),
                str(log),
            ]
        )
        for _ in range(3)
    ]
    for w in workers:
        assert w.wait(timeout=30) == 0
    times = sorted(float(t) for t in log.read_text().split())
    assert len(times) == 9
    assert min(b - a for a, b in itertools.pairwise(times)) >= 0.2 - 1e-6


def test_every_semantic_scholar_attempt_and_arxiv_call_is_paced(cache, capsys):
    http = FakeHTTP(
        (
            "GET",
            "/paper/",
            [status(429, RATE_LIMITED), status(429, RATE_LIMITED), ok(ATTENTION)],
        )
    )
    paces = []
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http, paces=paces)
    assert code == 0, err
    assert paces == ["Semantic Scholar"] * 3 + ["arXiv"]


def test_answers_from_the_cache_are_not_paced(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/", [ok(ATTENTION)]),
        (
            "GET",
            "export.arxiv.org",
            [arxiv_feed(("1706.03762v7", "2017-06-12", "2023-08-02"))],
        ),
    )
    first, again = [], []
    run(capsys, ["meta", "1706.03762"], http, paces=first)
    code, _, err, _ = run(capsys, ["meta", "1706.03762"], http, paces=again)
    assert code == 0, err
    assert first == ["Semantic Scholar", "arXiv"]
    assert again == []


def test_pdf_downloads_are_not_paced(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/", [ok(ATTENTION)]),
        ("GET", "arxiv.org/pdf/2404.00806", [paper.Response(200, {}, b"%PDF-1.4 x")]),
    )
    paces = []
    run(capsys, ["meta", "1706.03762"], http, paces=paces)
    assert paces == ["Semantic Scholar", "arXiv"]
    paces.clear()
    code, _, err, _ = run(
        capsys,
        ["text", "2404.00806"],
        http,
        extract=lambda pdf, txt: txt.write_text("x"),
        paces=paces,
    )
    assert code == 0, err
    assert paces == []


def test_by_default_the_pacing_files_live_in_the_cache(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [ok(ATTENTION)]))
    code = paper.main(["meta", "1706.03762"], fetch=http, sleep=lambda s: None)
    assert code == 0
    assert sorted(p.name for p in cache.glob(".pace-*")) == [
        ".pace-arxiv",
        ".pace-semantic-scholar",
    ]


# OpenAlex, the fallback when Semantic Scholar gives up

S2_DOWN = ("GET", "semanticscholar.org", [status(429, RATE_LIMITED)])
S2_BATCH_DOWN = ("POST", "semanticscholar.org", [status(429, RATE_LIMITED)])
OA_ATTENTION = {
    "id": "https://openalex.org/W2626778328",
    "doi": "https://doi.org/10.48550/arxiv.1706.03762",
    "display_name": "Attention Is All You Need",
    "publication_year": 2017,
    "cited_by_count": 6000,
    "authorships": [
        {
            "author": {"display_name": "Ashish Vaswani"},
            "institutions": [{"display_name": "Google (United States)"}],
        },
        {"author": {"display_name": "Noam Shazeer"}, "institutions": []},
    ],
    "primary_location": {
        "source": {"display_name": "arXiv (Cornell University)"},
        "landing_page_url": "http://arxiv.org/abs/1706.03762",
    },
    "best_oa_location": {
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
        "landing_page_url": "http://arxiv.org/abs/1706.03762",
    },
    "locations": [],
}
OA_JOURNAL = {
    "id": "https://openalex.org/W2095883081",
    "doi": "https://doi.org/10.1111/1467-937x.00136",
    "display_name": "A Study of Collusion in First-Price Auctions",
    "publication_year": 2000,
    "cited_by_count": 364,
    "authorships": [
        {
            "author": {"display_name": "Martin Pesendorfer"},
            "institutions": [{"display_name": "Yale University"}],
        }
    ],
    "primary_location": {"source": {"display_name": "The Review of Economic Studies"}},
    "best_oa_location": None,
    "locations": [
        {"pdf_url": None, "landing_page_url": "https://doi.org/10.1111/1467-937x.00136"}
    ],
}
OA_ATTENTION_ROUTE = (
    "GET",
    "api.openalex.org/works/doi:10.48550/arXiv.1706.03762",
    [ok(OA_ATTENTION)],
)


def openalex_calls(http):
    return calls_to(http, "api.openalex.org")


def test_meta_falls_back_to_openalex_when_semantic_scholar_gives_up(cache, capsys):
    http = FakeHTTP(S2_DOWN, OA_ATTENTION_ROUTE)
    code, out, err, _ = run(capsys, ["meta", "1706.03762v5"], http)
    assert code == 0, err
    title, source, *rest = out.splitlines()
    assert title == "Attention Is All You Need"
    assert source.startswith("  source: OpenAlex")
    assert "Semantic Scholar gave up" in source
    assert "counts and lists far fewer citations" in source and "arXiv" in source
    assert rest[:4] == [
        "  2017 · arXiv (Cornell University)",
        "  authors: Ashish Vaswani (Google (United States)), Noam Shazeer",
        "  citations: 6000",
        "  ids: arXiv:1706.03762 DOI:10.48550/arxiv.1706.03762 OpenAlex:W2626778328",
    ]
    assert len(s2_calls(http)) == 6
    (call,) = openalex_calls(http)
    assert call["params"]["mailto"] == "robocarver@worldofmail.com"
    assert "gave up" in err and "OpenAlex" in err


def test_a_semantic_scholar_answer_carries_no_source_line(cache, capsys):
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    _, out, _, _ = run(capsys, ["meta", "1706.03762"], http)
    assert "source:" not in out


def test_once_semantic_scholar_gives_up_later_ids_go_straight_to_openalex(
    cache, capsys
):
    http = FakeHTTP(
        S2_BATCH_DOWN,
        OA_ATTENTION_ROUTE,
        ("GET", "api.openalex.org/works/doi:10.1111/1467-937X.00136", [ok(OA_JOURNAL)]),
    )
    code, out, err, _ = run(
        capsys, ["meta", "1706.03762", "10.1111/1467-937X.00136"], http
    )
    assert code == 0, err
    assert len(s2_calls(http)) == 6
    assert "A Study of Collusion in First-Price Auctions" in out
    assert "  ids: DOI:10.1111/1467-937x.00136 OpenAlex:W2095883081" in out


def test_a_semantic_scholar_paper_id_has_no_fallback(cache, capsys):
    http = FakeHTTP(S2_DOWN)
    code, out, err, _ = run(capsys, ["meta", S2_ID], http)
    assert (code, out) == (3, "")
    assert openalex_calls(http) == []
    assert "gave up" in err


def test_openalex_without_the_paper_still_exits_3_naming_both(cache, capsys):
    http = FakeHTTP(S2_DOWN, OPENALEX_HAS_NOTHING)
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert (code, out) == (3, "")
    assert "Semantic Scholar" in err and "OpenAlex" in err


def test_openalex_failing_too_exits_3(cache, capsys):
    http = FakeHTTP(S2_DOWN, ("GET", "api.openalex.org", [status(503)]))
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert (code, out) == (3, "")
    assert "OpenAlex" in err


OA_KEY = "oa-secret-key-0123456789"


def test_the_openalex_key_goes_only_to_openalex(cache, capsys, openalex_key_file):
    openalex_key_file.write_text(OA_KEY + "\n")
    http = FakeHTTP(S2_DOWN, OA_ATTENTION_ROUTE)
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert code == 0, err
    (call,) = openalex_calls(http)
    assert call["params"]["api_key"] == OA_KEY
    assert all(OA_KEY not in json.dumps(c) for c in s2_calls(http))
    assert OA_KEY not in out + err
    for f in cache.rglob("*"):
        if f.is_file():
            assert OA_KEY not in f.read_text() and OA_KEY not in f.name


def test_the_openalex_env_var_wins_over_its_key_file(
    cache, capsys, openalex_key_file, monkeypatch
):
    openalex_key_file.write_text("from-file")
    monkeypatch.setenv("OPENALEX_API_KEY", " " + OA_KEY + " ")
    http = FakeHTTP(S2_DOWN, OA_ATTENTION_ROUTE)
    run(capsys, ["meta", "1706.03762"], http)
    assert openalex_calls(http)[0]["params"]["api_key"] == OA_KEY


def test_without_an_openalex_key_requests_carry_none(cache, capsys):
    http = FakeHTTP(S2_DOWN, OA_ATTENTION_ROUTE)
    run(capsys, ["meta", "1706.03762"], http)
    assert "api_key" not in openalex_calls(http)[0]["params"]


NOT_ACTIVATED = status(
    403,
    {"error": "Forbidden", "code": "key_not_activated", "message": "not activated"},
)


def test_an_unactivated_openalex_key_is_dropped_with_one_warning(
    cache, capsys, openalex_key_file
):
    openalex_key_file.write_text(OA_KEY)
    http = FakeHTTP(
        S2_BATCH_DOWN,
        ("GET", "api.openalex.org", [NOT_ACTIVATED, ok(OA_ATTENTION), ok(OA_JOURNAL)]),
    )
    code, out, err, _ = run(
        capsys, ["meta", "1706.03762", "10.1111/1467-937X.00136"], http
    )
    assert code == 0, err
    first, *rest = openalex_calls(http)
    assert first["params"]["api_key"] == OA_KEY
    assert rest and all("api_key" not in c["params"] for c in rest)
    assert err.count("not activated") == 1
    assert OA_KEY not in out + err


def test_help_says_where_the_openalex_key_goes_and_what_the_fallback_does(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "OPENALEX_API_KEY" in out and ".openalex/api-key" in out
    assert "OpenAlex" in out and "fewer citations" in out


def test_search_falls_back_to_openalex_under_a_source_line(cache, capsys):
    http = FakeHTTP(
        S2_DOWN,
        ("GET", "api.openalex.org/works", [ok({"meta": {}, "results": [OA_JOURNAL]})]),
    )
    code, out, err, _ = run(
        capsys, ["search", "collusion", "auctions", "--limit", "3"], http
    )
    assert code == 0, err
    first, *rest = out.splitlines()
    assert first.startswith("source: OpenAlex") and "fewer citations" in first
    assert rest == [
        "2000 · A Study of Collusion in First-Price Auctions · The Review of Economic Studies",
        "  authors: Martin Pesendorfer (Yale University)",
        "  ids: DOI:10.1111/1467-937x.00136 OpenAlex:W2095883081 · citations: 364",
    ]
    (call,) = openalex_calls(http)
    assert call["params"]["search"] == "collusion auctions"
    assert call["params"]["per_page"] == 3


def test_an_openalex_search_with_no_match_exits_1_naming_openalex(cache, capsys):
    http = FakeHTTP(
        S2_DOWN, ("GET", "api.openalex.org/works", [ok({"meta": {}, "results": []})])
    )
    code, out, err, _ = run(capsys, ["search", "zzzz"], http)
    assert (code, out) == (1, "")
    assert "no papers match" in err and "OpenAlex" in err


OA_CITING = dict(
    OA_JOURNAL,
    id="https://openalex.org/W2141486460",
    doi="https://doi.org/10.1162/003465303772815871",
    display_name="Deciding Between Competition and Collusion",
    publication_year=2003,
    primary_location={"source": {"display_name": "Review of Economics and Statistics"}},
)


def test_cites_falls_back_to_openalex_under_a_source_line(cache, capsys):
    http = FakeHTTP(
        S2_DOWN,
        OA_ATTENTION_ROUTE,
        ("GET", "api.openalex.org/works", [ok({"meta": {}, "results": [OA_CITING]})]),
    )
    code, out, err, _ = run(capsys, ["cites", "1706.03762", "--limit", "2"], http)
    assert code == 0, err
    first, *rest = out.splitlines()
    assert first.startswith("source: OpenAlex") and "fewer citations" in first
    assert rest == [
        "2003 · Deciding Between Competition and Collusion · Review of Economics and Statistics",
        "  authors: Martin Pesendorfer (Yale University)",
        "  ids: DOI:10.1162/003465303772815871 OpenAlex:W2141486460",
    ]
    lookup, citing = openalex_calls(http)
    assert "/works/doi:10.48550/arXiv.1706.03762" in lookup["url"]
    assert citing["params"]["filter"] == "cites:W2626778328"
    assert citing["params"]["per_page"] == 2


def test_no_citers_in_openalex_is_not_reported_as_no_citers(cache, capsys):
    http = FakeHTTP(
        S2_DOWN,
        OA_ATTENTION_ROUTE,
        ("GET", "api.openalex.org/works", [ok({"meta": {}, "results": []})]),
    )
    code, out, err, _ = run(capsys, ["cites", "1706.03762"], http)
    assert code == 0, err
    assert "no papers cite" not in out
    assert "OpenAlex lists no papers citing 1706.03762" in out


def test_cites_of_a_semantic_scholar_paper_id_has_no_fallback(cache, capsys):
    http = FakeHTTP(S2_DOWN)
    code, _, _, _ = run(capsys, ["cites", S2_ID], http)
    assert code == 3
    assert openalex_calls(http) == []


def test_text_takes_an_arxiv_doi_straight_to_arxiv(cache, capsys):
    http = FakeHTTP(("GET", "https://arxiv.org/pdf/2404.00806", [pdf_response()]))
    code, out, err, _ = run(
        capsys, ["text", "10.48550/arXiv.2404.00806"], http, extract=fake_extract
    )
    assert code == 0, err
    assert Path(out.strip()).read_text() == "extracted from %PDF-1.4"
    assert openalex_calls(http) == []


OPEN_PDF = "https://repository.example.org/open.pdf"


@pytest.mark.parametrize(
    "work",
    [
        dict(
            OA_JOURNAL, best_oa_location={"pdf_url": OPEN_PDF, "landing_page_url": "x"}
        ),
        dict(
            OA_JOURNAL,
            best_oa_location=None,
            locations=[{"pdf_url": None}, {"pdf_url": OPEN_PDF}],
        ),
    ],
    ids=["best-location", "any-location"],
)
def test_text_finds_a_doi_open_access_pdf_through_openalex(cache, capsys, work):
    http = FakeHTTP(
        ("GET", "api.openalex.org/works/doi:10.1111/1467-937X.00136", [ok(work)]),
        ("GET", OPEN_PDF, [pdf_response()]),
    )
    code, out, err, _ = run(
        capsys,
        ["text", "https://doi.org/10.1111/1467-937X.00136"],
        http,
        extract=fake_extract,
    )
    assert code == 0, err
    assert Path(out.strip()).read_text() == "extracted from %PDF-1.4"
    assert openalex_calls(http)[0]["params"]["mailto"] == "robocarver@worldofmail.com"
    assert s2_calls(http) == []


def test_text_of_a_doi_answers_a_repeat_from_the_cache(cache, capsys):
    work = dict(OA_JOURNAL, best_oa_location={"pdf_url": OPEN_PDF})
    http = FakeHTTP(
        ("GET", "api.openalex.org/works/doi:", [ok(work)]),
        ("GET", OPEN_PDF, [pdf_response()]),
    )
    _, first, _, _ = run(
        capsys, ["text", "10.1111/1467-937X.00136"], http, extract=fake_extract
    )
    again = FakeHTTP()
    code, out, _, _ = run(
        capsys, ["text", "10.1111/1467-937X.00136"], again, extract=fake_extract
    )
    assert (code, out) == (0, first)
    assert again.calls == []


def test_text_of_a_doi_with_no_open_access_pdf_exits_1_naming_its_landing_page(
    cache, capsys
):
    http = FakeHTTP(("GET", "api.openalex.org/works/doi:", [ok(OA_JOURNAL)]))
    code, out, err, _ = run(
        capsys, ["text", "10.1111/1467-937X.00136"], http, extract=fake_extract
    )
    assert (code, out) == (1, "")
    assert "no open-access PDF" in err
    assert "https://doi.org/10.1111/1467-937x.00136" in err


def test_text_of_a_doi_openalex_doesnt_know_exits_1(cache, capsys):
    http = FakeHTTP(OPENALEX_HAS_NOTHING)
    code, out, err, _ = run(
        capsys, ["text", "10.1000/unknown"], http, extract=fake_extract
    )
    assert (code, out) == (1, "")
    assert "10.1000/unknown" in err


def test_outline_takes_a_doi_too(cache, capsys):
    work = dict(OA_JOURNAL, best_oa_location={"pdf_url": OPEN_PDF})
    http = FakeHTTP(
        ("GET", "api.openalex.org/works/doi:", [ok(work)]),
        ("GET", OPEN_PDF, [pdf_response()]),
    )

    def headed(pdf, txt):
        txt.write_text("1 Introduction\nbody\n")

    code, out, err, _ = run(
        capsys, ["outline", "10.1111/1467-937X.00136"], http, extract=headed
    )
    assert code == 0, err
    assert "Introduction" in out


def test_a_doi_with_no_pdf_names_its_primary_landing_page(cache, capsys):
    work = dict(
        OA_JOURNAL,
        best_oa_location=None,
        primary_location={"landing_page_url": "https://academic.example.org/article/1"},
        locations=[{"pdf_url": None, "landing_page_url": "https://elsewhere.example"}],
    )
    http = FakeHTTP(("GET", "api.openalex.org/works/doi:", [ok(work)]))
    code, _, err, _ = run(
        capsys, ["text", "10.1111/1467-937X.00136"], http, extract=fake_extract
    )
    assert code == 1
    assert "https://academic.example.org/article/1" in err


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_openalex_key_is_dropped_naming_where_it_came_from(
    cache, capsys, openalex_key_file, code
):
    openalex_key_file.write_text(OA_KEY)
    refused = status(code, {"error": "Unauthorized"})
    http = FakeHTTP(S2_DOWN, ("GET", "api.openalex.org", [refused, ok(OA_ATTENTION)]))
    exit_code, out, err, _ = run(capsys, ["meta", "1706.03762"], http)
    assert exit_code == 0, err
    first, second = openalex_calls(http)
    assert first["params"]["api_key"] == OA_KEY and "api_key" not in second["params"]
    assert str(openalex_key_file) in err
    assert OA_KEY not in out + err


def test_openalex_lists_ask_for_at_most_200(cache, capsys):
    http = FakeHTTP(
        S2_DOWN,
        OA_ATTENTION_ROUTE,
        ("GET", "api.openalex.org/works", [ok({"meta": {}, "results": [OA_CITING]})]),
    )
    run(capsys, ["cites", "1706.03762", "--limit", "500"], http)
    run(capsys, ["search", "collusion", "--limit", "300"], http)
    lists = [c for c in openalex_calls(http) if "per_page" in c["params"]]
    assert [c["params"]["per_page"] for c in lists] == [200, 200]


def test_the_openalex_key_never_shows_in_an_openalex_error(
    cache, capsys, openalex_key_file
):
    openalex_key_file.write_text(OA_KEY)
    echo = status(
        400, {"error": "bad request", "message": f"api_key={OA_KEY} rejected"}
    )
    http = FakeHTTP(S2_DOWN, ("GET", "api.openalex.org", [echo]))
    code, out, err, _ = run(capsys, ["search", "collusion"], http)
    assert code == 3
    assert "OpenAlex" in err and OA_KEY not in out + err


SICI = "10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-0"


def test_a_doi_with_reserved_characters_is_escaped_for_openalex(cache, capsys):
    work = dict(OA_JOURNAL, best_oa_location={"pdf_url": OPEN_PDF})
    http = FakeHTTP(
        ("GET", "api.openalex.org/works/doi:", [ok(work)]),
        ("GET", OPEN_PDF, [pdf_response()]),
    )
    code, out, err, _ = run(capsys, ["text", SICI], http, extract=fake_extract)
    assert code == 0, err
    (call,) = openalex_calls(http)
    assert call["url"].endswith(
        "/works/doi:10.1002/%28SICI%291097-4571%28199806%2949:8%3C693::AID-ASI4%3E3.0.CO%3B2-0"
    )
    assert re.fullmatch(r"doi-[0-9a-f]{12}-[a-z0-9._-]+\.txt", Path(out.strip()).name)


def test_the_openalex_key_never_reaches_arxiv_or_a_pdf_host(
    cache, capsys, openalex_key_file
):
    openalex_key_file.write_text(OA_KEY)
    work = dict(OA_JOURNAL, best_oa_location={"pdf_url": OPEN_PDF})
    http = FakeHTTP(
        S2_DOWN,
        OA_ATTENTION_ROUTE,
        ("GET", "export.arxiv.org", [ATTENTION_FEED]),
        ("GET", "api.openalex.org/works/doi:10.1111", [ok(work)]),
        ("GET", OPEN_PDF, [pdf_response()]),
    )
    run(capsys, ["meta", "1706.03762"], http)
    run(capsys, ["text", "10.1111/1467-937X.00136"], http, extract=fake_extract)
    others = [c for c in http.calls if "api.openalex.org" not in c["url"]]
    assert calls_to(http, "export.arxiv.org") and calls_to(http, OPEN_PDF)
    assert all(OA_KEY not in json.dumps(c) for c in others)


# The request log and paper stats


def logged(cache):
    path = cache / "requests.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def test_each_request_is_logged_with_its_outcome(cache, capsys):
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    run(capsys, ["meta", "1706.03762"], http)
    (s2,) = [line for line in logged(cache) if line["service"] == "Semantic Scholar"]
    assert s2["method"] == "GET"
    assert (
        s2["url"] == "https://api.semanticscholar.org/graph/v1/paper/arXiv:1706.03762"
    )
    assert (s2["status"], s2["attempts"], s2["backoff"]) == (200, 1, 0)
    assert {"time", "waited", "seconds"} <= set(s2)


def test_a_request_that_gave_up_is_logged_as_such(cache, capsys):
    http = FakeHTTP(S2_DOWN, OPENALEX_HAS_NOTHING)
    run(capsys, ["meta", "1706.03762"], http)
    s2 = [line for line in logged(cache) if line["service"] == "Semantic Scholar"]
    assert [(line["status"], line["attempts"]) for line in s2] == [("gave up", 6)]
    assert s2[0]["backoff"] > 0
    assert [
        line["status"] for line in logged(cache) if line["service"] == "OpenAlex"
    ] == [404]


def test_the_log_holds_no_parameters_keys_or_mailto(
    cache, capsys, key_file, openalex_key_file
):
    key_file.write_text(KEY)
    openalex_key_file.write_text(OA_KEY)
    run(capsys, ["meta", "1706.03762"], FakeHTTP(S2_DOWN, OA_ATTENTION_ROUTE))
    run(
        capsys,
        ["search", "secret query words"],
        FakeHTTP(S2_DOWN, OPENALEX_HAS_NOTHING),
    )
    text = (cache / "requests.jsonl").read_text()
    for secret in (KEY, OA_KEY, "mailto", "worldofmail", "secret query words", "?"):
        assert secret not in text


def test_a_log_that_cant_be_written_warns_once_and_changes_nothing(cache, capsys):
    (cache / "requests.jsonl").mkdir(parents=True)
    http = FakeHTTP(("POST", "/paper/batch", [ok([ATTENTION, ATTENTION])]))
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "2404.00806"], http)
    assert code == 0
    assert out.count("Attention is All you Need") == 2
    assert err.count("request log") == 1


def write_log(cache, *lines):
    cache.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (cache / "requests.jsonl").open("w") as f:
        for ago, service, status, attempts, backoff_s, waited in lines:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
            f.write(
                json.dumps(
                    {"time": stamp, "service": service, "method": "GET", "url": "u",
                     "status": status, "attempts": attempts, "backoff": backoff_s,
                     "waited": waited, "seconds": 1.0}
                ) + "\n"
            )  # fmt: skip


def test_stats_summarises_the_log_per_service(cache, capsys):
    write_log(
        cache,
        (60, "Semantic Scholar", 200, 1, 0, 0.2),
        (61, "Semantic Scholar", 200, 1, 0, 1.4),
        (62, "Semantic Scholar", 404, 1, 0, 2.5),
        (63, "Semantic Scholar", "gave up", 6, 80.0, 0.4),
        (3600, "OpenAlex", 200, 2, 3.0, 0.0),
        (90000, "Semantic Scholar", 200, 1, 0, 9.0),  # older than 24 hours
    )
    code, out, err, _ = run(capsys, ["stats"], FakeHTTP())
    assert code == 0, err
    s2 = next(line for line in out.splitlines() if line.startswith("Semantic Scholar"))
    assert "4 requests" in s2
    assert "2 ok, 1 not found, 1 gave up or 429, 0 other" in s2
    assert "5 extra attempts, 80s backoff" in s2
    assert "waited: median 0.9s, max 2.5s, 2 over 1s" in s2
    assert "busiest minute 4" in s2
    oa = next(line for line in out.splitlines() if line.startswith("OpenAlex"))
    assert "1 requests" in oa and "1 extra attempts, 3s backoff" in oa


def test_stats_with_nothing_logged_in_the_window_says_so(cache, capsys):
    write_log(cache, (90000, "arXiv", 200, 1, 0, 0.0))
    code, out, _, _ = run(capsys, ["stats", "--hours", "1"], FakeHTTP())
    assert code == 0
    assert out == "paper: no requests logged in the last 1 hours\n"


def test_the_busiest_minute_counts_a_60_second_window(cache, capsys):
    write_log(cache, (120, "arXiv", 200, 1, 0, 0.0), (60, "arXiv", 200, 1, 0, 0.0))
    _, out, _, _ = run(capsys, ["stats"], FakeHTTP())
    assert out.rstrip().endswith("busiest minute 1")


def test_the_log_drops_a_urls_query_and_userinfo(cache, capsys):
    url = "https://user:pw123@example.org/a/b.pdf?token=s3cret"
    run(
        capsys,
        ["text", url],
        FakeHTTP(("GET", "example.org", [pdf_response()])),
        extract=fake_extract,
    )
    (entry,) = logged(cache)
    assert entry["url"] == "https://example.org/a/b.pdf"
    text = (cache / "requests.jsonl").read_text()
    assert "pw123" not in text and "s3cret" not in text


def test_an_unexpected_fetch_error_is_still_logged(cache, capsys):
    http = FakeHTTP(("GET", "/paper/", [RuntimeError("boom")]))
    with pytest.raises(RuntimeError):
        run(capsys, ["meta", "1706.03762"], http)
    (entry,) = logged(cache)
    assert entry["status"] == "error: RuntimeError"


def test_stats_skips_lines_it_cant_read(cache, capsys):
    write_log(cache, (60, "arXiv", 200, 1, 0, 0.0))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with (cache / "requests.jsonl").open("a") as f:
        f.write(json.dumps({"time": stamp, "status": 200}) + "\n")
        f.write(json.dumps({"time": stamp, "service": "x", "attempts": "many"}) + "\n")
        f.write("not json\n")
    code, out, err, _ = run(capsys, ["stats"], FakeHTTP())
    assert code == 0, err
    assert [line.split(":")[0] for line in out.splitlines()] == ["arXiv"]


# meta through POST /paper/batch


def batch_route(*answers):
    return ("POST", "/paper/batch", list(answers))


def test_meta_of_several_ids_sends_one_batch(cache, capsys):
    http = FakeHTTP(batch_route(ok([ATTENTION, COLLUSION])))
    code, out, err, _ = run(capsys, ["meta", "1706.03762v5", S2_ID], http)
    assert code == 0, err
    titles = [line for line in out.splitlines() if not line.startswith(" ")]
    assert titles == [ATTENTION["title"], COLLUSION["title"]]
    (call,) = s2_calls(http)
    assert call["method"] == "POST"
    assert call["json"] == {"ids": ["arXiv:1706.03762", S2_ID]}
    assert call["params"] == {"fields": paper.META_FIELDS}


def test_batch_answers_serve_later_single_lookups(cache, capsys):
    run(
        capsys,
        ["meta", "1706.03762", S2_ID],
        FakeHTTP(batch_route(ok([ATTENTION, COLLUSION]))),
    )
    again = FakeHTTP()
    code, out, _, _ = run(capsys, ["meta", S2_ID], again)
    assert code == 0 and COLLUSION["title"] in out
    assert s2_calls(again) == []


def test_a_cached_id_stays_out_of_the_batch(cache, capsys):
    run(
        capsys,
        ["meta", "1706.03762"],
        FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)])),
    )
    other = dict(
        ATTENTION, paperId="f" * 40, title="Other", externalIds={"ArXiv": "2404.00806"}
    )
    http = FakeHTTP(batch_route(ok([COLLUSION, other])))
    code, out, err, _ = run(capsys, ["meta", "1706.03762", S2_ID, "2404.00806"], http)
    assert code == 0, err
    (call,) = s2_calls(http)
    assert call["json"] == {"ids": [S2_ID, "arXiv:2404.00806"]}
    titles = [line for line in out.splitlines() if not line.startswith(" ")]
    assert titles == [ATTENTION["title"], COLLUSION["title"], "Other"]


def test_a_single_cache_miss_still_uses_a_get(cache, capsys):
    run(
        capsys,
        ["meta", "1706.03762"],
        FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)])),
    )
    http = FakeHTTP(("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]))
    code, _, err, _ = run(capsys, ["meta", "1706.03762", S2_ID], http)
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["GET"]


def test_a_null_in_the_batch_is_not_found_for_that_id(cache, capsys):
    http = FakeHTTP(
        batch_route(ok([ATTENTION, None])), OPENALEX_HAS_NOTHING, CROSSREF_HAS_NOTHING
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "10.1000/missing"], http)
    assert code == 1
    assert out.startswith(ATTENTION["title"] + "\n")
    assert "not found: 10.1000/missing" in err
    assert len(s2_calls(http)) == 1


def test_a_batch_refused_with_400_falls_back_to_single_lookups(cache, capsys):
    http = FakeHTTP(
        batch_route(status(400, {"error": "bad ids"})),
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762", S2_ID], http)
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["POST", "GET", "GET"]
    assert COLLUSION["title"] in out


def test_a_throttled_batch_falls_back_to_openalex_for_each_id(cache, capsys):
    http = FakeHTTP(
        ("POST", "semanticscholar.org", [status(429, RATE_LIMITED)]),
        OA_ATTENTION_ROUTE,
        ("GET", "api.openalex.org/works/doi:10.1111/1467-937X.00136", [ok(OA_JOURNAL)]),
    )
    code, out, err, _ = run(
        capsys, ["meta", "1706.03762", "10.1111/1467-937X.00136"], http
    )
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["POST"] * 6
    assert out.count("  " + paper.SOURCE_NOTE) == 2


def test_batches_hold_at_most_500_ids(cache, capsys):
    ids = [f"{n:040x}" for n in range(1, 502)]

    def answer(call):
        return ok(
            [
                dict(COLLUSION, paperId=i, title=f"Paper {i[-3:]}")
                for i in call["json"]["ids"]
            ]
        )

    http = FakeHTTP(batch_route(answer))
    code, out, err, _ = run(capsys, ["meta", *ids], http)
    assert code == 0, err
    assert [len(c["json"]["ids"]) for c in s2_calls(http)] == [500, 1]
    assert out.count("\nPaper ") + out.startswith("Paper ") == 501


def test_a_batch_answer_of_the_wrong_length_sends_the_ids_singly(cache, capsys):
    http = FakeHTTP(
        batch_route(ok([ATTENTION])),
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]),
    )
    code, _, err, _ = run(capsys, ["meta", "1706.03762", S2_ID], http)
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["POST", "GET", "GET"]


def test_a_batch_is_logged_with_its_id_count(cache, capsys):
    run(
        capsys,
        ["meta", "1706.03762", S2_ID],
        FakeHTTP(batch_route(ok([ATTENTION, COLLUSION]))),
    )
    (line,) = [
        entry for entry in logged(cache) if entry["service"] == "Semantic Scholar"
    ]
    assert (line["method"], line["ids"]) == ("POST", 2)


def test_a_failed_chunk_keeps_what_the_chunks_before_it_found(cache, capsys):
    ids = [f"{n:040x}" for n in range(1, 502)]

    def answer(call):
        asked = call["json"]["ids"]
        if len(asked) < 500:
            return status(403, {"error": "forbidden"})
        found = [dict(COLLUSION, paperId=i, title=f"Paper {i[-3:]}") for i in asked]
        found[1] = None  # the second id is unknown
        return ok(found)

    http = FakeHTTP(
        batch_route(answer),
        ("GET", f"/paper/{ids[-1]}", [ok(dict(COLLUSION, paperId=ids[-1]))]),
    )
    code, _, err, _ = run(capsys, ["meta", *ids], http)
    assert code == 1
    assert f"not found: {ids[1]}" in err
    assert [
        c["url"].rsplit("/", 1)[-1] for c in s2_calls(http) if c["method"] == "GET"
    ] == [ids[-1]]


def test_a_refused_batch_still_prints_the_cached_blocks_first(cache, capsys):
    run(
        capsys,
        ["meta", "1706.03762"],
        FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)])),
    )
    http = FakeHTTP(
        batch_route(status(403, {"error": "forbidden"})),
        ("GET", "/paper/arXiv:2404.00806", [status(403, {"error": "forbidden"})]),
    )
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "2404.00806", S2_ID], http)
    assert code == 3
    assert out.startswith(ATTENTION["title"] + "\n")
    assert "forbidden" in err


def test_a_batch_element_that_isnt_a_paper_sends_the_ids_singly(cache, capsys):
    http = FakeHTTP(
        batch_route(ok([ATTENTION, {"unexpected": True}])),
        ("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]),
        ("GET", f"/paper/{S2_ID}", [ok(COLLUSION)]),
    )
    code, _, err, _ = run(capsys, ["meta", "1706.03762", S2_ID], http)
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["POST", "GET", "GET"]


def test_a_duplicate_id_is_asked_for_once_and_printed_each_time(cache, capsys):
    http = FakeHTTP(batch_route(ok([ATTENTION, COLLUSION])))
    code, out, err, _ = run(capsys, ["meta", "1706.03762", "1706.03762v2", S2_ID], http)
    assert code == 0, err
    (call,) = s2_calls(http)
    assert call["json"] == {"ids": ["arXiv:1706.03762", S2_ID]}
    assert out.count(ATTENTION["title"]) == 2


# meta when Semantic Scholar doesn't know the paper: OpenAlex, then Crossref

NASH_DOI = "10.2307/1969529"
S2_DOESNT_KNOW = (
    "GET",
    "semanticscholar.org",
    [status(404, {"error": "Paper not found"})],
)
OA_NASH = dict(
    OA_JOURNAL,
    id="https://openalex.org/W1980209624",
    doi="https://doi.org/10.2307/1969529",
    display_name="Non-Cooperative Games",
    publication_year=1951,
    cited_by_count=5200,
    authorships=[
        {
            "author": {"display_name": "John Nash"},
            "institutions": [{"display_name": "Princeton University"}],
        }
    ],
    primary_location={"source": {"display_name": "Annals of Mathematics"}},
)
OA_NASH_ROUTE = ("GET", "api.openalex.org/works/doi:10.2307/1969529", [ok(OA_NASH)])
CROSSREF_NASH = {
    "status": "ok",
    "message-type": "work",
    "message": {
        "DOI": "10.2307/1969529",
        "title": ["Non-Cooperative Games"],
        "author": [
            {"given": "John", "family": "Nash", "sequence": "first", "affiliation": []}
        ],
        "container-title": ["The Annals of Mathematics"],
        "issued": {"date-parts": [[1951, 9]]},
        "is-referenced-by-count": 6600,
        "page": "286",
        "volume": "54",
        "type": "journal-article",
    },
}
CROSSREF_NASH_ROUTE = (
    "GET",
    "api.crossref.org/works/10.2307/1969529",
    [ok(CROSSREF_NASH)],
)
OPENALEX_FAILS = ("GET", "api.openalex.org", [status(503)])


def crossref_calls(http):
    return calls_to(http, "api.crossref.org")


def test_meta_asks_openalex_for_a_doi_semantic_scholar_doesnt_know(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OA_NASH_ROUTE)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert code == 0, err
    assert out == (
        "Non-Cooperative Games\n"
        "  source: OpenAlex, because Semantic Scholar doesn't know this paper.\n"
        "  1951 · Annals of Mathematics\n"
        "  authors: John Nash (Princeton University)\n"
        "  citations: 5200\n"
        "  ids: DOI:10.2307/1969529 OpenAlex:W1980209624\n"
    )
    assert err == ""
    assert crossref_calls(http) == []


def test_meta_asks_crossref_when_openalex_doesnt_know_the_doi_either(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_NASH_ROUTE)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert code == 0, err
    assert out == (
        "Non-Cooperative Games\n"
        "  source: Crossref, because Semantic Scholar doesn't know this paper and"
        " OpenAlex doesn't either. Crossref counts only the citations publishers"
        " deposit with it.\n"
        "  1951 · The Annals of Mathematics\n"
        "  authors: John Nash\n"
        "  citations: 6600\n"
        "  ids: DOI:10.2307/1969529\n"
    )
    (call,) = crossref_calls(http)
    assert call["params"]["mailto"] == "robocarver@worldofmail.com"


def test_a_doi_missing_from_the_batch_falls_back_too(cache, capsys):
    http = FakeHTTP(batch_route(ok([ATTENTION, None])), OA_NASH_ROUTE)
    code, out, err, _ = run(capsys, ["meta", "1706.03762", NASH_DOI], http)
    assert code == 0, err
    titles = [line for line in out.splitlines() if not line.startswith(" ")]
    assert titles == [ATTENTION["title"], "Non-Cooperative Games"]
    assert len(s2_calls(http)) == 1


def test_meta_says_not_found_only_when_every_source_does(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_HAS_NOTHING)
    code, out, err, _ = run(capsys, ["meta", "10.1080/1386979032000146807"], http)
    assert (code, out) == (1, "")
    assert err == "paper: not found: 10.1080/1386979032000146807\n"
    assert len(openalex_calls(http)) == len(crossref_calls(http)) == 1


def test_an_arxiv_id_semantic_scholar_doesnt_know_asks_openalex_not_crossref(
    cache, capsys
):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING)
    code, _, err, _ = run(capsys, ["meta", "2404.00806"], http)
    assert code == 1
    assert "not found: 2404.00806" in err
    (call,) = openalex_calls(http)
    assert "/works/doi:10.48550/arXiv.2404.00806" in call["url"]
    assert crossref_calls(http) == []


def test_crossref_still_answers_when_openalex_fails(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_FAILS, CROSSREF_NASH_ROUTE)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert code == 0, err
    assert out.splitlines()[1].startswith(
        "  source: Crossref, because Semantic Scholar doesn't know this paper and"
        " OpenAlex failed."
    )


def test_a_failed_fallback_is_not_reported_as_not_found(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_FAILS, CROSSREF_HAS_NOTHING)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert (code, out) == (3, "")
    assert "not found" not in err
    assert "Semantic Scholar doesn't know" in err and "OpenAlex" in err


def test_help_says_meta_asks_openalex_then_crossref_for_unknown_papers(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Semantic Scholar doesn't know a paper" in out
    assert "then Crossref" in out
    assert "Crossref's and doi.org's at least 0.2 seconds" in out


def test_a_crossref_answer_is_paced_logged_and_cached(cache, capsys):
    paces = []
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_NASH_ROUTE)
    code, first, err, _ = run(capsys, ["meta", NASH_DOI], http, paces=paces)
    assert code == 0, err
    assert paces == ["Semantic Scholar", "OpenAlex", "Crossref"]
    assert "Crossref" in {line["service"] for line in logged(cache)}
    again = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING)
    assert run(capsys, ["meta", NASH_DOI], again)[:2] == (0, first)
    assert crossref_calls(again) == []


@pytest.mark.parametrize(
    "openalex, why",
    [(OPENALEX_HAS_NOTHING, "doesn't know it"), (OPENALEX_FAILS, "failed")],
    ids=["openalex-doesnt-know", "openalex-fails"],
)
def test_meta_asks_crossref_when_semantic_scholar_gives_up_and_openalex_cant_answer(
    cache, capsys, openalex, why
):
    http = FakeHTTP(S2_DOWN, openalex, CROSSREF_NASH_ROUTE)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert code == 0, err
    title, source, *rest = out.splitlines()
    assert title == "Non-Cooperative Games"
    assert source == (
        "  source: Crossref, because Semantic Scholar gave up after its retries and"
        f" OpenAlex {why}. Crossref counts only the citations publishers deposit with"
        " it."
    )
    assert rest[0] == "  1951 · The Annals of Mathematics"
    assert "; answering from Crossref" in err
    assert "answering from OpenAlex" not in err


def test_help_says_meta_asks_crossref_after_a_give_up_too(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "then Crossref for a DOI that OpenAlex doesn't have" in out


def test_when_semantic_scholar_gives_up_and_no_one_has_the_doi_meta_exits_3(
    cache, capsys
):
    http = FakeHTTP(S2_DOWN, OPENALEX_HAS_NOTHING, CROSSREF_HAS_NOTHING)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert (code, out) == (3, "")
    assert "gave up" in err
    assert "OpenAlex has no record of it" in err
    assert "Crossref has no record of it" in err


def test_a_crossref_answer_without_a_work_exits_3_and_caches_nothing(cache, capsys):
    odd = ("GET", "api.crossref.org", [ok({"status": "ok", "message": ["no work"]})])
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, odd)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert (code, out) == (3, "")
    assert "Crossref sent an unexpected answer" in err
    sound = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_NASH_ROUTE)
    assert run(capsys, ["meta", NASH_DOI], sound)[0] == 0


def test_crossref_failing_for_a_doi_nobody_else_knows_exits_3_naming_it(cache, capsys):
    crossref_down = ("GET", "api.crossref.org", [status(503)])
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, crossref_down)
    code, out, err, _ = run(capsys, ["meta", NASH_DOI], http)
    assert (code, out) == (3, "")
    assert "not found" not in err
    assert "Crossref failed" in err


def test_an_arxiv_doi_nobody_knows_is_not_sent_to_crossref(cache, capsys):
    http = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_HAS_NOTHING)
    code, _, err, _ = run(capsys, ["meta", "10.48550/arXiv.2404.00806"], http)
    assert code == 1
    assert "not found: 10.48550/arXiv.2404.00806" in err
    assert len(openalex_calls(http)) == 1
    assert crossref_calls(http) == []


def test_crossref_answers_are_cached_whatever_the_dois_case(cache, capsys):
    work = {"message": dict(CROSSREF_NASH["message"], DOI="10.1111/1467-937X.00136")}
    http = FakeHTTP(
        S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, ("GET", "api.crossref.org", [ok(work)])
    )
    code, first, err, _ = run(capsys, ["meta", "10.1111/1467-937X.00136"], http)
    assert code == 0, err
    again = FakeHTTP(S2_DOESNT_KNOW, OPENALEX_HAS_NOTHING, CROSSREF_HAS_NOTHING)
    assert run(capsys, ["meta", "10.1111/1467-937x.00136"], again)[:2] == (0, first)
    assert crossref_calls(again) == []


# paper bib: BibTeX from doi.org's content negotiation

NASH_BIB = (
    " @article{Nash_1951, title={Non-Cooperative Games}, volume={54},"
    " ISSN={0003-486X}, url={http://dx.doi.org/10.2307/1969529},"
    " DOI={10.2307/1969529}, number={2}, journal={The Annals of Mathematics},"
    " publisher={JSTOR}, author={Nash, John}, year={1951}, month=Sept, pages={286} }"
)
ATTENTION_BIB = """\
@misc{https://doi.org/10.48550/arxiv.1706.03762,
  doi = {10.48550/ARXIV.1706.03762},
  url = {https://arxiv.org/abs/1706.03762},
  author = {Vaswani, Ashish and Shazeer, Noam},
  title = {Attention Is All You Need},
  publisher = {arXiv},
  year = {2017},
  copyright = {arXiv.org perpetual, non-exclusive license}
}"""
SEQUENTIAL_BIB = (
    "@article{Kreps_1982, title={Sequential Equilibria},"
    " author={Kreps, David M. and Wilson, Robert}, year={1982}}"
)
REPUTATION_BIB = (
    "@article{Kreps_1982, title={Reputation and imperfect information},"
    " author={Kreps, David M and Wilson, Robert}, year={1982}}"
)
DOI_NOT_FOUND = paper.Response(
    404, {"Content-Type": "text/html"}, b"<title>Error: DOI Not Found</title>"
)


def bibtex(text):
    return paper.Response(200, {"Content-Type": "application/x-bibtex"}, text.encode())


def bib_route(doi, text):
    return ("GET", "https://doi.org/" + doi, [bibtex(text)])


def test_bib_prints_a_dois_entry_keyed_by_family_name_and_year(cache, capsys):
    http = FakeHTTP(bib_route(NASH_DOI, NASH_BIB))
    code, out, err, _ = run(capsys, ["bib", NASH_DOI], http)
    assert code == 0, err
    assert out == (
        "@article{nash1951, title={Non-Cooperative Games}, volume={54},"
        " ISSN={0003-486X}, url={http://dx.doi.org/10.2307/1969529},"
        " DOI={10.2307/1969529}, number={2}, journal={The Annals of Mathematics},"
        " publisher={JSTOR}, author={Nash, John}, year={1951}, month=Sept,"
        " pages={286} }\n"
    )
    (call,) = http.calls
    assert call["url"] == "https://doi.org/10.2307/1969529"
    assert call["headers"]["Accept"] == "application/x-bibtex"
    assert "mailto:robocarver@worldofmail.com" in call["headers"]["User-Agent"]


def test_bib_of_an_arxiv_id_asks_doi_org_for_its_datacite_doi(cache, capsys):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD, bib_route("10.48550/arXiv.1706.03762", ATTENTION_BIB)
    )
    code, out, err, _ = run(capsys, ["bib", "https://arxiv.org/abs/1706.03762v5"], http)
    assert code == 0, err
    first, *rest = out.splitlines()
    assert first == "@misc{vaswani2017,"
    assert rest == ATTENTION_BIB.splitlines()[1:]
    (call,) = calls_to(http, "doi.org")
    assert call["url"] == "https://doi.org/10.48550/arXiv.1706.03762"


ATTENTION_LATEST = (
    "1706.03762v7",
    "2017-06-12",
    "2023-08-02",
    "Attention Is All\n  You Need",
    ("Ashish Vaswani", "Noam Shazeer"),
)
SIMILARITY_V1_BIB = """\
@misc{https://doi.org/10.48550/arxiv.2211.14468,
  doi = {10.48550/ARXIV.2211.14468},
  author = {Oesterheld, Caspar and Treutlein, Johannes},
  title = {Similarity-based Cooperation},
  publisher = {arXiv},
  year = {2022}
}"""
SIMILARITY_LATEST = (
    "2211.14468v2",
    "2022-11-26",
    "2023-11-12",
    "Similarity-based cooperative equilibrium",
    ("Caspar Oesterheld", "Johannes Treutlein", "Roger Grosse"),
)
ATTENTION_BIB_ROUTE = bib_route("10.48550/arXiv.1706.03762", ATTENTION_BIB)
# Neither Semantic Scholar (single lookups or a batch) nor OpenAlex knows the paper.
NO_VERSION_OF_RECORD = (
    S2_DOESNT_KNOW,
    batch_route(lambda call: ok([None] * len(call["json"]["ids"]))),
    OPENALEX_HAS_NOTHING,
)


def arxiv_route(*entries):
    return ("GET", "export.arxiv.org", [arxiv_feed(*entries)])


def test_bib_of_an_arxiv_preprint_gives_its_latest_version(cache, capsys):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD, ATTENTION_BIB_ROUTE, arxiv_route(ATTENTION_LATEST)
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762v5"], http)
    assert code == 0, err
    assert out == (
        "@misc{vaswani2017,\n"
        "  doi = {10.48550/ARXIV.1706.03762},\n"
        "  url = {https://arxiv.org/abs/1706.03762},\n"
        "  author = {Vaswani, Ashish and Shazeer, Noam},\n"
        "  title = {Attention Is All You Need},\n"
        "  publisher = {arXiv},\n"
        "  year = {2017},\n"
        "  copyright = {arXiv.org perpetual, non-exclusive license},\n"
        "  version = {7}\n"
        "}\n"
    )
    assert err == "paper: 1706.03762 is at v7 (2023-08-02); the entry's year is v1's.\n"


def test_bib_takes_title_and_authors_from_arxivs_latest_version(cache, capsys):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD,
        bib_route("10.48550/arXiv.2211.14468", SIMILARITY_V1_BIB),
        arxiv_route(SIMILARITY_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out == (
        "@misc{oesterheld2022,\n"
        "  doi = {10.48550/ARXIV.2211.14468},\n"
        "  author = {Oesterheld, Caspar and Treutlein, Johannes and Grosse, Roger},\n"
        "  title = {Similarity-based cooperative equilibrium},\n"
        "  publisher = {arXiv},\n"
        "  year = {2022},\n"
        "  version = {2}\n"
        "}\n"
    )
    assert err == "paper: 2211.14468 is at v2 (2023-11-12); the entry's year is v1's.\n"


def test_bib_keeps_datacites_names_and_writes_a_name_it_cant_split_as_arxiv_has_it(
    cache, capsys
):
    datacite = SIMILARITY_V1_BIB.replace(
        "Oesterheld, Caspar and Treutlein, Johannes", "Bengio, Yoshua and LeCun, Yann"
    )
    latest = (*SIMILARITY_LATEST[:3], SIMILARITY_LATEST[3]) + (
        ("Yoshua Bengio", "Yann Le Cun", "Geoffrey Hinton"),
    )
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD,
        bib_route("10.48550/arXiv.2211.14468", datacite),
        arxiv_route(latest),
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert "  author = {Bengio, Yoshua and Yann Le Cun and Hinton, Geoffrey},\n" in out
    assert out.startswith("@misc{bengio2022,")


def test_bib_asks_arxiv_once_for_every_preprint_in_the_call(cache, capsys):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD,
        ATTENTION_BIB_ROUTE,
        bib_route("10.48550/arXiv.2211.14468", SIMILARITY_V1_BIB),
        arxiv_route(ATTENTION_LATEST, SIMILARITY_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762", "2211.14468"], http)
    assert code == 0, err
    (call,) = arxiv_calls(http)
    assert call["params"]["id_list"] == "1706.03762,2211.14468"
    assert out.count("version = {") == 2


def test_bib_takes_an_arxiv_doi_as_its_arxiv_id(cache, capsys):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD, ATTENTION_BIB_ROUTE, arxiv_route(ATTENTION_LATEST)
    )
    code, out, err, _ = run(capsys, ["bib", "10.48550/arXiv.1706.03762"], http)
    assert code == 0, err
    assert "  version = {7}\n" in out


@pytest.mark.parametrize(
    "answer, warning",
    [
        (status(503), "arXiv's latest versions unavailable"),
        (arxiv_feed(), "arXiv has no entry for 1706.03762"),
    ],
    ids=["arxiv-fails", "arxiv-has-no-entry"],
)
def test_bib_without_arxivs_latest_version_prints_datacites_entry_and_warns(
    cache, capsys, answer, warning
):
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD,
        ATTENTION_BIB_ROUTE,
        ("GET", "export.arxiv.org", [answer]),
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762"], http)
    assert code == 0, err
    assert out.splitlines() == [
        "@misc{vaswani2017,",
        *ATTENTION_BIB.splitlines()[1:],
    ]
    assert warning in err


def test_arxiv_versions_cached_without_title_and_authors_are_fetched_again(
    cache, capsys
):
    old = {"version": 7, "submitted": "2017-06-12", "updated": "2023-08-02"}
    (cache / "arxiv").mkdir(parents=True)
    (cache / "arxiv" / "1706.03762.json").write_text(json.dumps(old))
    http = FakeHTTP(
        *NO_VERSION_OF_RECORD, ATTENTION_BIB_ROUTE, arxiv_route(ATTENTION_LATEST)
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762"], http)
    assert code == 0, err
    assert len(arxiv_calls(http)) == 1
    assert "  version = {7}\n" in out


# bib: an arXiv paper's version of record first

S2_SIMILARITY = {  # Semantic Scholar knows the venue but no DOI
    "paperId": "435dbef1d60cf2bf282b2b139703fd1609ad0a81",
    "title": "Similarity-based cooperative equilibrium",
    "year": 2022,
    "venue": "Neural Information Processing Systems",
    "citationCount": 9,
    "externalIds": {"ArXiv": "2211.14468", "DBLP": "conf/nips/OesterheldTGCF23"},
    "authors": [{"name": "Caspar Oesterheld"}],
}
NEURIPS_DOI = "10.52202/075280-1063"
OA_SIMILARITY = {
    "id": "https://openalex.org/W4310000000",
    "doi": "https://doi.org/10.52202/075280-1063",
    "display_name": "Similarity-based cooperative equilibrium",
    "type": "conference-paper",
    "primary_location": {
        "source": {
            "display_name": "neural information processing systems",
            "type": "conference",
        },
        "landing_page_url": "https://doi.org/10.52202/075280-1063",
        "version": "publishedVersion",
    },
}
OA_ATTENTION_REDEPOSIT = {  # OpenAlex's doi for 1706.03762: a 2025 re-deposit
    "id": "https://openalex.org/W2626778328",
    "doi": "https://doi.org/10.65215/2q58a426",
    "display_name": "Attention Is All You Need",
    "type": "preprint",
    "primary_location": {
        "source": None,
        "landing_page_url": "https://doi.org/10.65215/2q58a426",
        "version": "acceptedVersion",
    },
}
NEURIPS_BIB = (
    " @inproceedings{Oesterheld_2023, series={NeurIPS 2023},"
    " title={Similarity-based cooperative equilibrium},"
    " DOI={10.52202/075280-1063}, author={Oesterheld, Caspar and Treutlein,"
    " Johannes}, year={2023}, pages={24434–24465}, collection={NeurIPS 2023} }"
)
S2_SIMILARITY_ROUTE = ("GET", "/paper/arXiv:2211.14468", [ok(S2_SIMILARITY)])
OA_SIMILARITY_ROUTE = (
    "GET",
    "api.openalex.org/works/doi:10.48550/arXiv.2211.14468",
    [ok(OA_SIMILARITY)],
)
NEURIPS_BIB_ROUTE = bib_route(NEURIPS_DOI, NEURIPS_BIB)
SIMILARITY_LATEST_ROUTE = arxiv_route(SIMILARITY_LATEST)
SIMILARITY_PREPRINT_ROUTES = (
    bib_route("10.48550/arXiv.2211.14468", SIMILARITY_V1_BIB),
    SIMILARITY_LATEST_ROUTE,
)
PUBLISHED_NOTE = (
    "paper: 2211.14468 was published as 10.52202/075280-1063"
    " (neural information processing systems); printed that."
    " --preprint prints the arXiv entry.\n"
)


def test_bib_prints_the_version_of_record_openalex_has_for_an_arxiv_id(cache, capsys):
    http = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        OA_SIMILARITY_ROUTE,
        NEURIPS_BIB_ROUTE,
        SIMILARITY_LATEST_ROUTE,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468v1"], http)
    assert code == 0, err
    assert out == (
        "@inproceedings{oesterheld2023, series={NeurIPS 2023},"
        " title={Similarity-based cooperative equilibrium},"
        " DOI={10.52202/075280-1063}, author={Oesterheld, Caspar and Treutlein,"
        " Johannes}, year={2023}, pages={24434–24465}, collection={NeurIPS 2023},"
        " eprint={2211.14468}, archiveprefix={arXiv} }\n"
    )
    assert err == PUBLISHED_NOTE
    assert [c["url"] for c in calls_to(http, "doi.org/")] == [
        "https://doi.org/10.52202/075280-1063"
    ]


def test_bib_takes_semantic_scholars_doi_for_a_published_arxiv_paper(cache, capsys):
    published = dict(
        S2_SIMILARITY,
        venue="Advances in Neural Information Processing Systems",
        externalIds={"ArXiv": "2211.14468", "DOI": NEURIPS_DOI},
    )
    http = FakeHTTP(
        ("GET", "/paper/arXiv:2211.14468", [ok(published)]),
        NEURIPS_BIB_ROUTE,
        SIMILARITY_LATEST_ROUTE,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@inproceedings{oesterheld2023,")
    assert "(Advances in Neural Information Processing Systems)" in err
    assert openalex_calls(http) == []


def test_bib_passes_over_a_doi_that_isnt_a_published_version(cache, capsys):
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(dict(ATTENTION, externalIds={}))]),
        ("GET", "api.openalex.org", [ok(OA_ATTENTION_REDEPOSIT)]),
        ATTENTION_BIB_ROUTE,
        arxiv_route(ATTENTION_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762"], http)
    assert code == 0, err
    assert out.startswith("@misc{vaswani2017,") and "  version = {7}\n" in out
    assert "10.65215" not in out + err
    assert [c["url"] for c in calls_to(http, "doi.org/")] == [
        "https://doi.org/10.48550/arXiv.1706.03762"
    ]


def test_preprint_prints_the_arxiv_entry_without_looking_for_a_published_one(
    cache, capsys
):
    http = FakeHTTP(
        bib_route("10.48550/arXiv.2211.14468", SIMILARITY_V1_BIB),
        arxiv_route(SIMILARITY_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "--preprint", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@misc{oesterheld2022,") and "version = {2}" in out
    assert s2_calls(http) == [] and openalex_calls(http) == []


@pytest.mark.parametrize(
    "s2", [S2_DOESNT_KNOW, S2_DOWN], ids=["s2-doesnt-know", "s2-gives-up"]
)
def test_bib_asks_openalex_for_a_published_version_without_semantic_scholar(
    cache, capsys, s2
):
    http = FakeHTTP(s2, OA_SIMILARITY_ROUTE, NEURIPS_BIB_ROUTE, SIMILARITY_LATEST_ROUTE)
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@inproceedings{oesterheld2023,")
    assert PUBLISHED_NOTE in err


def test_bib_looks_up_published_versions_in_one_semantic_scholar_batch(cache, capsys):
    http = FakeHTTP(
        batch_route(ok([S2_SIMILARITY, dict(ATTENTION, externalIds={})])),
        OA_SIMILARITY_ROUTE,
        ("GET", "api.openalex.org/works/doi:10.48550/arXiv.1706", [ok(OA_ATTENTION)]),
        NEURIPS_BIB_ROUTE,
        ATTENTION_BIB_ROUTE,
        arxiv_route(SIMILARITY_LATEST, ATTENTION_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468", "1706.03762"], http)
    assert code == 0, err
    assert [c["method"] for c in s2_calls(http)] == ["POST"]
    keys = [entry.split(",", 1)[0] for entry in out.split("\n\n")]
    assert keys == ["@inproceedings{oesterheld2023", "@misc{vaswani2017"]
    (call,) = arxiv_calls(http)
    assert call["params"]["id_list"] == "2211.14468,1706.03762"


def test_a_published_version_without_bibtex_falls_back_to_the_arxiv_entry(
    cache, capsys
):
    http = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        OA_SIMILARITY_ROUTE,
        ("GET", "https://doi.org/10.52202", [DOI_NOT_FOUND]),
        bib_route("10.48550/arXiv.2211.14468", SIMILARITY_V1_BIB),
        arxiv_route(SIMILARITY_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@misc{oesterheld2022,") and "version = {2}" in out
    assert "10.52202/075280-1063" in err and "printed the arXiv entry" in err


def test_a_failed_lookup_stops_bib_after_the_entries_before_it(cache, capsys):
    http = FakeHTTP(
        bib_route(NASH_DOI, NASH_BIB),
        S2_DOESNT_KNOW,
        OPENALEX_FAILS,
    )
    code, out, err, _ = run(capsys, ["bib", NASH_DOI, "2211.14468"], http)
    assert code == 3
    assert out.startswith("@article{nash1951,")
    assert "OpenAlex" in err
    assert "while looking for 2211.14468's version of record" in err
    assert "--preprint skips this" in err


def test_openalex_doi_counts_only_from_its_own_published_page(cache, capsys):
    proceedings = {
        "source": {
            "display_name": "Neural Information Processing Systems",
            "type": "conference",
        },
        "landing_page_url": "https://proceedings.neurips.cc/paper/2017/hash/3f5ee.html",
        "version": "publishedVersion",
    }
    work = dict(OA_ATTENTION_REDEPOSIT, primary_location=proceedings, locations=[])
    http = FakeHTTP(
        ("GET", "/paper/arXiv:1706.03762", [ok(dict(ATTENTION, externalIds={}))]),
        ("GET", "api.openalex.org", [ok(work)]),
        ATTENTION_BIB_ROUTE,
        arxiv_route(ATTENTION_LATEST),
    )
    code, out, err, _ = run(capsys, ["bib", "1706.03762"], http)
    assert code == 0, err
    assert out.startswith("@misc{vaswani2017,")
    assert "10.65215" not in out + err


def test_openalex_doi_counts_from_any_location_that_is_its_page(cache, capsys):
    arxiv_page = {
        "source": {"display_name": "arXiv (Cornell University)", "type": "repository"},
        "landing_page_url": "http://arxiv.org/abs/2211.14468",
        "version": "submittedVersion",
    }
    doi_page = dict(
        OA_SIMILARITY["primary_location"],
        landing_page_url="HTTPS://DOI.ORG/10.52202/075280-1063",
    )
    work = dict(OA_SIMILARITY, primary_location=arxiv_page, locations=[doi_page])
    http = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        ("GET", "api.openalex.org", [ok(work)]),
        NEURIPS_BIB_ROUTE,
        SIMILARITY_LATEST_ROUTE,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@inproceedings{oesterheld2023,")
    assert err == PUBLISHED_NOTE


@pytest.mark.parametrize("kind", [None, "book series"])
def test_openalex_doi_counts_only_at_a_journal_or_conference(cache, capsys, kind):
    location = dict(
        OA_SIMILARITY["primary_location"],
        source={"display_name": "Some Source", "type": kind},
    )
    http = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        (
            "GET",
            "api.openalex.org",
            [ok(dict(OA_SIMILARITY, primary_location=location))],
        ),
        *SIMILARITY_PREPRINT_ROUTES,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@misc{oesterheld2022,")


def test_semantic_scholars_doi_doesnt_count_at_an_arxiv_venue(cache, capsys):
    junk = dict(
        S2_SIMILARITY,
        venue="arXiv.org",
        externalIds={"ArXiv": "2211.14468", "DOI": "10.65215/2q58a426"},
    )
    http = FakeHTTP(
        ("GET", "/paper/arXiv:2211.14468", [ok(junk)]),
        OPENALEX_HAS_NOTHING,
        *SIMILARITY_PREPRINT_ROUTES,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@misc{oesterheld2022,")
    assert "10.65215" not in out + err


@pytest.mark.parametrize(
    "entry, why",
    [
        (
            (
                "@article{X, title={Something Else}, author={Oesterheld, Caspar},"
                " year={2023}}"
            ),
            "its title \"Something Else\" isn't arXiv's",
        ),
        (
            (
                "@article{X, title={Similarity-Based  Cooperative Equilibrium},"
                " author={Smith, Jane and Oesterheld, Caspar}, year={2023}}"
            ),
            "its first author Smith isn't arXiv's Oesterheld",
        ),
    ],
    ids=["title", "first-author"],
)
def test_a_version_of_record_unlike_arxivs_latest_version_is_rejected(
    cache, capsys, entry, why
):
    record = dict(
        S2_SIMILARITY,
        venue="Journal X",
        externalIds={"ArXiv": "2211.14468", "DOI": "10.1000/other"},
    )
    http = FakeHTTP(
        ("GET", "/paper/arXiv:2211.14468", [ok(record)]),
        bib_route("10.1000/other", entry),
        *SIMILARITY_PREPRINT_ROUTES,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], http)
    assert code == 0, err
    assert out.startswith("@misc{oesterheld2022,")
    assert "rejected 10.1000/other as its version of record" in err
    assert why in err


def test_bib_looks_for_a_version_of_record_again_after_a_week(cache, capsys):
    preprint = dict(
        OA_SIMILARITY,
        doi="https://doi.org/10.48550/arxiv.2211.14468",
        type="preprint",
        primary_location={"source": None, "version": "submittedVersion"},
    )
    first = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        ("GET", "api.openalex.org", [ok(preprint)]),
        *SIMILARITY_PREPRINT_ROUTES,
    )
    assert run(capsys, ["bib", "2211.14468"], first)[1].startswith("@misc{")
    soon = FakeHTTP(*SIMILARITY_PREPRINT_ROUTES)
    assert run(capsys, ["bib", "2211.14468"], soon)[1].startswith("@misc{")
    assert s2_calls(soon) == openalex_calls(soon) == []
    eight_days_ago = time.time() - 8 * 24 * 3600
    for f in [*(cache / "s2").glob("*.json"), *(cache / "openalex").glob("*.json")]:
        os.utime(f, (eight_days_ago, eight_days_ago))
    later = FakeHTTP(
        S2_SIMILARITY_ROUTE,
        OA_SIMILARITY_ROUTE,
        NEURIPS_BIB_ROUTE,
        SIMILARITY_LATEST_ROUTE,
    )
    code, out, err, _ = run(capsys, ["bib", "2211.14468"], later)
    assert code == 0, err
    assert out.startswith("@inproceedings{oesterheld2023,")


def test_meta_still_answers_from_a_cache_older_than_a_week(cache, capsys):
    http = FakeHTTP(("GET", "/paper/arXiv:1706.03762", [ok(ATTENTION)]))
    run(capsys, ["meta", "1706.03762"], http)
    month_ago = time.time() - 30 * 24 * 3600
    for f in (cache / "s2").glob("*.json"):
        os.utime(f, (month_ago, month_ago))
    again = FakeHTTP()
    code, out, err, _ = run(capsys, ["meta", "1706.03762"], again)
    assert code == 0, err
    assert out.startswith("Attention is All you Need\n")
    assert s2_calls(again) == []


def test_help_says_bib_prints_the_published_version_first(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "version of record" in out and "--preprint" in out


def test_help_says_an_arxiv_entry_follows_the_latest_version(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "follow arXiv's latest version" in out and "version = {N}" in out


def test_bib_puts_a_blank_line_between_entries_and_suffixes_clashing_keys(
    cache, capsys
):
    http = FakeHTTP(
        bib_route("10.2307/1912767", SEQUENTIAL_BIB),
        bib_route(NASH_DOI, NASH_BIB),
        ("GET", "https://doi.org/10.1016/0022-0531", [bibtex(REPUTATION_BIB)]),
    )
    argv = ["bib", "10.2307/1912767", NASH_DOI, "10.1016/0022-0531(82)90029-1"]
    code, out, err, _ = run(capsys, argv, http)
    assert code == 0, err
    entries = out.split("\n\n")
    assert [e.split(",", 1)[0] for e in entries] == [
        "@article{kreps1982a",
        "@article{nash1951",
        "@article{kreps1982b",
    ]
    assert entries[0].endswith("title={Sequential Equilibria}, author={Kreps, David M."
                               " and Wilson, Robert}, year={1982}}")  # fmt: skip
    assert out.endswith("year={1982}}\n")


def test_the_27th_entry_sharing_a_key_gets_aa(cache, capsys):
    http = FakeHTTP(("GET", "https://doi.org/", [bibtex(NASH_BIB)]))
    dois = [f"10.1000/{n}" for n in range(27)]
    code, out, err, _ = run(capsys, ["bib", *dois], http)
    assert code == 0, err
    keys = [entry.split(",", 1)[0] for entry in out.split("\n\n")]
    assert keys[0] == "@article{nash1951a"
    assert keys[25:] == ["@article{nash1951z", "@article{nash1951aa"]


@pytest.mark.parametrize(
    "fields, key",
    [
        ("author={Nash, John}, year={1951}", "nash1951"),
        ('author = "Nash, John", year = 1951', "nash1951"),
        ("AUTHOR = {Nash, John}, Year = {1951}", "nash1951"),
        (
            "author={Caspar Oesterheld and Vincent Conitzer}, year={2023}",
            "oesterheld2023",
        ),
        ('author={M{\\"u}ller, J{\\"o}rg}, year={2003}', "muller2003"),
        ("author={\\v{S}ediv{\\'y}, Jan}, year={2020}", "sedivy2020"),
        ("author={Müller-Schäfer, Jörg}, year={2003}", "mullerschafer2003"),
        ("author={Strauß, Anna and Ødegård, Ola}, year={2010}", "strauss2010"),
        ("author={Sæther, Kari}, year={2001}", "saether2001"),
        ("author={Ødegård, Ola}, year={2010}", "odegard2010"),
        ("author={{OpenAI} and Achiam, Josh}, year={2023}", "openai2023"),
        ("author={de Finetti, Bruno}, year={1937}", "definetti1937"),
        ("author={Bruno de Finetti}, year={1937}", "definetti1937"),
        ("author={Johannes Diderik van der Waals}, year={1873}", "vanderwaals1873"),
        ("author={Martin Luther King Jr.}, year={1963}", "king1963"),
        ("author={Martin Luther King, Jr.}, year={1963}", "king1963"),
        ("author={Robert Wilson III and Kreps, David}, year={1982}", "wilson1982"),
        ("title={Proceedings}, year={2001}", "anon2001"),
        ("title={An {author={x}} trap}, author={Nash, John}", "nash"),
    ],
)
def test_bib_keys_are_the_ascii_letters_of_the_family_name_then_the_year(
    cache, capsys, fields, key
):
    http = FakeHTTP(bib_route(NASH_DOI, "@article{Service_Key, " + fields + "}"))
    code, out, err, _ = run(capsys, ["bib", NASH_DOI], http)
    assert code == 0, err
    assert out == "@article{" + key + ", " + fields + "}\n"


def test_bib_answers_from_the_cache_whatever_the_dois_case(cache, capsys):
    first = FakeHTTP(("GET", "doi.org/", [bibtex(NASH_BIB)]))
    run(capsys, ["bib", "10.1111/1467-937X.00136"], first)
    again = FakeHTTP(("GET", "doi.org/", [DOI_NOT_FOUND]))
    code, out, err, _ = run(capsys, ["bib", "10.1111/1467-937x.00136"], again)
    assert code == 0, err
    assert out.startswith("@article{nash1951,")
    assert again.calls == []


def test_bib_of_a_missing_doi_says_not_found_keeps_going_and_exits_1(cache, capsys):
    http = FakeHTTP(
        ("GET", "doi.org/10.1000/missing", [DOI_NOT_FOUND]),
        bib_route(NASH_DOI, NASH_BIB),
    )
    code, out, err, _ = run(capsys, ["bib", "10.1000/missing", NASH_DOI], http)
    assert code == 1
    assert err == "paper: not found: 10.1000/missing\n"
    assert out.startswith("@article{nash1951,")


@pytest.mark.parametrize(
    "answer, why",
    [
        (
            paper.Response(200, {"content-type": "text/html"}, b"<html>landing"),
            "doi.org sent text/html",
        ),
        (paper.Response(406, {}, b"Not Acceptable"), "HTTP 406"),
    ],
    ids=["landing-page", "406"],
)
def test_a_doi_without_bibtex_exits_1_naming_it_and_caches_nothing(
    cache, capsys, answer, why
):
    code, out, err, _ = run(
        capsys, ["bib", NASH_DOI], FakeHTTP(("GET", "doi.org/", [answer]))
    )
    assert (code, out) == (1, "")
    assert "no BibTeX for 10.2307/1969529" in err and why in err
    again = run(capsys, ["bib", NASH_DOI], FakeHTTP(bib_route(NASH_DOI, NASH_BIB)))
    assert again[0] == 0 and again[1].startswith("@article{nash1951,")


@pytest.mark.parametrize("other", [S2_ID, "https://example.com/a.pdf"])
def test_bib_refuses_an_id_without_a_doi_before_any_request(cache, capsys, other):
    http = FakeHTTP(bib_route(NASH_DOI, NASH_BIB))
    code, out, err, _ = run(capsys, ["bib", NASH_DOI, other], http)
    assert (code, out) == (2, "")
    assert "`paper bib`" in err and "DOI" in err and "arXiv" in err
    assert http.calls == []


def test_bib_is_paced_logged_and_cached(cache, capsys):
    paces = []
    http = FakeHTTP(bib_route(NASH_DOI, NASH_BIB))
    code, first, err, _ = run(capsys, ["bib", NASH_DOI], http, paces=paces)
    assert code == 0, err
    assert paces == ["doi.org"]
    assert [line["service"] for line in logged(cache)] == ["doi.org"]
    assert run(capsys, ["bib", NASH_DOI], FakeHTTP())[:2] == (0, first)


def test_bib_prints_the_entries_fetched_before_a_remote_failure(cache, capsys):
    http = FakeHTTP(
        bib_route(NASH_DOI, NASH_BIB), ("GET", "doi.org/10.1000/busy", [status(503)])
    )
    code, out, err, _ = run(capsys, ["bib", NASH_DOI, "10.1000/busy"], http)
    assert code == 3
    assert out.startswith("@article{nash1951,")
    assert "gave up" in err


def test_bib_escapes_a_doi_for_doi_org(cache, capsys):
    http = FakeHTTP(("GET", "https://doi.org/", [bibtex(NASH_BIB)]))
    code, _, err, _ = run(capsys, ["bib", SICI], http)
    assert code == 0, err
    (call,) = http.calls
    assert call["url"] == (
        "https://doi.org/"
        "10.1002/%28SICI%291097-4571%28199806%2949:8%3C693::AID-ASI4%3E3.0.CO%3B2-0"
    )


def test_help_explains_bib(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "bib prints a BibTeX entry" in out
    assert "nash1951" in out and "doi.org" in out


# paper sweep: /paper/search/bulk to a TSV file

SWEPT = [
    {
        "paperId": "1" * 40,
        "title": "Secret Collusion among AI Agents",
        "year": 2024,
        "publicationDate": "2024-02-12",
        "venue": "NeurIPS",
        "citationCount": 154,
        "externalIds": {"ArXiv": "2402.07510", "DOI": "10.5555/secret"},
        "authors": [{"name": "Sumeet Motwani"}, {"name": "Mikhail Baranchuk"}],
    },
    {
        "paperId": "2" * 40,
        "title": "Algorithmic Collusion by\tLarge\nLanguage Models",
        "year": 2024,
        "publicationDate": "2024-04-01",
        "venue": "",
        "citationCount": 75,
        "externalIds": {"ArXiv": "2404.00806"},
        "authors": [{"name": "Sara Fish"}],
    },
    {
        "paperId": "3" * 40,
        "title": "Tacit collusion",
        "year": 2025,
        "publicationDate": "2025-06-30",
        "venue": "AER",
        "citationCount": 3,
        "externalIds": {},
        "authors": [],
    },
]
SECRET, FISH, TACIT = SWEPT


def bulk_route(*pages):
    return ("GET", "/paper/search/bulk", [ok(page) for page in pages])


def one_page(*papers, total=None):
    return bulk_route(
        {"total": total or len(papers), "token": None, "data": list(papers)}
    )


def sweep_file(out):
    return Path(out.splitlines()[-1].removeprefix("full list: "))


def listed_titles(out):
    """The printed rows' titles, in order (each row starts "year · title")."""
    return [
        line.split(" · ")[1] for line in out.splitlines() if " · citations: " in line
    ]


def test_sweep_pages_with_the_token_and_writes_every_paper_to_a_file(cache, capsys):
    http = FakeHTTP(
        bulk_route(
            {"total": 3, "token": "t1", "data": [SECRET, FISH]},
            {"total": 3, "token": None, "data": [TACIT]},
        )
    )
    code, out, err, _ = run(capsys, ["sweep", '"collusion"', "+", "LLM"], http)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[:3] == [
        "3 papers fetched; Semantic Scholar estimates 3 match",
        "most cited:",
        "2024 · Secret Collusion among AI Agents · NeurIPS · citations: 154"
        " · arXiv:2402.07510 DOI:10.5555/secret S2:" + "1" * 40,
    ]
    assert lines[3].startswith(
        "2024 · Algorithmic Collusion by Large Language Models · citations: 75"
    )
    assert len(lines) == 6 and lines[-1].startswith("full list: ")
    first, second = s2_calls(http)
    assert first["params"] == {
        "query": '"collusion" + LLM',
        "fields": paper.SWEEP_FIELDS,
        "sort": "citationCount:desc",
    }
    assert second["params"]["token"] == "t1"
    rows = sweep_file(out).read_text().splitlines()
    assert (
        rows[0] == "year\tdate\tcitations\ttitle\tvenue\tfirst author\tarXiv\tDOI\tS2"
    )
    assert rows[1] == (
        "2024\t2024-02-12\t154\tSecret Collusion among AI Agents\tNeurIPS"
        "\tSumeet Motwani et al.\t2402.07510\t10.5555/secret\t" + "1" * 40
    )
    assert rows[2].split("\t")[3] == "Algorithmic Collusion by Large Language Models"
    assert rows[3] == "2025\t2025-06-30\t3\tTacit collusion\tAER\t\t\t\t" + "3" * 40


def test_the_newest_list_shows_what_the_most_cited_list_leaves_out(cache, capsys):
    http = FakeHTTP(one_page(*SWEPT))
    code, out, err, _ = run(capsys, ["sweep", "collusion", "--top", "1"], http)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[1] == "most cited:" and lines[3] == "newest, not listed above:"
    assert listed_titles(out) == ["Secret Collusion among AI Agents", "Tacit collusion"]
    assert len(s2_calls(http)) == 1


def test_a_paper_both_most_cited_and_newest_is_listed_once(cache, capsys):
    landmark = dict(
        SECRET,
        paperId="9" * 40,
        title="Landmark",
        publicationDate="2026-01-02",
        year=2026,
        citationCount=500,
    )
    http = FakeHTTP(one_page(landmark, *SWEPT))
    _, out, _, _ = run(capsys, ["sweep", "collusion", "--top", "1"], http)
    assert listed_titles(out) == ["Landmark", "Tacit collusion"]


def test_a_paper_without_a_date_counts_as_its_year(cache, capsys):
    undated = dict(
        TACIT,
        paperId="8" * 40,
        title="Undated",
        year=2026,
        publicationDate=None,
        citationCount=1,
    )
    http = FakeHTTP(one_page(*SWEPT, undated))
    _, out, _, _ = run(capsys, ["sweep", "collusion", "--top", "1"], http)
    assert listed_titles(out) == ["Secret Collusion among AI Agents", "Undated"]


def test_every_paper_in_the_first_list_leaves_out_the_second(cache, capsys):
    http = FakeHTTP(one_page(*SWEPT))
    _, out, _, _ = run(capsys, ["sweep", "collusion"], http)
    assert "newest" not in out
    assert len(listed_titles(out)) == 3


def test_a_cut_short_sweep_asks_again_newest_first_and_merges(cache, capsys):
    http = FakeHTTP(
        bulk_route(
            {"total": 900, "token": "t1", "data": [SECRET, FISH]},
            {"total": 900, "token": "t2", "data": [TACIT, FISH]},
        )
    )
    code, out, err, _ = run(
        capsys, ["sweep", "collusion", "--max", "2", "--top", "1"], http
    )
    assert code == 0, err
    first, second = s2_calls(http)
    assert first["params"]["sort"] == "citationCount:desc"
    assert (
        second["params"]["sort"] == "publicationDate:desc"
        and "token" not in second["params"]
    )
    assert out.splitlines()[0] == (
        "3 papers fetched; Semantic Scholar estimates 900 match; the file holds the 2 most"
        " cited and the 2 newest, so raise --max for the rest"
    )
    assert listed_titles(out) == ["Secret Collusion among AI Agents", "Tacit collusion"]
    rows = sweep_file(out).read_text().splitlines()[1:]
    assert [row.split("\t")[2] for row in rows] == ["154", "75", "3"]
    assert (
        json.loads(sweep_file(out).with_suffix(".json").read_text())["complete"]
        is False
    )


def test_a_complete_sweep_says_so_in_its_details(cache, capsys):
    _, out, _, _ = run(capsys, ["sweep", "collusion"], FakeHTTP(one_page(*SWEPT)))
    assert (
        json.loads(sweep_file(out).with_suffix(".json").read_text())["complete"] is True
    )


def test_sweep_sends_its_filters(cache, capsys):
    http = FakeHTTP(one_page(*SWEPT))
    run(
        capsys,
        ["sweep", "collusion", "--year", "2024-", "--min-citations", "5",
         "--fields-of-study", "Computer Science,Economics"],
        http,
    )  # fmt: skip
    (call,) = s2_calls(http)
    params = call["params"]
    assert params["year"] == "2024-" and params["minCitationCount"] == 5
    assert params["fieldsOfStudy"] == "Computer Science,Economics"


def test_sweep_reuses_its_file_for_a_day(cache, capsys):
    _, first, _, _ = run(capsys, ["sweep", "collusion"], FakeHTTP(one_page(*SWEPT)))
    again = FakeHTTP()
    code, out, _, _ = run(capsys, ["sweep", "collusion"], again)
    assert (code, again.calls) == (0, [])
    assert out == first
    two_days_ago = time.time() - 2 * 86400
    os.utime(sweep_file(out), (two_days_ago, two_days_ago))
    later = FakeHTTP(one_page(*SWEPT))
    run(capsys, ["sweep", "collusion"], later)
    assert len(s2_calls(later)) == 1


def test_a_sweep_with_no_match_exits_1(cache, capsys):
    code, out, err, _ = run(capsys, ["sweep", "zzzz"], FakeHTTP(one_page()))
    assert (code, out) == (1, "")
    assert "no papers match" in err


def test_a_sweep_never_falls_back_to_openalex(cache, capsys):
    http = FakeHTTP(S2_DOWN)
    code, out, err, _ = run(capsys, ["sweep", "collusion"], http)
    assert (code, out) == (3, "")
    assert openalex_calls(http) == [] and "gave up" in err


def test_help_explains_sweep_and_its_query_syntax(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "sweep" in out and "title and abstract" in out and "relevance" in out
    assert "most cited" in out and "newest" in out
    for syntax in ("+", "|", '"', "*"):
        assert syntax in out


def test_zero_citations_show_as_0(cache, capsys):
    unread = dict(TACIT, citationCount=0)
    _, out, _, _ = run(capsys, ["sweep", "tacit"], FakeHTTP(one_page(unread)))
    assert "citations: 0 ·" in out
    assert sweep_file(out).read_text().splitlines()[1].split("\t")[2] == "0"


@pytest.mark.parametrize("argv", [["--max", "0"], ["--top", "-1"]])
def test_sweep_refuses_a_max_below_1_or_a_negative_top(cache, capsys, argv):
    http = FakeHTTP()
    with pytest.raises(SystemExit) as e:
        run(capsys, ["sweep", "collusion", *argv], http)
    assert e.value.code == 2 and http.calls == []


def test_the_sweep_synopsis_names_every_option():
    synopsis = paper.__doc__.split("\n\n")[1]
    sweep = synopsis[synopsis.index("paper sweep") :].split("    paper ")[0]
    for option in ("--year", "--min-citations", "--fields-of-study", "--max", "--top"):
        assert option in sweep
    assert "--sort" not in sweep


def test_the_sweep_file_is_plain_tsv_with_its_details_beside_it(cache, capsys):
    query = 'estimated total: 7\n"collusion"'
    code, out, err, _ = run(capsys, ["sweep", query], FakeHTTP(one_page(*SWEPT)))
    assert code == 0, err
    assert out.splitlines()[0] == "3 papers fetched; Semantic Scholar estimates 3 match"
    rows = sweep_file(out).read_text().splitlines()
    assert rows[0] == paper.SWEEP_HEADER and len(rows) == 4
    details = json.loads(sweep_file(out).with_suffix(".json").read_text())
    assert details["query"] == query and details["estimated_total"] == 3


def test_a_truncated_sweep_file_is_fetched_again(cache, capsys):
    _, out, _, _ = run(capsys, ["sweep", "collusion"], FakeHTTP(one_page(*SWEPT)))
    sweep_file(out).write_text("")
    again = FakeHTTP(one_page(*SWEPT))
    code, out, _, _ = run(capsys, ["sweep", "collusion"], again)
    assert code == 0 and len(s2_calls(again)) == 1
    assert out.splitlines()[0] == "3 papers fetched; Semantic Scholar estimates 3 match"


def test_sweep_files_differ_by_query_filter_and_max(cache, capsys):
    variants = [
        ["collusion"], ["cartel"], ["collusion", "--year", "2024-"],
        ["collusion", "--min-citations", "5"], ["collusion", "--fields-of-study", "Economics"],
        ["collusion", "--max", "5"],
    ]  # fmt: skip
    paths = set()
    for argv in variants:
        _, out, _, _ = run(capsys, ["sweep", *argv], FakeHTTP(one_page(*SWEPT)))
        paths.add(sweep_file(out))
    assert len(paths) == len(variants)


@pytest.mark.parametrize(
    "data", [[None], ["x"], [{"title": "No id"}]], ids=["null", "string", "no-id"]
)
def test_a_malformed_bulk_answer_exits_3(cache, capsys, data):
    http = FakeHTTP(bulk_route({"total": 1, "token": None, "data": data}))
    code, out, err, _ = run(capsys, ["sweep", "collusion"], http)
    assert (code, out) == (3, "")
    assert "unexpected" in err


def test_a_total_that_isnt_a_number_still_caches(cache, capsys):
    run(
        capsys,
        ["sweep", "collusion"],
        FakeHTTP(bulk_route({"total": "lots", "token": None, "data": SWEPT})),
    )
    again = FakeHTTP()
    code, out, _, _ = run(capsys, ["sweep", "collusion"], again)
    assert (code, again.calls) == (0, [])
    assert (
        out.splitlines()[0]
        == "3 papers fetched; Semantic Scholar's estimate of the matches is missing"
    )


def test_a_top_above_max_keeps_the_newest_out_of_the_most_cited_list(cache, capsys):
    http = FakeHTTP(
        bulk_route(
            {"total": 900, "token": "t1", "data": [SECRET, FISH]},
            {"total": 900, "token": "t2", "data": [TACIT, FISH]},
        )
    )
    _, out, _, _ = run(capsys, ["sweep", "collusion", "--max", "2"], http)
    cited = out.split("newest, not listed above:")[0]
    assert "Tacit collusion" not in cited
    assert listed_titles(out) == [
        "Secret Collusion among AI Agents",
        "Algorithmic Collusion by Large Language Models",
        "Tacit collusion",
    ]


def test_a_paper_repeated_across_pages_is_written_and_listed_once(cache, capsys):
    http = FakeHTTP(
        bulk_route(
            {"total": 3, "token": "t1", "data": [SECRET, FISH]},
            {"total": 3, "token": None, "data": [FISH, TACIT]},
        )
    )
    _, out, _, _ = run(capsys, ["sweep", "collusion", "--top", "1"], http)
    assert len(sweep_file(out).read_text().splitlines()) == 1 + 3
    assert sorted(listed_titles(out)) == [
        "Secret Collusion among AI Agents",
        "Tacit collusion",
    ]


def test_a_sweep_file_cut_short_after_its_header_is_fetched_again(cache, capsys):
    _, out, _, _ = run(capsys, ["sweep", "collusion"], FakeHTTP(one_page(*SWEPT)))
    sweep_file(out).write_text(paper.SWEEP_HEADER + "\n")
    again = FakeHTTP(one_page(*SWEPT))
    code, out, _, _ = run(capsys, ["sweep", "collusion"], again)
    assert code == 0 and len(s2_calls(again)) == 1
    assert out.splitlines()[0].startswith("3 papers fetched")


def test_details_without_their_counts_mean_a_refetch(cache, capsys):
    _, out, _, _ = run(capsys, ["sweep", "collusion"], FakeHTTP(one_page(*SWEPT)))
    details = sweep_file(out).with_suffix(".json")
    details.write_text(json.dumps({"estimated_total": 3}))
    again = FakeHTTP(one_page(*SWEPT))
    run(capsys, ["sweep", "collusion"], again)
    assert len(s2_calls(again)) == 1


def test_a_page_over_max_with_no_token_still_asks_for_the_newest(cache, capsys):
    http = FakeHTTP(one_page(*SWEPT, total=3))
    run(capsys, ["sweep", "collusion", "--max", "2"], http)
    assert [c["params"]["sort"] for c in s2_calls(http)] == [
        "citationCount:desc",
        "publicationDate:desc",
    ]


def test_help_says_a_thin_result_means_rephrase(capsys):
    with pytest.raises(SystemExit):
        paper.main(["-h"])
    out = " ".join(capsys.readouterr().out.split())
    assert "2-3 key terms" in out and "synonyms" in out and "rephrase" in out
