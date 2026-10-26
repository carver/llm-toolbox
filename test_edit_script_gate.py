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


SCRATCH = "/tmp/claude-1000/-proj/session/scratchpad"


def edit_of(target: str) -> str:
    return f"p = {target}\ns = open(p).read()\ns = s.replace('x', 'y')\nopen(p, 'w').write(s)\n"


@pytest.mark.parametrize(
    "command",
    [
        f"python3 - <<'EOF'\n{edit_of(repr(SCRATCH + '/labels.py'))}EOF",
        f"S={SCRATCH}/labels.py && python3 - \"$S\" <<'EOF'\nimport sys\n{edit_of('sys.argv[1]')}EOF",
        f"export S='{SCRATCH}/labels.py'; python3 - ${{S}} <<'EOF'\nimport sys\n{edit_of('sys.argv[1]')}EOF",
        f"cd /repo\nS={SCRATCH}/x.py\npython3 - \"$S\" '{SCRATCH}/y' <<'EOF'\nimport sys\n{edit_of('sys.argv[1]')}EOF",
        (
            f"python3 - {SCRATCH}/a.py <<'EOF'\nimport sys\ns = open(sys.argv[1]).read()\n"
            "open(sys.argv[1], 'w').write(s.replace('x', 'y'))\nEOF"
        ),
        (
            f"python3 - <<'EOF'\nfrom pathlib import Path\np = Path('{SCRATCH}/a.py')\n"
            "p.write_text(p.read_text().replace('x', 'y'))\nEOF"
        ),
        (
            'python3 -c "import sys; p=sys.argv[1]; s=open(p).read(); '
            f"open(p,'w').write(s.replace('x','y'))\" {SCRATCH}/a.py"
        ),
    ],
)
def test_an_edit_of_a_file_under_tmp_passes(monkeypatch, capsys, command):
    assert run_hook(monkeypatch, capsys, command) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        f"python3 - <<'EOF'\n{edit_of(repr('/tmp/../home/carver/a.py'))}EOF",
        f"cd /tmp && python3 - <<'EOF'\n{edit_of(repr('a.py'))}EOF",
        f"python3 - \"$S\" <<'EOF'\nimport sys\n{edit_of('sys.argv[1]')}EOF",
        f"S=/home/carver/repo/a.py && python3 - \"$S\" <<'EOF'\nimport sys\n{edit_of('sys.argv[1]')}EOF",
        f"python3 - {SCRATCH}/a.py <<'EOF'\nimport sys\n{edit_of('sys.argv[2]')}EOF",
        (
            f"python3 - <<'EOF'\nfor p in ['{SCRATCH}/a.py', 'repo/b.py']:\n"
            "    s = open(p).read()\n    open(p, 'w').write(s.replace('x', 'y'))\nEOF"
        ),
        (
            f"python3 - <<'EOF'\na = '{SCRATCH}/a.py'\nb = 'b.py'\n"
            "open(a, 'w').write(open(a).read().replace('x', 'y'))\n"
            "open(b, 'w').write(open(b).read().replace('x', 'y'))\nEOF"
        ),
        f"python3 - <<'EOF'\np = '{SCRATCH}/a.py'\np = 'repo/b.py'\n{edit_of('p')}EOF",
        (
            f"python3 - <<'EOF'\np = '{SCRATCH}/a.py'\nfor p in ['repo/b.py']:\n"
            "    s = open(p).read()\n    open(p, 'w').write(s.replace('x', 'y'))\nEOF"
        ),
    ],
)
def test_an_edit_not_provably_under_tmp_is_still_refused(monkeypatch, capsys, command):
    code, err = run_hook(monkeypatch, capsys, command)

    assert code == 2
    assert "apply-edits" in err


HOME = "/home/carver/f.py"
BS = chr(92)
ARGV1 = "import sys\n" + edit_of("sys.argv[1]")


def tmp_then(extra: str) -> str:
    """An edit of p = '/tmp/x' where ``extra`` rebinds p before the edit."""
    return (
        "python3 - <<'EOF'\np = '/tmp/x'\n"
        f"{extra}s = open(p).read()\nopen(p, 'w').write(s.replace('x', 'y'))\nEOF"
    )


# Ways a path outside /tmp could pass for one under it (found in review).
@pytest.mark.parametrize(
    "command",
    [
        f"S={HOME} && python3 - \"$S\" <<'EOF'\n{ARGV1}EOF\nS=/tmp/x",
        f"S={HOME} && python3 - \"$S\" <<'EOF'\n# S=/tmp/x\n{ARGV1}EOF",
        f'S={HOME}; echo " S=/tmp/x"; python3 - "$S" <<\'EOF\'\n{ARGV1}EOF',
        f"S=/tmp/x python3 - \"$S\" <<'EOF'\n{ARGV1}EOF",
        f'S="/tmp/x"/../../home/carver/f.py; python3 - "$S" <<\'EOF\'\n{ARGV1}EOF',
        f"X=../home/carver/f.py; S=/tmp/$X; python3 - \"$S\" <<'EOF'\n{ARGV1}EOF",
        f"S=/tmp/$(printf ../home/carver/f.py); python3 - \"$S\" <<'EOF'\n{ARGV1}EOF",
        f"S=/tmp/x; S+=/../../home/carver/f.py; python3 - \"$S\" <<'EOF'\n{ARGV1}EOF",
        f"S=/tmp/x; for S in {HOME}; do python3 - \"$S\" <<'EOF'\n{ARGV1}EOF\ndone",
        f"S={HOME}; (S=/tmp/x); python3 - \"$S\" <<'EOF'\n{ARGV1}EOF",
        f"cat > notes <<'EOF'\nS=/tmp/x\nEOF\npython3 - \"$S\" <<'PY'\n{ARGV1}PY",
        f"python3 - /tmp/{{a,../../home/carver/f.py}} /tmp/b <<'EOF'\n{edit_of('sys.argv[2]')}EOF",
        f"python3 - /home/carver/*.py /tmp/b <<'EOF'\nimport sys\n{edit_of('sys.argv[2]')}EOF",
        f"python3 - /tmp/x <<'EOF'\nimport sys\nsys.argv[1] = '{HOME}'\n{edit_of('sys.argv[1]')}EOF",
        "python3 - <<'EOF'\n"
        + edit_of("'/tmp/" + BS + "x2e" + BS + "x2e/home/carver/f.py'")
        + "EOF",
        "python3 - <<'EOF'\n"
        + edit_of("'/tmp/" + BS + "056" + BS + "056/home/carver/f.py'")
        + "EOF",
        tmp_then("p += '/../../home/carver/f.py'\n"),
        tmp_then("if 1: p = '/home/carver/f.py'\n"),
        tmp_then("p: str = '/home/carver/f.py'\n"),
        tmp_then("(p := '/home/carver/f.py')\n"),
        tmp_then("p, q = '/home/carver/f.py', 1\n"),
        tmp_then("with open('/x') as p: pass\n"),
        tmp_then("try:\n    1/0\nexcept Exception as p:\n    pass\n"),
        tmp_then("from os import sep as p\n"),
        (
            "python3 - <<'EOF'\np = '/tmp/x'\ndef f(p):\n    s = open(p).read()\n"
            "    open(p, 'w').write(s.replace('x', 'y'))\nf('/home/carver/f.py')\nEOF"
        ),
        "python3 - <<'EOF'\nq = '/home/carver/f.py'\np = q\n"
        + edit_of("p").replace("p = p\n", "")
        + "EOF",
        f"S='/tmp/a {HOME}'; python3 - $S <<'EOF'\nimport sys\n{edit_of('sys.argv[2]')}EOF",
        f"S='/home/carver/a /x'; python3 - $S /tmp/b <<'EOF'\nimport sys\n{edit_of('sys.argv[2]')}EOF",
        f"for f in {HOME}; do python3 - \"$f\" <<'EOF'\n{ARGV1}EOF\ndone",
    ],
)
def test_a_path_that_only_looks_like_tmp_is_refused(monkeypatch, capsys, command):
    code, _ = run_hook(monkeypatch, capsys, command)

    assert code == 2


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_the_docstring(flag):
    import subprocess

    shown = subprocess.run(
        [str(SCRIPT), flag],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert shown.returncode == 0, shown.stderr
    assert "apply-edits" in shown.stdout and "Traceback" not in shown.stderr
