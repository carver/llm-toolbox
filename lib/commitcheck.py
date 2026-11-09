"""Check the commits of a branch before its tip with the repo's agent check, each
checked out in turn in the worktree, as wt-ship does before it lands or pushes, and keep
the worktree safe while it does.

While the work tree holds an earlier commit, a marker in the worktree's own git dir
names the branch's tip and the process checking (lib/fifo.py's Holder: host, PID and
start time). agent-commit-gate refuses a commit there while that process lives, since
the commit would go on the earlier commit and be lost when the tip comes back; the
checks that process starts carry its PID in CHECKING_EARLIER_COMMITS_PID, and the gate
runs them. A marker whose process has ended means a run was killed before it put the
tip back: the next run finds it and says how to put the tip back.

The caller makes a stop unwind through the checks (wt-ship turns SIGTERM, SIGHUP and
SIGINT into SystemExit), so the tip comes back and the marker goes. A git command that
fails raises GitError, whose message says which and what git said; wt-ship uses this
module's git() for its own commands too.
"""

import os
import signal
import subprocess
from contextlib import ExitStack, suppress
from functools import partial
from pathlib import Path
from typing import NamedTuple

import fifo
import heavyqueue
import passrecord
from fifo import Holder

HOOK_NAME = "agent-pre-commit"
MARKER = "checking-earlier-commits"
# The modes of an executable file, or a link to one, in a git tree.
RUNNABLE_MODES = ("100755", "120000")


class GitError(Exception):
    """A git command failed; the message names it and what git printed."""


def git(*args, check=True):
    run = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and run.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed:\n{run.stdout}{run.stderr}".rstrip()
        )
    return run


def out(*args):
    return git(*args).stdout.strip()


def gate_env():
    """The environment to run agent-commit-gate --run in for a landing's check: as a
    git hook runs it, and never with AGENT_CHECK_AT_COMMIT, which would let the commit
    check stand in for the full one."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_CHECK_AT_COMMIT"}
    return env | {"AGENT_PRE_COMMIT": "1"}


def run_stoppable(argv, env=None, cwd=None):
    """Run argv to its end, in a process group of its own, and return its exit status.
    When this run is stopped meanwhile, that whole group is stopped too, so no check
    outlives it."""
    process = subprocess.Popen(argv, env=env, cwd=cwd, start_new_session=True)
    try:
        return process.wait()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        process.wait()
        raise


def hook_path():
    """Where the repo's agent check lives, whether or not it's there: the hooks
    directory git uses (`core.hooksPath`, else the common git dir's hooks)."""
    # --git-path is relative to the current directory, which may be a subdirectory.
    return Path(out("rev-parse", "--git-path", "hooks")).resolve() / HOOK_NAME


def runnable(hook):
    return hook.is_file() and os.access(hook, os.X_OK)


def in_tree(hook):
    """The hook's path in the work tree's commits, when the repo keeps its hooks
    there; None when they live outside it, as in .git/hooks."""
    top = Path(out("rev-parse", "--show-toplevel")).resolve()
    try:
        return hook.relative_to(top).as_posix()
    except ValueError:
        return None


def holds_check(commit, path):
    """Whether `commit` has an executable file, or a link, at `path`."""
    # --full-tree: ls-tree reads paths relative to the current directory otherwise.
    listed = git(
        "ls-tree", "--full-tree", commit, "--", path, check=False
    ).stdout.split()
    return bool(listed) and listed[0] in RUNNABLE_MODES


def dropping_check(upstream, hook):
    """The first commit in upstream..HEAD, oldest first, without the agent check, when
    the repo keeps it in its tree and upstream has it; None otherwise. Such a commit
    would land with nothing to check."""
    path = in_tree(hook)
    if path is None or not holds_check(upstream, path):
        return None
    for sha in out("rev-list", "--reverse", f"{upstream}..HEAD").split():
        if not holds_check(sha, path):
            return sha
    return None


def passed(commit, hook):
    """Whether `commit`'s tree passed the agent check at `hook`, as its bytes are now,
    by agent-commit-gate's record."""
    return passrecord.passed(out("rev-parse", f"{commit}^{{tree}}"), hook)


def unchecked(upstream, hook):
    """The commits in upstream..HEAD, HEAD included, oldest first, whose trees have no
    pass of the agent check at `hook`, as its bytes are now."""
    # rev-list, not log, which a config such as log.showSignature adds lines to.
    commits = out("rev-list", "--reverse", f"{upstream}..HEAD").split()
    return [sha for sha in commits if not passed(sha, hook)]


# The exit status agent-commit-gate --run --no-queue gives a check that needs heavy's
# queue (agent-commit-gate's QUEUE_EXIT).
QUEUE_EXIT = 75


class Marker(NamedTuple):
    path: Path
    holder: Holder
    tip: str
    branch: str  # empty for a detached worktree

    def restore_command(self):
        """The command that puts the worktree back at its tip."""
        if self.branch:
            return f"git checkout --force {self.branch}"
        return f"git checkout --force --detach {self.tip}"


def marker_path():
    """The marker's path in this worktree's own git dir; None outside a repository."""
    run = git("rev-parse", "--absolute-git-dir", check=False)
    return Path(run.stdout.strip(), MARKER) if run.returncode == 0 else None


def marker():
    """This worktree's marker; None when there's none or it can't be read."""
    path = marker_path()
    try:
        text = path.read_text() if path else None
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines()[1:] if "=" in line)
    return Marker(
        path, Holder.parse(text), fields.get("tip", ""), fields.get("branch", "")
    )


def blocking_commits():
    """The marker of another process checking earlier commits in this worktree, while
    that process may still run; None otherwise. For agent-commit-gate, which refuses
    a commit then. The checks that process starts name it in OWNER_ENV."""
    found = marker()
    if found is None or found.holder.gone():
        return None
    owner = os.environ.get("CHECKING_EARLIER_COMMITS_PID")
    if found.holder.pid is not None and found.holder.pid == owner:
        return None
    return found


def first_failing(hook, commits, gate, front=False):
    """Check out each of `commits` in turn, oldest first, index and work tree, forced,
    since some checks read the work tree, and run the agent check at `hook` through
    `gate --run`; return the first that fails, or None. Two rounds: each commit first
    with --no-queue, which runs a check that needs no build or test, such as a
    docs-only commit's, outside heavy's queue; then those whose check needs the queue
    all in one heavy job, `front` putting it at the queue's front, so no other job
    runs between them. The oldest commit that fails is the one returned, whichever
    round finds it. A commit without the hook has nothing to check (dropping_check
    catches one that should have it). The worktree's marker stands for the whole run,
    and HEAD comes back to where it was, on its branch, however the run ends; the
    marker goes once it has. The caller made sure no tracked file has changes."""
    top = out("rev-parse", "--show-toplevel")
    ref = git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    tip = out("rev-parse", "HEAD")
    path = marker_path()
    me = Holder.mine()
    # Whole, so a run killed mid-write can't leave a marker nobody can read.
    fifo.write_whole(path, f"{me.line()}tip={tip}\nbranch={branch}\n")
    env = gate_env() | {"CHECKING_EARLIER_COMMITS_PID": me.pid}
    # The check runs at the top of the work tree, as git runs a hook.
    argv = [str(gate), "--run", str(hook)]
    try:
        queued = []
        rest = partial(in_one_job, argv=argv, env=env, top=top, front=front)
        for sha in commits:
            check_out(sha, say=False)
            if not runnable(hook):
                print(
                    f"  {named(sha)}: no {HOOK_NAME} at this commit, so nothing to check"
                )
                continue
            code = run_stoppable([*argv, "--no-queue"], env, cwd=top)
            if code == QUEUE_EXIT:
                queued.append(sha)
            elif code != 0:
                # An older commit set aside for the queue may fail too: it goes first.
                return (rest(queued) if queued else None) or sha
        return rest(queued) if queued else None
    finally:
        git("checkout", "-q", "--force", *([branch] if branch else ["--detach", tip]))
        path.unlink(missing_ok=True)


def named(sha):
    """A commit as messages name it: its SHA's first 12 digits and its subject."""
    return out("log", "-1", "--format=%h %s", "--abbrev=12", sha)


def check_out(sha, say=True):
    if say:
        print(f"  {named(sha)}", flush=True)
    git("checkout", "-q", "--force", "--detach", sha)


def in_one_job(commits, argv, env, top, front):
    """Run the check `argv` on each of `commits`, checked out in turn, inside one heavy
    job, where each gate run starts at once; the first that fails, or None. A queue
    that can't be used leaves each check to queue for itself, as the gate does."""
    print(
        f"Checking the {len(commits)} that need heavy's queue in one job"
        f"{' at its front' if front else ''}:",
        flush=True,
    )
    cores = os.sched_getaffinity(0)
    with ExitStack() as stack:
        stack.callback(restore_cores, cores)
        try:
            turn = heavyqueue.turn(command="wt-ship's checks", hint=False, front=front)
            job = stack.enter_context(turn)
        except heavyqueue.QueueError as error:
            print(f"heavy: can't use its queue ({error}), so each check queues alone.")
            job = None
        if job is not None:
            job.pin()
            env = env | {
                key: job.env[key]
                for key in ("HEAVY_SLOT", "CARGO_BUILD_JOBS")
                if key in job.env
            }
        for sha in commits:
            check_out(sha)
            if run_stoppable(argv, env, cwd=top) != 0:
                if job is not None:
                    job.exited(1)
                return sha
        if job is not None:
            job.exited(0)  # a passing run goes into heavy's history
        return None


def restore_cores(cores):
    """Give this process back the cores it had before a job pinned it; a failure only
    leaves it on fewer, as on_one_core's does in agent-commit-gate."""
    with suppress(OSError):
        os.sched_setaffinity(0, cores)
