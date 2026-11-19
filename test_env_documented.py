"""Every environment variable a bin/ tool or lib/ module reads, its docstring names.
A variable its Environment table describes as for the tests is named after the tool
or a lib/ module (CODING_STANDARDS.md, Tools).

A tool's docstring is its -h, and test_readme_env.py holds its README entry to that,
so a variable the code reads but the docstring leaves out is one nobody can find.
A read is `os.environ.get`, `.setdefault` or `.pop`, `os.getenv`, `os.environ[...]`
or `in os.environ` with a literal name, or a call with a literal name to a function
of these files that reads the variable one of its parameters names, such as
`heavyqueue.knob("HEAVY_POLL", 1)`, counted where the call is."""

import ast
import re
from pathlib import Path

import pytest

from test_readme_env import env_rows

ROOT = Path(__file__).resolve().parent
SOURCES = sorted(
    [p for p in (ROOT / "bin").iterdir() if p.is_file()]
    + list((ROOT / "lib").rglob("*.py"))
)

# Set by something else, and only passed on or followed: no tool's to document.
PASSED_THROUGH = {
    "PATH": "the shell's",
    "HOME": "the user's home",
    "PYTHONPATH": "Python's, passed on to the interpreters a tool starts",
    "XDG_CACHE_HOME": "the XDG cache folder, which tools follow when it's set",
    "CLAUDE_CODE_SESSION_ID": "Claude Code sets it in every session",
    "CLAUDE_CONFIG_DIR": "Claude Code's own config folder",
    "AI_AGENT": "Claude Code sets it in its shells",
}
PASSED_THROUGH_PREFIXES = ("GIT_",)  # git's own, set inside hooks


def parse(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def is_environ(node):
    """`os.environ`, or `environ` imported from os."""
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )
    return isinstance(node, ast.Name) and node.id == "environ"


def named(node):
    """The expression that names the variable node reads, or None when it reads none."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in ("get", "setdefault", "pop") and is_environ(func.value):
                return node.args[0]
            if (
                func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                return node.args[0]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and is_environ(node.value)
    ):
        return node.slice
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and is_environ(node.comparators[0])
    ):
        return node.left
    return None


def constants(tree):
    """The module's `NAME = "text"` assignments, as {NAME: text}."""
    found = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def literal(node, consts):
    """The text node stands for: a string, or a module constant holding one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def wrappers(trees):
    """{function name: the index, among its call's arguments, of the parameter whose
    variable it reads}, over trees. A method's self or cls isn't an argument."""
    found = {}
    for tree in trees:
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [arg.arg for arg in func.args.args]
            if params[:1] in (["self"], ["cls"]):
                params = params[1:]
            for node in ast.walk(func):
                name = named(node)
                if isinstance(name, ast.Name) and name.id in params:
                    found[func.name] = params.index(name.id)
    return found


def reads(tree, wrappers):
    """The variables tree reads, directly or through one of wrappers."""
    consts = constants(tree)
    found = set()
    for node in ast.walk(tree):
        name = literal(named(node), consts)
        if name is None and isinstance(node, ast.Call):
            func = node.func
            callee = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            index = wrappers.get(callee)
            if index is not None and len(node.args) > index:
                name = literal(node.args[index], consts)
        if name:
            found.add(name)
    return found


def passed_through(name):
    return name in PASSED_THROUGH or name.startswith(PASSED_THROUGH_PREFIXES)


TREES = {path: parse(path) for path in SOURCES}
WRAPPERS = wrappers(tree for tree in TREES.values() if tree is not None)
TOOLS_WITH_DOCS = [
    p for p, tree in TREES.items() if tree is not None and p.parent.name == "bin"
]


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_docstring_names_every_variable_the_code_reads(source):
    tree = TREES[source]
    if tree is None:
        pytest.skip("not Python")
    doc = ast.get_docstring(tree) or ""
    missing = sorted(
        name
        for name in reads(tree, WRAPPERS)
        if not passed_through(name) and not re.search(rf"\b{name}\b", doc)
    )
    assert not missing, f"{source.relative_to(ROOT)}'s docstring doesn't name {missing}"


SAMPLE = """
import os
from os import environ

def knob(name, default):
    return float(os.environ.get(name, default))

a = os.environ.get("A_GET")
b = os.getenv("B_GETENV")
c = os.environ["C_ITEM"]
d = "D_IN" in os.environ
e = environ.setdefault("E_SETDEFAULT", "x")
f = knob("F_KNOB", 1)
g = mod.knob("G_ATTR_KNOB", 1)
h = os.environ.get(some_name)
i = os.environ.get("PATH")
j = "J_NOT_IN" not in os.environ
k = os.environ.get("GIT_DIR")
os.environ["W_WRITE"] = "x"
del os.environ["W_DEL"]
KEY = "L_CONSTANT"
m = os.environ.get(KEY)

class Settings:
    def value(self, name):
        return os.environ.get(name)

n = settings.value("N_METHOD")
"""


def test_reads_finds_each_form_and_reads_through_a_helper():
    tree = ast.parse(SAMPLE)
    found = reads(tree, wrappers([tree]))
    # Writes and deletes aren't reads; a constant's value and a method's parameter
    # are followed.
    assert found == {
        "A_GET",
        "B_GETENV",
        "C_ITEM",
        "D_IN",
        "E_SETDEFAULT",
        "F_KNOB",
        "G_ATTR_KNOB",
        "PATH",
        "J_NOT_IN",
        "GIT_DIR",
        "L_CONSTANT",
        "N_METHOD",
    }
    assert {name for name in found if passed_through(name)} == {"PATH", "GIT_DIR"}


# Modules and packages, such as lib/paperkit/.
LIB_MODULES = sorted(
    p.stem
    for p in (ROOT / "lib").iterdir()
    if p.suffix == ".py" or (p / "__init__.py").is_file()
)


def misnamed_test_knobs(tool: str, doc: str, lib_modules: list[str]) -> list[str]:
    """The variables doc's Environment table describes as for the tests whose names
    start with neither the tool's name nor a lib/ module's, upper case with - as _
    (CODING_STANDARDS.md, Tools)."""
    prefixes = tuple(
        name.upper().replace("-", "_") + "_" for name in [tool, *lib_modules]
    )
    return [
        name
        for name, description in env_rows(doc)
        if re.search(r"\btests?\b", description) and not name.startswith(prefixes)
    ]


KNOBS_SAMPLE = """Do a thing.

Environment:
  MY_TOOL_GRACE    seconds to wait (tests set it short)
  FIFO_LOCK_SECONDS  seconds for the lock (default 5; the tests
                   set it short)
  SHORT_WAIT       seconds between looks, which the
                   tests set to 0
  WAIT_PID         the process to wait for, by its
                   PID (tests)
  OPENAI_API_KEY   the key it sends
"""


def test_a_test_knob_must_be_named_after_its_tool_or_a_lib_module():
    assert misnamed_test_knobs("my-tool", KNOBS_SAMPLE, ["fifo"]) == [
        "SHORT_WAIT",
        "WAIT_PID",
    ]


def test_lib_packages_count_as_modules():
    assert "paperkit" in LIB_MODULES


@pytest.mark.parametrize("source", TOOLS_WITH_DOCS, ids=lambda p: p.name)
def test_each_test_knob_is_named_after_its_tool(source):
    doc = ast.get_docstring(TREES[source]) or ""
    # A link, such as claude-json to claude-fresh, prints its target's docstring.
    misnamed = misnamed_test_knobs(source.resolve().name, doc, LIB_MODULES)
    assert not misnamed, (
        f"{source.name}'s test knobs {misnamed} aren't named after it: "
        "CODING_STANDARDS.md, Tools"
    )
