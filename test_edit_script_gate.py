"""bin/edit-script-gate: which inline Python edits it refuses, and what it lets through."""

import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "edit-script-gate"
loader = importlib.machinery.SourceFileLoader("edit_script_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader("edit_script_gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)

READ_REPLACE_WRITE = (
    "p = 'a.py'\ns = open(p).read()\ns = s.replace('x', 'y')\nopen(p, 'w').write(s)\n"
)


def run_hook(monkeypatch, capsys, command, tool="Bash"):
    event = {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/r"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = gate.main([])
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        f"python3 - <<'EOF'\n{READ_REPLACE_WRITE}EOF",
        f"cd repo && python3 -I - <<'PY'\n{READ_REPLACE_WRITE}PY",
        f"uv run python - <<EOF\n{READ_REPLACE_WRITE}EOF",
        "python3 -c \"p='a.py'; s=open(p).read(); open(p,'w').write(s.replace('x','y'))\"",
        "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('a.py')\np.write_text(p.read_text().replace('x', 'y'))\nEOF",
        f"/usr/bin/python3 - <<'EOF'\n{READ_REPLACE_WRITE}EOF",
        f"env PYTHONIOENCODING=utf8 python3 - <<'EOF'\n{READ_REPLACE_WRITE}EOF",
        f"sudo python3 - <<'EOF'\n{READ_REPLACE_WRITE}EOF",
        "python3 - <<'EOF'\nlines = open('a.py').readlines()\nopen('a.py', 'w').writelines(l.replace('x', 'y') for l in lines)\nEOF",
    ],
)
def test_an_inline_read_replace_write_script_is_refused(monkeypatch, capsys, command):
    code, err = run_hook(monkeypatch, capsys, command)

    assert code == 2
    assert "apply-edits" in err


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/x/y/' a.py",
        "python3 tool.py --fix a.py",
        "python3 - <<'EOF'\nimport re\np = 'a.py'\ns = open(p).read()\nopen(p, 'w').write(re.sub(r'x+', 'y', s))\nEOF",
        "python3 - <<'EOF'\nfor line in open('log.txt'):\n    print(line.replace('\\n', ' '))\nEOF",
        f"cat > fix.py <<'EOF'\n#!/usr/bin/env python3\n{READ_REPLACE_WRITE}EOF",
        f"cat > test_x.py <<'EOF'\n{READ_REPLACE_WRITE}EOF\npython3 -m pytest -q",
        f"apply-edits <<'EOF'\n--- a.py\n<<<<<<<\nx\n=======\n{READ_REPLACE_WRITE}>>>>>>>\nEOF\npython3 -m pytest -q",
        "git commit -F - <<'EOF'\nRead a file, .replace() it and .write( it back\nEOF\npython3 -c 'print(1)'",
        (
            "python3 - <<'EOF'\nimport json\nfrom pathlib import Path\n"
            "runs = json.loads(Path('runs.json').read_text())\n"
            "Path('report.md').write_text('\\n'.join(r['model'].replace('/', ' ') for r in runs))\nEOF"
        ),
        "python3 -c \"import sys; sys.stdout.write(sys.stdin.read().replace('a', 'b'))\" < in.txt > out.txt",
        "python3 - <<'EOF'\nimport json\nrows = json.load(open('runs.json'))\nprint(rows)\nEOF\ngrep -rn '\\.replace(' bin/ > hits.txt",
    ],
)
def test_other_commands_pass(monkeypatch, capsys, command):
    assert run_hook(monkeypatch, capsys, command) == (0, "")


def test_other_tools_pass(monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, READ_REPLACE_WRITE, tool="Write") == (0, "")
