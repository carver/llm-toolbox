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
ENV_TABLE = re.compile(r"^Environment[^\n]*:\n((?:[ \t]+\S[^\n]*\n?)+)", re.MULTILINE)
ENV_ROW = re.compile(r"([ \t]+)([A-Z][A-Z0-9_]+)\b")


def env_vars(docstring: str) -> set[str]:
    """The variables a docstring names: as `$NAME`, or opening a line of its
    Environment table (a line starting `Environment` and ending in `:`, then indented
    lines) at the table's first indentation. A wrapped description, indented deeper,
    names none."""
    found = set(ENV_VAR.findall(docstring))
    for block in ENV_TABLE.findall(docstring):
        lines = block.splitlines()
        indent = len(lines[0]) - len(lines[0].lstrip())
        for line in lines:
            listed = ENV_ROW.match(line)
            if listed and len(listed.group(1)) == indent:
                found.add(listed.group(2))
    return found


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
        {v for v in env_vars(docstring) if not re.search(rf"\b{v}\b", entry)}
    )
    assert not missing, f"README's {tool.name} entry doesn't name {missing}"


@pytest.mark.parametrize(
    "docstring, names",
    [
        (
            (
                "Environment:\n  FOO_DIR   the folder, wrapped onto\n"
                "            PR and other words\n  BAR_N     a count\n"
            ),
            {"FOO_DIR", "BAR_N"},
        ),
        ("Environment variables:\n\tFOO_DIR\tthe folder\n", {"FOO_DIR"}),
        ("Uses $FOO_DIR.\n", {"FOO_DIR"}),
        ("Options:\n  ALSO_CAPS   not a variable\n", set()),
    ],
    ids=["wrapped-line", "tabs", "dollar", "other-section"],
)
def test_env_vars_reads_the_table_and_dollar_names_only(docstring, names):
    assert env_vars(docstring) == names
