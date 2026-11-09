"""Read a Bash command's words and what they run, for a hook that judges the command.

prepare() cleans a command for shlex: it drops comments and heredoc bodies, joins
continued lines, and sets aside each $(...), backtick or <(...) substitution and each
heredoc's body, which a hook judges as scripts of their own; with first_mark, each
substitution leaves a numbered marker, so a hook can tell which one a word holds.
commands() splits what's left into simple commands: the words of each, without the
redirections, which it keeps apart; whether its output goes into a pipe; and whether it
sits inside a compound command. A command shlex can't parse, such as one with a quote
left open, is split without regard to quotes; parses() says whether shlex can.

command_of() finds the command a simple command runs, past variable settings, keywords
such as `if` and `do`, and wrappers such as timeout, nice, env, sudo and xargs, whose
options it reads with split_options(). Then SHELLS names the shells: shell_script()
finds the script one runs with -c, reads_stdin() says whether it reads its script
from stdin, and set_pipefail() whether set's or a shell's options turn pipefail on.
commit_args() finds the arguments of a `git commit`, bgrun_command() reads
bgrun's, and is_help() spots an option that asks for help.
"""

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Characters shlex returns as operator tokens: separators and redirections.
OPERATORS = ";&|()<>\n`"
# Where a `#` starts a comment: at a word's start.
WORD_BREAK = " \t\n;&|()<>"
# A heredoc's opener, not a here-string's `<<<`; the delimiter is a name, maybe quoted.
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*\\?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words of a quote left open, for a command shlex can't parse.
FALLBACK = re.compile(r"[;&|()<>\n`]+|[^\s;&|()<>`]+")
SHELLS = {"sh", "bash", "dash", "ksh", "zsh"}
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{"}
# The words that open and close a compound command whose output a pipe can take whole.
OPENERS = {"{", "if", "for", "while", "until", "select", "case"}
CLOSERS = {"}", "fi", "done", "esac"}
# A quoted word of these alone, such as '|', which shlex returns unquoted, as an operator.
OPERATOR_WORD = re.compile(r"[;&|()<>]+")
# Commands that run the command after them: (their options that take a value, how many
# arguments come between their options and that command).
WRAPPERS = {
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "nohup": (set(), 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "sudo": (
        {"-u", "--user", "-g", "--group", "-C", "-D", "--chdir", "-p", "-r", "-t"},
        0,
    ),
    "command": (set(), 0),
    "exec": ({"-a"}, 0),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "ionice": ({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "-u"}, 0),
    "taskset": (set(), 1),
    "chrt": (set(), 1),
    "setsid": (set(), 0),
    "xargs": ({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--max-args"}, 0),
    "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1),
}
# What prepare() leaves for substitution N when given first_mark: characters from
# Unicode's private use area, which shlex keeps inside a word and a command never holds.
MARK_FORMAT = "{}"
MARK = re.compile("([0-9]+)")
GIT_VALUES = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
BGRUN_VALUES = {"-o", "--output", "-C", "--cwd", "--tail"}


@dataclass
class Command:
    """A simple command's words; whether its output goes into a pipe, because `|` or `|&`
    follows it or a compound command around it; whether it sits inside a compound
    command: ( ... ), { ...; }, if ... fi, case ... esac, or a for, while, until or
    select loop; and its redirections, each as (operator, target), such as (">>", "log")
    or ("<<", "EOF"). A redirection after a compound command's close, as in
    `( ... ) > log`, goes to the last simple command before it."""

    words: list[str]
    piped: bool
    grouped: bool
    redirects: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Output:
    """Whether the output of a simple or compound command goes into a pipe, and whether
    the compound command is a case, whose patterns end in a `)` that closes nothing."""

    piped: bool = False
    case: bool = False


def _lexer(cleaned: str) -> shlex.shlex:
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    return lexer


def parses(cleaned: str) -> bool:
    """Whether shlex can split ``cleaned``, a command prepare() has cleaned: no quote
    is left open."""
    try:
        list(_lexer(cleaned))
    except ValueError:
        return False
    return True


def commands(cleaned: str) -> list[Command]:
    """Each simple command in ``cleaned``, a command prepare() has cleaned, in order."""
    try:
        tokens = list(_lexer(cleaned))
    except ValueError:  # a quote left open
        tokens = FALLBACK.findall(cleaned)
    # Each command's words, with its own output and those of the compound commands
    # around it, and its redirections; the compound commands open now, innermost last;
    # the output a `|` would take now; the words of the command being read, and its
    # redirections so far.
    found: list[tuple[list[str], list[_Output], list[tuple[str, str]]]] = []
    groups: list[_Output] = []
    pipe_source: _Output | None = None
    words: list[str] = []
    redirects: list[tuple[str, str]] = []

    def end() -> None:
        nonlocal words, pipe_source, redirects
        if not words:
            if redirects and found:  # after a compound command's close
                found[-1][2].extend(redirects)
                redirects = []
            return
        closed = None
        for word in words:
            if word in OPENERS:
                groups.append(_Output(case=word == "case"))
            elif word in CLOSERS:
                closed = groups.pop() if groups else None
            elif word not in KEYWORDS:
                break
        own = _Output()
        found.append((words, [own, *groups], redirects))
        pipe_source, words, redirects = closed or own, [], []

    def close() -> None:
        """A `)`: it ends a subshell, or a case's pattern."""
        nonlocal pipe_source
        end()
        if groups and groups[-1].case:
            pipe_source = None
        else:
            pipe_source = groups.pop() if groups else None

    redirection = None  # the operator whose target the next token is
    for token in tokens:
        if redirection is not None:
            redirects.append((redirection, token))
            redirection = None
            continue
        if not is_operator(token):
            words.append(token)
            continue
        i = 0
        while i < len(token):  # shlex runs operators together, as in `)|` or `;>`
            char = token[i]
            if char in "<>" or token.startswith("&>", i):
                # The rest of the token redirects, and the next one is its target.
                if words and words[-1].isdigit():
                    words.pop()  # the file descriptor of 2>FILE
                redirection = token[i:]
                break
            if char == ")":
                close()
            else:
                end()
                if token.startswith("||", i):
                    i += 1
                elif char == "|" and pipe_source:  # the & of a |& only separates
                    pipe_source.piped = True
                elif char == "(":
                    groups.append(_Output())
                pipe_source = None
            i += 1
    end()
    return [
        Command(words, any(o.piped for o in outputs), len(outputs) > 1, redirects)
        for words, outputs, redirects in found
    ]


def is_operator(token: str) -> bool:
    return bool(token) and all(c in OPERATORS for c in token)


def prepare(
    text: str, first_mark: int | None = None
) -> tuple[str, list[str], list[tuple[str, str]]]:
    """(``text`` ready for shlex; the bodies of its command substitutions outside single
    quotes and of its process substitutions outside quotes; each heredoc's body, with the
    line that opened it). Drops comments and heredoc bodies, joins continued lines, turns
    $'...' into a plain quoted word and each substitution into a placeholder, so the word
    around it stays one word. The placeholder is `_`, or with ``first_mark`` a marker
    (MARK) numbered from it, the substitutions in order. A quoted word of operator
    characters alone, such as '|', becomes underscores, since shlex drops the quotes and
    would return it as an operator."""
    out, substitutions, heredocs = [], [], []
    delimiters, line_start, quote, i = [], 0, None, 0
    while i < len(text):
        char = text[i]
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == "\\":
            out.append(" " if text[i + 1 : i + 2] == "\n" else text[i : i + 2])
            i += 2
            continue
        elif quote is None and text.startswith("$'", i):
            end = ansi_end(text, i + 2)
            body = re.sub(r"\\.?", "_", text[i + 2 : end])
            if OPERATOR_WORD.fullmatch(body):
                body = "_" * len(body)
            out.append("'" + body + text[end : end + 1])
            i = end + 1
            continue
        elif quote is None and char == "#" and (i == 0 or text[i - 1] in WORD_BREAK):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif quote is None and char == "\n" and delimiters:
            opener = text[line_start:i]
            bodies, i = heredoc_bodies(text, i + 1, delimiters)
            heredocs += [(opener, body) for body in bodies]
            delimiters, line_start = [], i
            out.append("\n")
            continue
        elif quote is None and (heredoc := HEREDOC.match(text, i)):
            delimiters.append(heredoc.group(2))
            out.append(heredoc.group(0))
            i = heredoc.end()
            continue
        elif quote is None and char in "'\"":
            end = text.find(char, i + 1)
            if end != -1 and OPERATOR_WORD.fullmatch(text, i + 1, end):
                out.append(char + "_" * (end - i - 1) + char)
                i = end + 1
                continue
            quote = char
        elif quote == '"' and char == '"':
            quote = None
        elif (
            text.startswith("$(", i)
            or char == "`"
            or quote is None
            and text.startswith(("<(", ">("), i)
        ):
            start = i + (1 if char == "`" else 2)
            end = substitution_end(text, start, char)
            if first_mark is None:
                out.append("_")
            else:
                out.append(MARK_FORMAT.format(first_mark + len(substitutions)))
            substitutions.append(text[start:end])
            i = end + 1
            continue
        if char == "\n":
            line_start = i + 1
        out.append(char)
        i += 1
    heredocs += [(text[line_start:], "") for _ in delimiters]  # opened on the last line
    return "".join(out), substitutions, heredocs


def heredoc_bodies(
    text: str, start: int, delimiters: list[str]
) -> tuple[list[str], int]:
    """(the bodies of the heredocs one line opened, read from ``start`` on; where the text
    after the last one's closing line starts). A heredoc left open takes the rest."""
    bodies, body, i = [], [], start
    while i < len(text) and len(bodies) < len(delimiters):
        end = text.find("\n", i)
        end = len(text) if end == -1 else end
        line, i = text[i:end], end + 1
        if line.strip() == delimiters[len(bodies)]:
            bodies.append("\n".join(body))
            body = []
        else:
            body.append(line)
    if len(bodies) < len(delimiters):
        bodies.append("\n".join(body))
    return bodies, min(i, len(text))


def ansi_end(text: str, start: int) -> int:
    """Where the $'...' word whose body starts at ``start`` closes: len(text) if it doesn't."""
    i = start
    while i < len(text) and text[i] != "'":
        i += 2 if text[i] == "\\" else 1
    return min(i, len(text))


def substitution_end(text: str, start: int, opener: str) -> int:
    """Where the substitution ($(, <(, >( or a backtick) whose body starts at ``start``
    closes: len(text) if it doesn't. Heredoc bodies inside it are skipped, so a quote in
    one, as in a commit message's "Don't", opens nothing."""
    depth, quote, delimiters, i = 1, None, [], start
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = None if char == quote else quote
        elif char == "\n" and delimiters:
            i = heredoc_bodies(text, i + 1, delimiters)[1]
            delimiters = []
            continue
        elif heredoc := HEREDOC.match(text, i):
            delimiters.append(heredoc.group(2))
            i = heredoc.end()
            continue
        elif opener == "`":
            if char == "`":
                return i
        elif char in "'\"":
            quote = char
        elif char in "()":
            depth += 1 if char == "(" else -1
            if depth == 0:
                return i
        i += 1
    return len(text)


def command_of(words: list[str]) -> tuple[list[str], set[str]]:
    """(the words of the command a simple command runs, past variable settings, keywords
    and wrappers; the variables set in front of it). No words when it runs none, as for
    `command -v`."""
    assigned = set()
    while words:
        word, name = words[0], Path(words[0]).name
        if ASSIGNMENT.match(word):
            assigned.add(word.split("=", 1)[0])
            words = words[1:]
        elif word in KEYWORDS:
            words = words[1:]
        elif name in WRAPPERS:
            values, between = WRAPPERS[name]
            options, words = split_options(words[1:], values)
            if name == "command" and {"-v", "-V"} & set(options):
                return [], assigned
            words = words[between:]
        else:
            break
    return words, assigned


def split_options(args: list[str], values: set[str]) -> tuple[list[str], list[str]]:
    """(the options in front of ``args``, the words after them and the `--` that may end
    them). An option in ``values``, or a long option that abbreviates one, takes the next
    word as its value."""
    options, i = [], 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        option = args[i]
        i += 1
        if option == "--":
            break
        options.append(option)
        abbreviated = (
            option.startswith("--")
            and "=" not in option
            and any(v.startswith(option) for v in values)
        )
        if option in values or abbreviated:
            i += 1
    return options, args[i:]


def is_help(option: str) -> bool:
    return option == "-h" or len(option) > 2 and "--help".startswith(option)


def bgrun_command(args: list[str]) -> tuple[bool, list[str]] | None:
    """(whether bgrun waits for its command, the command's words), from bgrun's
    arguments, or None when they ask for help."""
    options, rest = split_options(args, BGRUN_VALUES)
    if any(map(is_help, options)):
        return None
    return any(len(o) > 2 and "--wait".startswith(o) for o in options), rest


def commit_args(git_args: list[str]) -> list[str] | None:
    """The arguments of the `git commit` that git's arguments run, or None when they
    run some other git command."""
    _, rest = split_options(git_args, GIT_VALUES)
    return rest[1:] if rest[:1] == ["commit"] else None


def shell_options(args: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """(each option in front of ``args``, as set and the shells read them, with the
    words it takes; the words after them and the `--` that may end them). An option
    starts with - or +. Each o or O in a cluster of short options takes one word, as in
    -euo pipefail, and a long option such as --norc takes none."""
    options, i = [], 0
    while i < len(args) and len(args[i]) > 1 and args[i][0] in "-+":
        option = args[i]
        i += 1
        if option == "--":
            break
        taken = 0 if option.startswith("--") else option.count("o") + option.count("O")
        options.append((option, args[i : i + taken]))
        i += taken
    return options, args[i:]


def set_pipefail(args: list[str], pipefail: bool) -> bool:
    """``pipefail`` after set's or a shell's options ``args``: -o pipefail turns it on,
    as does a cluster such as -euo pipefail, and +o pipefail off."""
    for option, values in shell_options(args)[0]:
        if "o" in option and "pipefail" in values:
            pipefail = option[0] == "-"
    return pipefail


def shell_flags(words: list[str]) -> tuple[str, list[str]] | None:
    """(the letters of a shell command's short options that start with -, the words
    after its options), or None when the simple command doesn't run a shell."""
    words, _ = command_of(words)
    if not words or Path(words[0]).name not in SHELLS:
        return None
    options, rest = shell_options(words[1:])
    on = [option for option, _ in options if option[0] == "-" and option[1] != "-"]
    return "".join(option[1:] for option in on), rest


def shell_script(words: list[str]) -> str | None:
    """The script a shell command runs with -c, or None."""
    parsed = shell_flags(words)
    if parsed is None:
        return None
    letters, rest = parsed
    return rest[0] if "c" in letters and rest else None


def reads_stdin(words: list[str]) -> bool:
    """Whether a shell command reads its script from stdin: -s, or neither -c nor a
    script file."""
    parsed = shell_flags(words)
    if parsed is None:
        return False
    letters, rest = parsed
    return "c" not in letters and (not rest or "s" in letters)
