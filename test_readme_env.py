"""Every env var a bin/ tool's -h names, its README entry names too.

A tool is documented twice, in its docstring (what -h prints) and in README.md,
and an edit to one is easy to leave out of the other."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TOOLS = sorted(p for p in (ROOT / "bin").iterdir() if p.is_file())
ENV_VAR = re.compile(r"\$\{?([A-Z][A-Z0-9_]+)")


def readme_entry(name: str) -> str:
    """The README bullet for a tool: from the line bolding its name to the next."""
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if f"**{name}**" in line), None)
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(("- **", "#"))),
        len(lines),
    )
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("tool", TOOLS, ids=lambda p: p.name)
def test_readme_names_every_env_var_the_help_names(tool):
    docstring = ast.get_docstring(ast.parse(tool.read_text(encoding="utf-8"))) or ""
    entry = readme_entry(tool.name)
    missing = sorted(
        {v for v in ENV_VAR.findall(docstring) if not re.search(rf"\b{v}\b", entry)}
    )
    assert not missing, f"README's {tool.name} entry doesn't name {missing}"
