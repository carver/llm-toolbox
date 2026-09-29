"""bin/sofa against a stub SOFA server: credentials, the cached session, retries, and the key never printed."""

import http.server
import importlib.machinery
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "sofa"
KEY = "q1-this-is-the-secret-key-X0"


def load(monkeypatch, base, cache):
    monkeypatch.setenv("SOFA_BASE_URL", base)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    loader = importlib.machinery.SourceFileLoader("sofa_cli", str(SCRIPT))
    spec = importlib.util.spec_from_loader("sofa_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Stub(http.server.BaseHTTPRequestHandler):
    """Sessions, a post, votes that need a read first, and a way to expire every session."""
    sessions, reads, calls, expired = set(), set(), [], False

    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=()):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        Stub.calls.append((method, self.path))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        if self.path == "/skill.md":
            return self.reply(200, "# skill", [("X-Sofa-Skill-Digest", "ab" * 32)])
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self.reply(401, {"error": "invalid key"})
        if self.path == "/api/sessions":
            sid = f"s{len(Stub.sessions)}"
            Stub.sessions.add(sid)
            return self.reply(201, {"session_id": sid, "expires_at": "2099-01-01T00:00:00+00:00"})
        sid = self.headers.get("X-Sofa-Session")
        if Stub.expired or sid not in Stub.sessions:
            Stub.expired = False
            return self.reply(401, {"error": "invalid_session"})
        if self.path.startswith("/api/posts/"):
            Stub.reads.add((sid, self.path.rsplit("/", 1)[1]))
            return self.reply(200, {"id": "p1", "content_type": "til", "title": "T", "body": "B", "tags": [],
                                    "replies": [], "trust_summary": {"score": 42}})
        if self.path == "/api/votes":
            if (sid, body["post_id"]) not in Stub.reads:
                return self.reply(403, {"error": "read the post first"})
            return self.reply(201, {"ok": True})
        if self.path.startswith("/api/posts?"):
            return self.reply(200, {"items": [{"id": "p1", "content_type": "til", "title": "T",
                                               "body_excerpt": "x", "trust_summary": {"score": None}}]})
        return self.reply(404, {"error": "no such stub route"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


@pytest.fixture
def sofa(monkeypatch, tmp_path):
    Stub.sessions, Stub.reads, Stub.calls, Stub.expired = set(), set(), [], False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    creds = tmp_path / "work" / ".sofa" / "credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"agent-1": {"agent_name": "robo", "api_key": KEY}}))
    monkeypatch.chdir(creds.parent.parent)
    monkeypatch.delenv("SOFA_API_KEY", raising=False)
    monkeypatch.delenv("SOFA_CREDENTIALS", raising=False)
    yield load(monkeypatch, f"http://127.0.0.1:{server.server_port}", tmp_path / "cache")
    server.shutdown()


def run(sofa, capsys, *argv):
    code = sofa.main(list(argv))
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    return code, out.out + out.err


def test_search_and_get_print_summaries_and_links(sofa, capsys):
    code, out = run(sofa, capsys, "search", "x")
    assert code == 0 and "p1  til" in out and "unscored" in out
    code, out = run(sofa, capsys, "get", "p1")
    assert code == 0 and "/tils/p1" in out and "trust +42" in out


def test_the_session_is_cached_across_calls(sofa, capsys):
    run(sofa, capsys, "search", "x")
    run(sofa, capsys, "search", "y")
    assert [c for c in Stub.calls if c[1] == "/api/sessions"] == [("POST", "/api/sessions")]


def test_an_expired_session_is_replaced_once(sofa, capsys):
    run(sofa, capsys, "search", "x")
    Stub.expired = True
    code, _ = run(sofa, capsys, "search", "x")
    assert code == 0 and len(Stub.sessions) == 2


def test_vote_reads_the_post_first(sofa, capsys):
    code, out = run(sofa, capsys, "vote", "p1", "up")
    assert code == 0 and "vote recorded" in out


def test_several_agents_need_a_choice_and_the_error_never_shows_a_key(sofa, capsys, tmp_path):
    creds = Path(".sofa/credentials.json")
    creds.write_text(json.dumps({"a1": {"agent_name": "one", "api_key": KEY}, "a2": {"agent_name": "two", "api_key": KEY}}))
    code, out = run(sofa, capsys, "search", "x")
    assert code == 1 and "--agent" in out and "one, two" in out
    assert run(sofa, capsys, "--agent", "two", "search", "x")[0] == 0


def test_an_http_error_is_reported_not_raised(sofa, capsys):
    code, out = run(sofa, capsys, "api", "GET", "/api/nowhere")
    assert code == 1 and "HTTP 404" in out
