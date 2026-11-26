"""Where a Bash command's parts run, and whether a directory is a repo's main tree.

The owner's rule is that agents edit in a linked worktree, and a repo's main tree only
receives landings. A hook that holds agents to it needs two answers, and each must say
when it can't tell, so the hook can let the command through:

walk(command, cwd) lists each simple command in a Bash command with the directory it
runs in: the hook's cwd, moved by each `cd DIR` or `pushd DIR` before it in its scope (a
`cd` inside `( ... )`, a pipeline or a list sent to the background with `&` ends with
it), by `git -C DIR`, and by bgrun's `-C DIR`. The directory is None whenever it isn't
certain: a `cd` to a word with an expansion (`"$WT"`, `~/x`, `$(...)`), `cd -`, `popd`,
a plain `cd`, a sourced file or an eval before it, a function defined anywhere in the
command, an `env -C` or `sudo -D` in front, a `GIT_DIR` or `GIT_WORK_TREE` set before
the command or exported earlier, or `git --git-dir`/`--work-tree`. A directory that
doesn't exist yet, as when `git worktree add` runs earlier in the same command, is given
as written, and place() then says it can't tell. Each step also names the earlier cds
in its scope whose failure wouldn't have stopped it, each with the folder the shell
would have stayed in: one that ends with `;` or a newline, unless `set -e` held for it
(not in a condition, nor in a compound command that a non-final part of an `&&` or
`||` list is, nor anywhere once the command defines a function), and one chained with
`&&` to a list that then ended with `;` or a newline; less those before a cd to an
absolute folder that the step is chained to. The script a shell runs with -c is walked
from the directory the shell starts in, and a $(...) substitution's body from an
unknown one.

place(directory) asks git what the directory is: its work tree's top level, whether
that is the main tree (its git dir is the common one), the hooks folder, the branch,
and what would make a commit there legitimate: an unborn HEAD (a new repo's first
commit), a submodule, a bare repo, or a merge, cherry-pick, revert, rebase or sequencer
in progress. It returns None when git can't answer: not a repo, a directory that
doesn't exist, a git that fails or runs past GIT_SECONDS.

lib/paperkit/git.py keeps a copy of the main-tree test, since paperkit is vendored into
projects without this folder; test_maintree.py holds the two to the same answers.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from shellparse import (
    ASSIGNMENT,
    BGRUN_VALUES,
    MARK,
    bgrun_command,
    command_of,
    commands,
    prepare,
    set_errexit,
    shell_script,
)

# Variables that point git at another repo or work tree than the directory's.
GIT_PLACE_VARIABLES = {"GIT_DIR", "GIT_WORK_TREE"}
# git's own options whose value is the next word, and those that move where it works.
GIT_VALUES = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
GIT_ELSEWHERE = ("--git-dir", "--work-tree")
# Options of the wrappers command_of() passes that change the directory: env -C and
# --chdir, sudo -D and --chdir.
CHDIR_OPTIONS = re.compile(r"-C|-D|--chdir(=.*)?|-C.+")
# Commands that run the command after their own options, which walk() looks past:
# bgrun (its -C DIR moves it) and heavy.
RUNNERS = {"bgrun", "heavy"}
# A word whose value the shell would work out: a variable, a substitution (`` ` ``, or
# prepare()'s marker), a glob or a home directory.
UNCERTAIN = re.compile(r"[$`*?\[]|^~")
# Commands that run shell code in the current shell, which may change its directory.
SOURCES = {".", "source", "eval"}
# A function definition, `name() {` or `function name`, outside quoted words.
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
FUNCTION = re.compile(r"(^|[\s;&|(])(function\s+\S|[\w.:-]+\s*\(\s*\))")
# The operators after a `cd` that don't wait for it to succeed.
LOOSE = (";", "\n")
# The operators that end an && or || list, so a chained cd's failure no longer stops
# what comes next.
LIST_ENDS = (";", "\n", "&", "")
GIT_SECONDS = 5
# Place.exempt for what the repo is, rather than what's under way in it.
BARE, SUBMODULE, UNBORN = "a bare repo", "a submodule", "an unborn HEAD"
# The files and folders in a git dir that mark an operation in progress.
IN_PROGRESS = (
    ("MERGE_HEAD", "a merge"),
    ("CHERRY_PICK_HEAD", "a cherry-pick"),
    ("REVERT_HEAD", "a revert"),
    ("rebase-merge", "a rebase"),
    ("rebase-apply", "a rebase"),
    ("sequencer", "a cherry-pick or revert of several commits"),
)


@dataclass(frozen=True)
class LooseCd:
    """A cd whose failure wouldn't stop a later step: its `words`, the folder the
    shell stays in if it fails (`fallback`, None when that isn't certain), and its
    place among the command's simple commands (`index`)."""

    words: list[str]
    fallback: Path | None
    index: int


@dataclass
class Step:
    """A simple command: `words`, the command it runs (past variable settings,
    wrappers, bgrun and heavy); `directory`, where it runs, or None when that isn't
    certain; `assigned`, the variables set in front of it, with their values as
    written; its redirections, as shellparse.Command gives them; `loose_cd`, the
    earlier cds in its scope whose failure wouldn't stop it, in order: each one that
    ends with `;` or a newline while `set -e` holds for it, or that a `&&` chains to
    commands whose list then ends with `;` or a newline, less those before a cd to an
    absolute folder chained to the step itself; `guess`, the directory from its plain
    `cd`s and -C options alone, which a sourced file, a function, GIT_DIR, `env -C`
    or a piped group's cd don't undo: for a check that reads an unknown directory as
    a reason to act rather than to pass."""

    words: list[str]
    directory: Path | None
    assigned: dict[str, str] = field(default_factory=dict)
    redirects: list[tuple[str, str]] = field(default_factory=list)
    loose_cd: list[LooseCd] = field(default_factory=list)
    guess: Path | None = None


@dataclass(frozen=True)
class _Chained:
    """A cd that `&&` chains to the rest of its list: while the list goes on, its
    failure stops what comes next. `depth`: how many compound commands it sits in;
    `covers`: whether it names a certain absolute folder, so the steps chained to it
    run there whatever earlier cds did."""

    cd: LooseCd
    depth: int
    covers: bool


@dataclass(frozen=True)
class _Scope:
    """Where a scope's shell stands: its directory, the guess, its loose cds and its
    chained cds."""

    directory: Path | None
    guess: Path | None
    loose: tuple[LooseCd, ...] = ()
    chained: tuple[_Chained, ...] = ()

    def loose_for_step(self) -> list[LooseCd]:
        cover = max((c.cd.index for c in self.chained if c.covers), default=-1)
        return [cd for cd in self.loose if cd.index > cover]

    def ended(self, depth: int) -> "_Scope":
        """This scope once a list ends at `depth`: the cds chained in it are loose."""
        done = [c.cd for c in self.chained if c.depth >= depth]
        if not done:
            return self
        kept = tuple(c for c in self.chained if c.depth < depth)
        loose = tuple(sorted((*self.loose, *done), key=lambda cd: cd.index))
        return replace(self, loose=loose, chained=kept)


@dataclass
class GitCall:
    """A git command: `subcommand` and `args` after git's own options, and the
    directory it works in (its step's, moved by -C), or None when that isn't certain;
    `guess`, its step's guess moved by -C."""

    subcommand: str
    args: list[str]
    directory: Path | None
    step: Step
    guess: Path | None = None


def certain(word: str) -> bool:
    """Whether a word is plain text, with nothing for the shell to work out."""
    return bool(word) and not UNCERTAIN.search(word) and not MARK.search(word)


def moved(directory: Path | None, word: str) -> Path | None:
    """`directory` after a cd to `word`, as bash's logical cd works it out; None
    when the word isn't certain, or is relative to a directory that isn't."""
    if not certain(word) or directory is None and not word.startswith("/"):
        return None
    if directory is None:
        return Path(os.path.normpath(word))
    return Path(os.path.normpath(directory / word))


def cd_target(words: list[str]) -> tuple[bool, str | None]:
    """(whether the command changes directory, the word it changes to, or None when
    that can't be told) for a `cd`, `pushd` or `popd`."""
    name, args = words[0], words[1:]
    if name == "popd":
        return True, None
    if name == "pushd":
        if args[:1] == ["-n"]:
            return False, None
        return True, args[0] if len(args) == 1 and args[0][:1] not in "+-" else None
    while args and args[0] in ("-L", "-P", "-e", "-@"):
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    return True, args[0] if len(args) == 1 and args[0] != "-" else None


def runner_command(words: list[str]) -> tuple[list[str], str | None] | None:
    """(the command bgrun or heavy runs, bgrun's -C directory or None), or None when
    `words` runs neither, or asks it for help."""
    name, args = Path(words[0]).name, words[1:]
    if name == "bgrun":
        parsed = bgrun_command(args)
        if parsed is None:
            return None
        return parsed[1], bgrun_cwd(args)
    if name == "heavy":
        return (args[args.index("--") + 1 :] if "--" in args else []), None
    return None


def bgrun_cwd(args: list[str]) -> str | None:
    """The directory bgrun's -C (--cwd) names among its options `args`, or None."""
    cwd, i = None, 0
    while i < len(args) and args[i].startswith("-") and args[i] not in ("-", "--"):
        option, i = args[i], i + 1
        name, equals, value = option.partition("=")
        if option == "-C" or len(name) > 3 and "--cwd".startswith(name):
            if equals:
                cwd = value
            else:
                cwd, i = (args[i] if i < len(args) else None), i + 1
        elif option.startswith("-C"):
            cwd = option[2:]
        elif not equals and (
            option in BGRUN_VALUES
            or len(option) > 2
            and any(v.startswith(option) for v in BGRUN_VALUES if v.startswith("--"))
        ):
            i += 1
    return cwd


def walk(command: str, cwd: Path | None) -> list[Step]:
    """Each simple command in `command`, run from `cwd`, with the directory it runs
    in, in order; then those of each $(...) substitution, from an unknown directory."""
    cleaned, substitutions, _ = prepare(command, first_mark=0)
    has_function = bool(FUNCTION.search(QUOTED.sub("''", cleaned)))
    steps = _walk(cleaned, cwd, has_function)
    if has_function:
        # A function's body runs where it's called, not where it's defined.
        for step in steps:
            step.directory = None
    for body in substitutions:
        steps += walk(body, None)
    return steps


def background_lists(simples) -> list[tuple[int, int]]:
    """(first, last) index of each list of commands joined by &&, || or | that ends
    with `&`, and so runs in the background."""
    found, start = [], 0
    for index, simple in enumerate(simples):
        if simple.after in ("&&", "||", "|"):
            continue
        if simple.after == "&":
            found.append((start, index))
        start = index + 1
    return found


def _walk(cleaned: str, cwd: Path | None, has_function: bool) -> list[Step]:
    # Where the shell stands in each scope: the subshells a command sits in, as
    # shellparse numbers them. A scope seen first starts from the nearest one around
    # it, which is where the shell stood when the subshell opened.
    places: dict[tuple[int, ...], _Scope] = {(): _Scope(cwd, cwd)}
    steps: list[Step] = []
    # Whether `set -e` is on in each scope, so a cd that fails ends it. A function
    # may be called where bash ignores it, as in `f || true`, so then it never counts.
    errexit: dict[tuple[int, ...], bool] = {(): False}
    elsewhere = False  # GIT_DIR or GIT_WORK_TREE exported, or set for the shell
    simples = commands(cleaned)
    # A list sent to the background, as in `cd WT && sleep 900 &`, runs in a
    # subshell: its cds move the commands in it, and the shell stays where it was.
    # (the index of its last command, its scope, that scope's place before it)
    saved: tuple[int, tuple[int, ...], tuple] | None = None
    background = dict(background_lists(simples))
    for index, simple in enumerate(simples):
        if saved is not None and index > saved[0]:
            places[saved[1]] = saved[2]
            saved = None
        if index in background and simple.subshells in places:
            saved = (background[index], simple.subshells, places[simple.subshells])
        scope = simple.subshells
        for size in range(len(scope), -1, -1):
            if scope[:size] in places:
                places.setdefault(scope, places[scope[:size]])
                break
        for size in range(len(scope), -1, -1):
            if scope[:size] in errexit:
                errexit.setdefault(scope, errexit[scope[:size]])
                break
        directory, guess = places[scope].directory, places[scope].guess
        words, _ = command_of(simple.words)
        prefix = simple.words[: len(simple.words) - len(words)]
        if any(CHDIR_OPTIONS.fullmatch(word) for word in prefix):
            here = None
        else:
            here = directory
        guessed = guess
        while words and (found := runner_command(words)) is not None:
            inner, runner_cwd = found
            if runner_cwd is not None:
                here = moved(here, runner_cwd)
                guessed = moved(guessed, runner_cwd)
            words, _ = command_of(inner)
            prefix += inner[: len(inner) - len(words)]
        assigned = dict(word.split("=", 1) for word in prefix if ASSIGNMENT.match(word))
        if not words:
            if GIT_PLACE_VARIABLES & assigned.keys():
                elsewhere = True
            _end_lists(places, simple)
            continue
        name = Path(words[0]).name
        if name in ("export", "declare", "typeset") and any(
            word.partition("=")[0] in GIT_PLACE_VARIABLES for word in words[1:]
        ):
            elsewhere = True
        if elsewhere or GIT_PLACE_VARIABLES & assigned.keys():
            here = None
        step = Step(
            words,
            here,
            assigned,
            simple.redirects,
            places[scope].loose_for_step(),
            guessed,
        )
        steps.append(step)
        if name == "set":
            errexit[scope] = set_errexit(words[1:], errexit[scope])
        elif name in ("cd", "pushd", "popd"):
            changes, target = cd_target(words)
            # A cd in a pipeline, or sent to the background, runs in a subshell:
            # alone it moves nothing, but in a group, as in `(cd x && git commit) |
            # tail`, it moves the commands beside it, which a scope can't tell
            # from those after the pipeline.
            apart = simple.piped or simple.fed or simple.after == "&"
            if changes and not (apart and not simple.grouped):
                after = None if target is None else moved(directory, target)
                now = places[scope]
                cd = LooseCd(words, directory, index)
                stops = errexit[scope] and not simple.unchecked and not has_function
                if simple.after in LOOSE and not stops:
                    now = replace(now, loose=(*now.loose, cd))
                elif simple.after == "&&":
                    covers = target is not None and certain(target)
                    covers = covers and target[:1] == "/"
                    chained = _Chained(cd, simple.depth, covers)
                    now = replace(now, chained=(*now.chained, chained))
                places[scope] = replace(
                    now,
                    directory=None if apart else after,
                    guess=None if target is None else moved(guess, target),
                )
        elif words[0] in SOURCES:
            # A sourced file or an eval may cd anywhere.
            places[scope] = replace(places[scope], directory=None)
        elif (script := shell_script(words)) is not None:
            steps += walk(script, here)
        _end_lists(places, simple)
    return steps


def _end_lists(places: dict[tuple[int, ...], _Scope], simple) -> None:
    """Make loose the cds chained in each list that `simple` ends: its own, by the
    operator after it, and those around each subshell it closes."""
    ends = [(simple.depth, simple.after)]
    ends += [(simple.depth - 1 - k, op) for k, op in enumerate(simple.closes)]
    for depth, op in ends:
        if op not in LIST_ENDS:
            continue
        for size in range(len(simple.subshells), -1, -1):
            if (scope := simple.subshells[:size]) in places:
                places[scope] = places[scope].ended(depth)


def git_calls(command: str, cwd: Path | None) -> list[GitCall]:
    """Each git command in `command`, run from `cwd`, with the directory it works in."""
    calls = []
    for step in walk(command, cwd):
        if not step.words or Path(step.words[0]).name != "git":
            continue
        directory, guess, rest = step.directory, step.guess, step.words[1:]
        while rest and rest[0].startswith("-") and rest[0] != "--":
            option = rest.pop(0)
            name = option.split("=", 1)[0]
            if name in GIT_ELSEWHERE:
                directory = None
            if option in GIT_VALUES and rest:
                value = rest.pop(0)
                if option == "-C":
                    directory = moved(directory, value)
                    guess = moved(guess, value)
        if rest:
            calls.append(GitCall(rest[0], rest[1:], directory, step, guess))
    return calls


@dataclass(frozen=True)
class Place:
    """What git says a directory is. `top`: its work tree's top level (the git dir,
    for a bare repo); `main`: whether that is the repo's main tree, its git dir the
    common one; `hooks`: the folder git takes hooks from; `branch`: HEAD's branch, or
    None when detached; `exempt`: why a commit there needs no worktree, or None."""

    top: Path
    main: bool
    hooks: Path
    branch: str | None
    exempt: str | None


def git(directory: Path, *args: str) -> str | None:
    """A git command's output in `directory`, or None when it fails or runs past
    GIT_SECONDS. Without the GIT_* variables a hook may have, which would point git
    at another repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        run = subprocess.run(
            ["git", *args],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return run.stdout if run.returncode == 0 else None


def place(directory: Path | None) -> Place | None:
    """What git says `directory` is, or None when it can't answer."""
    if directory is None or not directory.is_dir():
        return None
    found = git(
        directory,
        "rev-parse",
        "--is-bare-repository",
        "--absolute-git-dir",
        "--path-format=absolute",
        "--git-common-dir",
        "--git-path",
        "hooks",
    )
    lines = found.splitlines() if found else []
    if len(lines) != 4:
        return None
    bare, git_dir, common, hooks = lines[0] == "true", *map(Path, lines[1:])
    main = git_dir.resolve() == common.resolve()
    if bare:
        return Place(git_dir, main, hooks, None, BARE)
    found = git(
        directory, "rev-parse", "--show-toplevel", "--show-superproject-working-tree"
    )
    lines = found.splitlines() if found else []
    if not lines:
        return None
    top = Path(lines[0])
    branch = git(directory, "symbolic-ref", "-q", "HEAD")
    branch = branch.strip().removeprefix("refs/heads/") if branch else None
    exempt = None
    if len(lines) > 1:
        exempt = SUBMODULE
    elif git(directory, "rev-parse", "-q", "--verify", "HEAD^{commit}") is None:
        exempt = UNBORN
    else:
        exempt = next(
            (what for name, what in IN_PROGRESS if (git_dir / name).exists()), None
        )
        exempt = exempt and f"{exempt} in progress"
    return Place(top, main, hooks, branch, exempt)


def has_origin(top: Path) -> bool:
    """Whether the repo at `top` has a remote named origin."""
    return git(top, "config", "--get", "remote.origin.url") is not None
