"""Read a Bash command's words and what they run, for a hook that judges the command.

prepare() cleans a command for shlex: it drops comments and heredoc bodies, joins
continued lines, and sets aside each $(...) or backtick substitution and each heredoc's
body, which a hook judges as scripts of their own. simple_commands() splits what's left
into the words of each simple command, without the redirections. A command shlex can't
parse, such as one with a quote left open, is split without regard to quotes.

command_of() finds the command a simple command runs, past variable settings, keywords
such as `if` and `do`, and wrappers such as timeout, nice, env, sudo and xargs, whose
options it reads with split_options(). Then SHELLS names the shells: shell_script()
finds the script one runs with -c, and reads_stdin() says whether it reads its script
from stdin. is_commit() says whether git's arguments run `git commit`, bgrun_command()
reads bgrun's, and is_help() spots an option that asks for help.
"""

import re
import shlex
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
GIT_VALUES = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
BGRUN_VALUES = {"-o", "--output", "-C", "--cwd", "--tail"}


def simple_commands(cleaned: str) -> list[list[str]]:
    """The words of each simple command in ``cleaned``, a command prepare() has cleaned."""
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    try:
        tokens = list(lexer)
    except ValueError:  # a quote left open
        tokens = FALLBACK.findall(cleaned)
    commands, words, redirected = [], [], False
    for token in tokens:
        if redirected:
            redirected = False
        elif not is_operator(token):
            words.append(token)
        elif is_redirection(token):
            if words and words[-1].isdigit():
                words.pop()  # the file descriptor of 2>FILE
            redirected = True  # the next word is the redirection's target
        else:
            commands.append(words)
            words = []
    commands.append(words)
    return [words for words in commands if words]


def is_operator(token: str) -> bool:
    return all(c in OPERATORS for c in token)


def is_redirection(operator: str) -> bool:
    """Whether an operator redirects (>, 2>&, <<<), rather than separating commands or
    starting a process substitution such as <(...)."""
    return bool(set(operator) & set("<>")) and "(" not in operator


def prepare(text: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """(``text`` ready for shlex; the bodies of its command substitutions outside single
    quotes; each heredoc's body, with the line that opened it). Drops comments and
    heredoc bodies, joins continued lines, turns $'...' into a plain quoted word and each
    substitution into a placeholder, so the word around it stays one word."""
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
            out.append(
                "'" + re.sub(r"\\.?", "_", text[i + 2 : end]) + text[end : end + 1]
            )
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
            quote = char
        elif quote == '"' and char == '"':
            quote = None
        elif text.startswith("$(", i) or char == "`":
            start = i + (2 if char == "$" else 1)
            end = substitution_end(text, start, char)
            substitutions.append(text[start:end])
            out.append("_")
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
    """Where the $( or backtick substitution whose body starts at ``start`` closes:
    len(text) if it doesn't."""
    depth, quote, i = 1, None, start
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = None if char == quote else quote
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


def is_commit(git_args: list[str]) -> bool:
    _, rest = split_options(git_args, GIT_VALUES)
    return rest[:1] == ["commit"]


def shell_flags(words: list[str]) -> tuple[str, list[str]] | None:
    """(the letters of a shell command's short options, the words after its options), or
    None when the simple command doesn't run a shell."""
    words, _ = command_of(words)
    if not words or Path(words[0]).name not in SHELLS:
        return None
    options, rest = split_options(words[1:], {"-o", "-O"})
    return "".join(o[1:] for o in options if not o.startswith("--")), rest


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
