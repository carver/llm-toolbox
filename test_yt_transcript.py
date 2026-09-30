"""bin/yt-transcript: video and timestamp parsing, the time window, and the CLI with a fake fetch."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "yt-transcript"
loader = importlib.machinery.SourceFileLoader("yt_transcript", str(SCRIPT))
spec = importlib.util.spec_from_loader("yt_transcript", loader)
yt = importlib.util.module_from_spec(spec)
loader.exec_module(yt)

ID = "LlgiOCmFG_w"


@pytest.mark.parametrize("text, expected", [
    (ID, (ID, None)),
    (f"https://www.youtube.com/watch?v={ID}", (ID, None)),
    (f"https://m.youtube.com/watch?v={ID}&t=1238", (ID, 1238)),
    (f"https://youtube.com/watch?t=1238s&v={ID}", (ID, 1238)),
    (f"https://youtu.be/{ID}?t=20m38s", (ID, 1238)),
    (f"https://www.youtube.com/shorts/{ID}", (ID, None)),
    (f"https://www.youtube.com/live/{ID}?t=1h2m3s", (ID, 3723)),
    (f"https://www.youtube.com/embed/{ID}?start=90", (ID, 90)),
])
def test_parses_video_and_timestamp(text, expected):
    assert yt.parse_video(text) == expected


@pytest.mark.parametrize("bad", ["", "notanid", "https://example.com/watch?v=" + ID, "https://www.youtube.com/watch"])
def test_rejects_what_is_not_a_video(bad):
    with pytest.raises(yt.UsageError):
        yt.parse_video(bad)


@pytest.mark.parametrize("text, seconds", [
    ("1238", 1238), ("1238s", 1238), ("20m38s", 1238), ("1h", 3600),
    ("20:38", 1238), ("1:02:03", 3723), ("0:05", 5),
])
def test_parses_times(text, seconds):
    assert yt.parse_time(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "20:99", "-5", "m"])
def test_rejects_bad_times(bad):
    with pytest.raises(yt.UsageError):
        yt.parse_time(bad)


SEGMENTS = [(0.0, "intro"), (100.0, "setup"), (1150.0, "before"), (1238.5, "at"), (1290.0, "after"), (1400.0, "late")]


def test_window_keeps_segments_around_the_moment():
    assert [t for _, t in yt.window(SEGMENTS, at=1238, before=90, after=60)] == ["before", "at", "after"]


def test_window_without_a_moment_keeps_everything():
    assert yt.window(SEGMENTS, at=None, before=90, after=60) == SEGMENTS


def test_window_clamps_at_the_start():
    assert [t for _, t in yt.window(SEGMENTS, at=30, before=90, after=100)] == ["intro", "setup"]


def test_formats_a_segment_with_its_clock_time():
    assert yt.format_segment((1238.5, "you never want\nto write it twice")) == "[20:38] you never want to write it twice"
    assert yt.format_segment((3723.0, "x")) == "[1:02:03] x"


def run_main(monkeypatch, capsys, argv, segments=SEGMENTS, error=None):
    def fake_fetch(video_id, refresh=False):
        assert video_id == ID
        if error:
            raise error
        return segments

    monkeypatch.setattr(yt, "fetch", fake_fetch)
    code = yt.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_prints_the_window_from_the_url_timestamp(monkeypatch, capsys):
    code, out, _ = run_main(monkeypatch, capsys, [f"https://youtu.be/{ID}?t=1238"])
    assert code == 0
    assert out.splitlines() == ["[19:10] before", "[20:38] at", "[21:30] after"]


def test_at_flag_overrides_the_url_timestamp(monkeypatch, capsys):
    _, out, _ = run_main(monkeypatch, capsys, ["--at", "1:40", "--before", "0", "--after", "0", f"https://youtu.be/{ID}?t=1238"])
    assert out.splitlines() == ["[1:40] setup"]


def test_fetch_failure_exits_1_with_the_reason(monkeypatch, capsys):
    code, out, err = run_main(monkeypatch, capsys, [ID], error=yt.FetchFailed("no transcript for this video"))
    assert (code, out) == (1, "")
    assert "no transcript for this video" in err


def test_bad_input_exits_2(monkeypatch, capsys):
    code, _, err = run_main(monkeypatch, capsys, ["https://example.com/x"])
    assert code == 2
    assert "not a YouTube video" in err


def test_a_refused_request_exits_3(monkeypatch, capsys):
    code, _, err = run_main(monkeypatch, capsys, [ID], error=yt.TryLater("IpBlocked: blocked"))
    assert code == 3
    assert "try later" in err and "IpBlocked" in err


class Downloads:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, video_id):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_a_fetched_transcript_is_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    downloads = Downloads([(1.5, "hi")])
    monkeypatch.setattr(yt, "download", downloads)
    assert yt.fetch(ID) == [(1.5, "hi")]
    assert yt.fetch(ID) == [(1.5, "hi")]
    assert downloads.calls == 1
    assert (tmp_path / "yt-transcript" / f"{ID}.json").exists()


def test_refresh_skips_the_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    downloads = Downloads([(1.5, "hi")])
    monkeypatch.setattr(yt, "download", downloads)
    yt.fetch(ID)
    yt.fetch(ID, refresh=True)
    assert downloads.calls == 2


def test_a_failed_fetch_caches_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(yt, "download", Downloads(yt.TryLater("blocked")))
    with pytest.raises(yt.TryLater):
        yt.fetch(ID)
    assert not (tmp_path / "yt-transcript").exists()


@pytest.mark.parametrize("name, expected", [("IpBlocked", "TryLater"), ("TranscriptsDisabled", "FetchFailed"),
                                            ("ConnectionError", "TryLater")])
def test_download_sorts_errors_into_no_transcript_and_try_later(monkeypatch, name, expected):
    import types

    error = type(name, (Exception,), {})
    fake = types.ModuleType("youtube_transcript_api")

    class Api:
        def fetch(self, video_id):
            raise error("first line\nmore")

    fake.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake)
    with pytest.raises(getattr(yt, expected), match=f"{name}: first line$"):
        yt.download(ID)
