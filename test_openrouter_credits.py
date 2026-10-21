"""bin/openrouter-credits: what it prints, where it finds the key, and that it never shows it."""

import importlib.machinery
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "openrouter-credits"
loader = importlib.machinery.SourceFileLoader("openrouter_credits", str(SCRIPT))
spec = importlib.util.spec_from_loader("openrouter_credits", loader)
credits = importlib.util.module_from_spec(spec)
loader.exec_module(credits)

KEY = "sk-or-v1-test-key-123"


def fake_fetch(seen):
    def fetch(key):
        seen.append(key)
        return {"total_credits": 25, "total_usage": 15.341}

    return fetch


def test_prints_bought_used_and_left(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr(credits, "fetch_credits", fake_fetch(seen))

    assert credits.main([]) == 0

    out = capsys.readouterr().out
    assert "$25.00 bought, $15.34 used, $9.66 left" in out
    assert seen == [KEY] and KEY not in out


def test_the_key_can_come_from_a_dotenv_file(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"OTHER=1\nOPENROUTER_API_KEY={KEY}\n")
    monkeypatch.setattr(credits, "fetch_credits", fake_fetch(seen))

    assert credits.main([]) == 0
    assert seen == [KEY]


def test_env_file_names_another_file(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / "keys.env").write_text(f'export OPENROUTER_API_KEY="{KEY}"\n')
    monkeypatch.setattr(credits, "fetch_credits", fake_fetch(seen))

    assert credits.main(["--env-file", str(tmp_path / "keys.env")]) == 0
    assert seen == [KEY]


def test_without_a_key_it_says_where_it_looked(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert credits.main([]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_it_asks_the_credits_endpoint_with_the_key(monkeypatch, capsys):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return FakeResponse(
            json.dumps({"data": {"total_credits": 10, "total_usage": 2.5}}).encode()
        )

    monkeypatch.setenv(
        "OPENROUTER_API_KEY", KEY + "\n"
    )  # as a line read from a file can end
    monkeypatch.setattr(credits.urllib.request, "urlopen", urlopen)

    assert credits.main([]) == 0

    [request] = requests
    assert request.full_url == "https://openrouter.ai/api/v1/credits"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert "$10.00 bought, $2.50 used, $7.50 left" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(
            "https://openrouter.ai", 401, "Unauthorized", None, None
        ),
        ValueError(f"Invalid header value b'Bearer {KEY}\\n'"),
        urllib.error.URLError("no route"),
        KeyError("data"),
    ],
)
def test_a_failure_is_reported_without_the_key(monkeypatch, capsys, error):
    def urlopen(request, timeout):
        raise error

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr(credits.urllib.request, "urlopen", urlopen)

    assert credits.main([]) == 1

    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert "OpenRouter" in captured.err
