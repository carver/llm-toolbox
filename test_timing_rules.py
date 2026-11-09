"""Tests wait by polling, and say why when they sleep a fixed time or send SIGSTOP.

A fixed sleep costs its whole length on every run, and on a loaded machine it can end
before what it waits for. A call to time.sleep with a number of SHORTEST seconds or
more needs a `# fixed sleep: <why>` comment, on its line or on a comment line just
above it, for a sleep that is the scenario itself: a window in which something must
not happen. A process stopped with SIGSTOP may be holding a lock that every other
process in the test waits on, so each use of SIGSTOP needs a `# SIGSTOP: <why>`
comment on its line. Once ruff format splits a long line, any line of the statement
counts as its line. A sleep whose length is computed isn't checked, nor is code a
test runs from a string."""

import ast
import io
import os
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHORTEST = 0.2  # a polling loop's pause between looks is shorter
SLEEP_REASON = re.compile(r"#\s*fixed sleep:\s*\S")
SIGSTOP_REASON = re.compile(r"#\s*SIGSTOP:\s*\S")


def comments(text):
    """{line number: (its comment, whether the comment is all the line holds)}, read
    as tokens, so a # inside a string is no comment."""
    found = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            alone = not token.line[: token.start[1]].strip()
            found[token.start[0]] = (token.string, alone)
    return found


def statement_lines(tree):
    """{id of each node in a simple statement: that statement's first and last lines}.
    A node in a compound statement's header, such as an if's test, has none."""
    lines = {}
    for statement in ast.walk(tree):
        if isinstance(statement, ast.stmt) and not {"body", "cases"} & set(
            statement._fields
        ):
            for node in ast.walk(statement):
                lines[id(node)] = (statement.lineno, statement.end_lineno)
    return lines


def unexplained(text, tree, nodes, reason, above):
    """The first line of each of nodes that has no comment matching reason.

    The comment may sit on any line of the simple statement that holds the node,
    since ruff format moves a split line's comment to its last line; a node in a
    compound statement's header has only its own lines. When above, it may also
    sit on a line of nothing but the comment, just above those lines."""
    if not nodes:
        return []
    notes, spans = comments(text), statement_lines(tree)

    def says_why(first, last):
        if any(
            reason.match(notes.get(line, ("", False))[0])
            for line in range(first, last + 1)
        ):
            return True
        comment, alone = notes.get(first - 1, ("", False))
        return above and alone and bool(reason.match(comment))

    found = set()
    for node in nodes:
        first, last = spans.get(id(node), (node.lineno, node.end_lineno))
        if not says_why(first, last):
            found.add(first)
    return sorted(found)


def bound(tree, module, member):
    """The names tree's imports bind to module, and those they bind to its member."""
    modules, members = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == module}
        elif isinstance(node, ast.ImportFrom) and node.module == module:
            members |= {a.asname or a.name for a in node.names if a.name == member}
    return modules, members


def is_sleep(func, modules, functions):
    """Whether func is time.sleep: `t.sleep` with t a name for the time module, or a
    name for its sleep."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        )
    return isinstance(func, ast.Name) and func.id in functions


def is_sigstop(node, names):
    """Whether node is SIGSTOP: any attribute of that name, or a name for signal's."""
    if isinstance(node, ast.Attribute):
        return node.attr == "SIGSTOP"
    return isinstance(node, ast.Name) and node.id in names


def long_sleeps(text):
    """The lines of text's calls to time.sleep with a number of SHORTEST seconds or
    more that no `# fixed sleep:` comment explains, on their lines or just above."""
    if "sleep" not in text:  # every way to reach time.sleep names it
        return []
    tree = ast.parse(text)
    modules, functions = bound(tree, "time", "sleep")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 1:
            continue
        if not is_sleep(node.func, modules, functions):
            continue
        seconds = node.args[0]
        if (
            isinstance(seconds, ast.Constant)
            and type(seconds.value) in (int, float)
            and seconds.value >= SHORTEST
        ):
            found.append(node)
    return unexplained(text, tree, found, SLEEP_REASON, above=True)


def sigstops(text):
    """The lines of text's uses of SIGSTOP that no `# SIGSTOP:` comment on their lines
    explains."""
    if "SIGSTOP" not in text:
        return []
    tree = ast.parse(text)
    _, names = bound(tree, "signal", "SIGSTOP")
    found = [node for node in ast.walk(tree) if is_sigstop(node, names)]
    return unexplained(text, tree, found, SIGSTOP_REASON, above=False)


def sources():
    """The repo's test_*.py and conftest.py files, outside hidden folders such as
    .git and .venv."""
    for folder, subfolders, names in os.walk(ROOT):
        subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
        for name in sorted(names):
            if (
                name == "conftest.py"
                or name.startswith("test_")
                and name.endswith(".py")
            ):
                yield Path(folder, name)


def located(find):
    return [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sources()
        for line in find(path.read_text(encoding="utf-8"))
    ]


def test_every_long_fixed_sleep_says_why():
    found = located(long_sleeps)
    assert not found, (
        f"fixed sleeps of {SHORTEST:g} s or more at {', '.join(found)}. Poll the"
        " condition with a deadline instead, as conftest's wait_until does: a"
        " fixed sleep costs its whole length on every run, and under load it can end"
        " before what it waits for. When the sleep is the scenario itself, a window in"
        " which something must not happen, say so in a `# fixed sleep: <why>` comment"
        " on its line or the line above."
    )


def test_every_sigstop_says_why():
    found = located(sigstops)
    assert not found, (
        f"SIGSTOP at {', '.join(found)}. A stopped process may hold a lock that every"
        " other process in the test waits on: have it wait for a file the test"
        " creates instead, or say why it can't in a `# SIGSTOP: <why>` comment on the"
        " line."
    )


SLEEPS = """
import time
import time as clock
from time import sleep
from time import sleep as nap

time.sleep(0.2)
time.sleep(0.19)
time.sleep(1)
clock.sleep(2)
sleep(3)
nap(4)
time.sleep(5)  # fixed sleep: a window that must pass
# fixed sleep: a window in which nothing may happen
time.sleep(6)
# fixed sleep: two lines up doesn't count

time.sleep(7)
time.sleep(8)  # fixed sleep:
x = "# fixed sleep: in a string"; time.sleep(9)
time.sleep(0.5)  # fixed sleep: for this line only
time.sleep(10)
time.sleep(
    11  # fixed sleep: on a later line of the call
)
time.sleep(
    12
)  # fixed sleep: where ruff format puts a long line's comment
if slow:  # fixed sleep: on the line of an if, which isn't the sleep's
    time.sleep(15)
time.sleep(seconds)
time.sleep(0.5 if slow else 0.05)
time.sleep(True)
asyncio.sleep(13)
other.sleep(14)

def nested():
    import time as later
    later.sleep(16)
"""


def caught(find, sample):
    lines = sample.splitlines()
    return [lines[number - 1].strip() for number in find(sample)]


def test_long_sleeps_finds_each_form_and_honours_a_reason():
    assert caught(long_sleeps, SLEEPS) == [
        "time.sleep(0.2)",
        "time.sleep(1)",
        "clock.sleep(2)",
        "sleep(3)",
        "nap(4)",
        "time.sleep(7)",
        "time.sleep(8)  # fixed sleep:",
        'x = "# fixed sleep: in a string"; time.sleep(9)',
        "time.sleep(10)",
        "time.sleep(15)",
        "later.sleep(16)",
    ]


def test_a_sleep_imported_from_elsewhere_is_not_times():
    sample = "from asyncio import sleep\nimport time\nsleep(1)\ntime.sleep(2)\n"
    assert caught(long_sleeps, sample) == ["time.sleep(2)"]


STOPS = """
import os
import signal
import signal as sig
from signal import SIGSTOP
from signal import SIGSTOP as halt

os.kill(pid, signal.SIGSTOP)
os.kill(pid, sig.SIGSTOP)
os.kill(pid, signal.Signals.SIGSTOP)
os.kill(pid, SIGSTOP)
os.kill(pid, halt)
os.kill(pid, signal.SIGSTOP)  # SIGSTOP: it holds no lock
# SIGSTOP: a comment above doesn't count
os.kill(pid, sig.Signals.SIGSTOP)
os.kill(pid, signal.SIGSTOP)  # SIGSTOP:
os.kill(pid, signal.SIGCONT)
s = "signal.SIGSTOP"
proc.send_signal(
    signal.SIGSTOP
)  # SIGSTOP: where ruff format puts a long line's comment
if proc.send_signal(signal.SIGSTOP):
    pass  # SIGSTOP: on a line of the if's body, which isn't the signal's
"""


def test_sigstops_finds_each_form_and_honours_a_reason():
    assert caught(sigstops, STOPS) == [
        "os.kill(pid, signal.SIGSTOP)",
        "os.kill(pid, sig.SIGSTOP)",
        "os.kill(pid, signal.Signals.SIGSTOP)",
        "os.kill(pid, SIGSTOP)",
        "os.kill(pid, halt)",
        "os.kill(pid, sig.Signals.SIGSTOP)",
        "os.kill(pid, signal.SIGSTOP)  # SIGSTOP:",
        "if proc.send_signal(signal.SIGSTOP):",
    ]


def test_the_scan_reaches_the_repos_tests():
    found = {path.relative_to(ROOT) for path in sources()}
    assert {Path("conftest.py"), Path("test_heavy.py")} <= found, sorted(found)
