"""bin/gui-shot: action parsing, plus a real run per backend when the
display tools are installed."""

import importlib.machinery
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "gui-shot"
loader = importlib.machinery.SourceFileLoader("gui_shot", str(SCRIPT))
spec = importlib.util.spec_from_loader("gui_shot", loader)
gui_shot = importlib.util.module_from_spec(spec)
loader.exec_module(gui_shot)


def test_parses_actions():
    assert gui_shot.parse_action("click 10 20") == gui_shot.Action("click", ("10", "20"))
    assert gui_shot.parse_action("click 10 20 3").args == ("10", "20", "3")
    assert gui_shot.parse_action("type hello world").args == ("hello world",)
    assert gui_shot.parse_action("key ctrl+z").args == ("ctrl+z",)
    assert gui_shot.parse_action("  sleep 0.5 ").args == ("0.5",)


@pytest.mark.parametrize("bad", ["jump 1 2", "click 10", "click a b", "sleep", "shot", "resize 10"])
def test_rejects_bad_actions(bad):
    with pytest.raises(gui_shot.UsageError):
        gui_shot.parse_action(bad)


def test_wayland_refuses_input_actions(capsys):
    code = gui_shot.main(["--backend", "wayland", "--do", "click 1 1", "--", "true"])
    assert code == 2
    assert "only sleep and shot" in capsys.readouterr().err


def have(*tools):
    return all(shutil.which(t) for t in tools)


def run_tool(*args, tmp_path):
    return subprocess.run([str(SCRIPT), "--out", str(tmp_path), *args], capture_output=True, text=True, timeout=120, check=False)


@pytest.mark.skipif(not have("Xvfb", "xdotool", "import", "xeyes"), reason="x11 tools missing")
def test_x11_run_takes_named_and_final_shots(tmp_path):
    result = run_tool("--size", "640x480", "--do", "click 20 20", "--do", "key Escape", "--do", "shot mid",
                      "--", "xeyes", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(tmp_path / "mid.png"), str(tmp_path / "final.png")]
    assert (tmp_path / "final.png").stat().st_size > 0


@pytest.mark.skipif(not have("weston", "weston-screenshooter", "weston-simple-shm"), reason="weston missing")
def test_wayland_run_takes_a_shot(tmp_path):
    result = run_tool("--backend", "wayland", "--do", "sleep 0.5", "--", "weston-simple-shm", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "final.png").stat().st_size > 0


def test_app_that_never_opens_a_window_times_out(tmp_path):
    if not have("Xvfb", "xdotool", "import"):
        pytest.skip("x11 tools missing")
    result = run_tool("--timeout", "2", "--", "sleep", "30", tmp_path=tmp_path)
    assert result.returncode == 1
    assert "timed out" in result.stderr
