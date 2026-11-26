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
heredocs() lists the heredoc bodies bash would hand on, each with the delimiter that
delimiter_of() reads from the word naming it. commit_args() finds the arguments of a
`git commit`, bgrun_command() reads bgrun's, and is_help() spots an option that asks
for help. exports() lists the names a script's `export` commands name.
"""

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# How every gate's refusal ends, before lib/refused.py's sentence naming where it saved
# the command. A refused Bash call runs none of its parts, and an agent once took a
# refusal of an inline edit to cover only that part, so a heredoc before it that wrote
# a file was never redone.
NOTHING_RAN = "Nothing in this command ran, the parts before the refused one included."

# Characters shlex returns as operator tokens: separators and redirections.
OPERATORS = ";&|()<>\n`"
# Where a `#` starts a comment: at a word's start.
WORD_BREAK = " \t\n;&|()<>"
# A heredoc's opener, not a here-string's `<<<`: group 1 its dash, group 2 the delimiter
# word as written, quotes and all. The word starts with a quote, a backslash or a name's
# first character, so arithmetic's `1<<2` opens none.
_QUOTED = r"'[^'\n]*'|\"[^\"\n]*\"|\\."
_WORD_PART = r"[^\s;&|()<>'\"\\`]"
HEREDOC = re.compile(
    rf"(?<!<)<<(?!<)(-?)[ \t]*((?:{_QUOTED}|[A-Za-z_])(?:{_QUOTED}|{_WORD_PART})*)"
)
QUOTE_PART = re.compile(r"'([^']*)'|\"([^\"]*)\"|\\(.)")
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(rf"^{VARIABLE_NAME.pattern}=")
# Words of a quote left open, for a command shlex can't parse.
FALLBACK = re.compile(r"[;&|()<>\n`]+|[^\s;&|()<>`]+")
SHELLS = {"sh", "bash", "dash", "ksh", "zsh"}
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{"}
# The words that open and close a compound command whose output a pipe can take whole.
OPENERS = {"{", "if", "for", "while", "until", "select", "case"}
CLOSERS = {"}", "fi", "done", "esac"}
# The openers whose condition, up to `then` or `do`, bash runs with `set -e` ignored.
CONDITIONS = {"if", "while", "until"}
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
    `( ... ) > log`, goes to the last simple command before it. `fed`: a `|` comes
    right before it, so its stdin is the output of the command or compound command
    before it, as in `{ ...; } | CMD`. `subshells`: the `( ... )` subshells it sits
    in, each a number of its own, outermost first, so a `cd` inside one can be told
    to end with it. `after`: the operator that ends it (`&&`, `||`, `|`, `;`, `&`,
    a newline, `(` or `)`), or "" at the end of the command. `depth`: how many
    compound commands it sits in. `closes`: when `after` is a `)`, the operator
    after each subshell it closes, innermost first, as in `;` for `(a; b); c`.
    `unchecked`: bash ignores `set -e` for it, since it sits in an if, while or until
    condition, after `!`, or in a compound command that a non-final part of an `&&`
    or `||` list is, as in `{ ...; } || echo`."""

    words: list[str]
    piped: bool
    grouped: bool
    redirects: list[tuple[str, str]] = field(default_factory=list)
    fed: bool = False
    subshells: tuple[int, ...] = ()
    after: str = ""
    depth: int = 0
    closes: list[str] = field(default_factory=list)
    unchecked: bool = False


@dataclass
class _Output:
    """Whether the output of a simple or compound command goes into a pipe, whether
    the compound command is a case, whose patterns end in a `)` that closes nothing,
    the number of the `( ... )` subshell it is, if it is one, whether its condition
    (an if's, while's or until's) is being read, and whether it is a non-final part
    of an && or || list."""

    piped: bool = False
    case: bool = False
    subshell: int | None = None
    condition: bool = False
    nonfinal: bool = False


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
    # redirections so far, the operators after the subshells it closes, and whether
    # it sits in a condition.
    found: list[
        tuple[
            list[str], list[_Output], list[tuple[str, str]], bool, str, list[str], bool
        ]
    ] = []
    groups: list[_Output] = []
    pipe_source: _Output | None = None
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    fed = False  # a `|` came since the last command ended
    subshells = 0  # how many ( ... ) subshells have opened so far
    shut: _Output | None = None  # the subshell a `)` just closed

    def end(after: str = "") -> None:
        nonlocal words, pipe_source, redirects, fed, shut
        if shut is not None and found:
            if not words:
                found[-1][5].append(after)
            shut.nonfinal = after in ("&&", "||")
        shut = None
        if not words:
            if redirects and found:  # after a compound command's close
                found[-1][2].extend(redirects)
                redirects = []
            return
        closed = None
        negated = False
        for word in words:
            if word in OPENERS:
                groups.append(
                    _Output(case=word == "case", condition=word in CONDITIONS)
                )
            elif word in CLOSERS:
                closed = groups.pop() if groups else None
                if closed is not None:
                    closed.nonfinal = after in ("&&", "||")
            elif word in KEYWORDS:
                if word == "elif" and groups:
                    groups[-1].condition = True
                elif word in ("then", "do") and groups:
                    groups[-1].condition = False
                negated = negated or word == "!"
            else:
                break
        own = _Output()
        condition = negated or any(group.condition for group in groups)
        found.append((words, [own, *groups], redirects, fed, after, [], condition))
        pipe_source, words, redirects, fed = closed or own, [], [], False

    def close() -> None:
        """A `)`: it ends a subshell, or a case's pattern."""
        nonlocal pipe_source, shut
        end(")")
        if groups and groups[-1].case:
            pipe_source = None
        else:
            pipe_source = groups.pop() if groups else None
            shut = pipe_source

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
                pair = token[i : i + 2]
                end(pair if pair in ("&&", "||") else char)
                if pair in ("&&", "||"):
                    i += 1
                elif char == "|":  # the & of a |& only separates
                    fed = True
                    if pipe_source:
                        pipe_source.piped = True
                elif char == "(":
                    subshells += 1
                    groups.append(_Output(subshell=subshells))
                pipe_source = None
            i += 1
    end()
    return [
        Command(
            words,
            any(o.piped for o in outputs),
            len(outputs) > 1,
            redirects,
            fed,
            tuple(o.subshell for o in outputs[1:] if o.subshell is not None),
            after,
            len(outputs) - 1,
            closes,
            condition or any(o.nonfinal for o in outputs[1:]),
        )
        for words, outputs, redirects, fed, after, closes, condition in found
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
    cleaned, substitutions, found = _scan(text, first_mark)
    return cleaned, substitutions, [(h.opener, "\n".join(h.lines)) for h in found]


def heredocs(text: str) -> list[tuple[str, str]]:
    """Each top-level heredoc of ``text`` that bash would close: (its delimiter, its
    body as bash hands it on, the final newline kept and, for <<-, the leading tabs
    stripped). One left open, or with an empty body, is left out."""
    found = []
    for heredoc in _scan(text, None)[2]:
        lines = heredoc.lines
        if heredoc.dash:
            lines = [line.lstrip("\t") for line in lines]
        if heredoc.closed and lines:
            found.append((heredoc.delimiter, "".join(line + "\n" for line in lines)))
    return found


def delimiter_of(word: str) -> str:
    """A heredoc's delimiter as bash reads it: the word with its quoting removed."""
    return QUOTE_PART.sub(lambda part: "".join(g or "" for g in part.groups()), word)


@dataclass
class _Heredoc:
    opener: str  # the line that opened it
    delimiter: str
    dash: bool  # <<-, whose body bash hands on without its leading tabs
    lines: list[str]  # its body's lines, as written
    closed: bool  # whether its closing line came


def _scan(text, first_mark):
    """prepare()'s cleaned text and substitutions, then each heredoc, in order."""
    out, substitutions, found = [], [], []
    opened, line_start, quote, i = [], 0, None, 0
    closed_at = -1  # where the last substitution closed
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
        elif quote is None and char == "#" and comment_starts(text, i, 0, closed_at):
            i = line_end(text, i)
            continue
        elif quote is None and char == "\n" and opened:
            opener = text[line_start:i]
            read, i = heredoc_bodies(text, i + 1, opened)
            found += [
                _Heredoc(opener, d, dash, lines, closed)
                for (d, dash), (lines, closed) in zip(opened, read)
            ]
            opened, line_start = [], i
            out.append("\n")
            continue
        elif quote is None and (heredoc := HEREDOC.match(text, i)):
            opened.append((delimiter_of(heredoc.group(2)), bool(heredoc.group(1))))
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
            closed_at, i = end, end + 1
            continue
        if char == "\n":
            line_start = i + 1
        out.append(char)
        i += 1
    # Opened on the last line, so no body follows.
    found += [_Heredoc(text[line_start:], d, dash, [], False) for d, dash in opened]
    return "".join(out), substitutions, found


def closes_heredoc(line: str, delimiter: str, dash: bool) -> bool:
    """Whether ``line`` closes a heredoc as bash reads it: a line that is the delimiter
    exactly, or, for <<-, after its leading tabs. A space before or after it, or a tab
    under plain <<, leaves the line in the body. A CR that ends the line is part of
    bash's delimiter in a CRLF command, which HEREDOC's word leaves out, so it counts
    as none here."""
    line = line.removesuffix("\r")
    return (line.lstrip("\t") if dash else line) == delimiter


def heredoc_bodies(
    text: str, start: int, delimiters: list[tuple[str, bool]]
) -> tuple[list[tuple[list[str], bool]], int]:
    """(each heredoc one line opened, given as its delimiter and whether it is <<-,
    read from ``start`` on: its body's lines and whether its closing line came; where
    the text after the last one's closing line starts). A heredoc left open takes the
    rest."""
    read, body, i = [], [], start
    while i < len(text) and len(read) < len(delimiters):
        end = text.find("\n", i)
        end = len(text) if end == -1 else end
        line, i = text[i:end], end + 1
        if closes_heredoc(line, *delimiters[len(read)]):
            read.append((body, True))
            body = []
        else:
            body.append(line)
    while len(read) < len(delimiters):
        read.append((body, False))
        body = []
    return read, min(i, len(text))


def ansi_end(text: str, start: int) -> int:
    """Where the $'...' word whose body starts at ``start`` closes: len(text) if it doesn't."""
    i = start
    while i < len(text) and text[i] != "'":
        i += 2 if text[i] == "\\" else 1
    return min(i, len(text))


def comment_starts(text: str, i: int, start: int, closed_at: int) -> bool:
    """Whether the # at ``i`` starts a comment: it is the first character from
    ``start`` on, or follows a word break. A `)` that closes a substitution, at
    ``closed_at``, breaks no word: `$(echo a)#b` is the word a#b."""
    return i == start or text[i - 1] in WORD_BREAK and i - 1 != closed_at


def line_end(text: str, i: int) -> int:
    """Where the line holding ``i`` ends: its newline, or len(text)."""
    end = text.find("\n", i)
    return len(text) if end == -1 else end


def substitution_end(text: str, start: int, opener: str) -> int:
    """Where the substitution ($(, <(, >( or a backtick) whose body starts at ``start``
    closes: len(text) if it doesn't. Heredoc bodies inside it are skipped, so a quote in
    one, as in a commit message's "Don't", opens nothing, and so are comments in a $(,
    <( or >(, where a "don't" or a ")" would otherwise open a quote or close it early.
    Bash finds a backtick's close before reading comments, so there a # skips nothing."""
    # For each ( open inside the body, whether it opens a substitution: $(, <( or >(.
    opened: list[bool] = []
    quote, delimiters, closed_at, i = None, [], -1, start
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
            delimiters.append((delimiter_of(heredoc.group(2)), bool(heredoc.group(1))))
            i = heredoc.end()
            continue
        elif opener == "`":
            if char == "`":
                return i
        elif char == "#" and comment_starts(text, i, start, closed_at):
            i = line_end(text, i)
            continue
        elif char in "'\"":
            quote = char
        elif char == "(":
            opened.append(i > start and text[i - 1] in "$<>")
        elif char == ")":
            if not opened:
                return i
            if opened.pop():
                closed_at = i
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


def exports(script: str) -> set[str]:
    """The names the `export` commands in ``script`` name: each word after `export` but
    its options, up to its `=` or `+=`, as in `export A B=1 C+=2`. A word that isn't a
    name there, as one written through an expansion, gives none: each substitution is
    a marker, so `export $(...)` doesn't read as the name `_`."""
    names = set()
    for command in commands(prepare(script, first_mark=0)[0]):
        words, _ = command_of(command.words)
        if words[:1] == ["export"]:
            for word in words[1:]:
                name = word.partition("=")[0].removesuffix("+")
                if VARIABLE_NAME.fullmatch(name):
                    names.add(name)
    return names


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


def set_errexit(args: list[str], errexit: bool) -> bool:
    """``errexit`` after set's options ``args``: -e (alone or in a cluster such as
    -euo pipefail) or -o errexit turns it on, +e or +o errexit off."""
    for option, values in shell_options(args)[0]:
        if option.startswith("--"):
            continue
        if "e" in option or "o" in option and "errexit" in values:
            errexit = option[0] == "-"
    return errexit


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
