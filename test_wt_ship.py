"""wt-ship, against a bare remote, a main tree and a detached worktree beside it."""

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from conftest import path_without

SCRIPT = Path(__file__).resolve().parent / "bin" / "wt-ship"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(cwd, path, text, message):
    with open(Path(cwd) / path, "a") as file:
        file.write(text)
    git(cwd, "add", path)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def fake_ci_status(folder, says="", sleep=0):
    """Make `folder` hold a `ci-status` that logs its arguments to calls.log, waits
    `sleep` seconds, then prints `says`; returns the env that puts it first on PATH."""
    folder.mkdir(exist_ok=True)
    script = folder / "ci-status"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"open({str(folder / 'calls.log')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({says!r})\n"
    )
    script.chmod(0o755)
    return {"PATH": f"{folder}:{os.environ['PATH']}"}


@pytest.fixture(autouse=True)
def quiet_ci(tmp_path, monkeypatch):
    """A silent ci-status first on PATH for every test, so none asks the real GitHub
    (one sets origin to a github.com URL); a test that wants one to speak puts its own
    in front. Returns its folder."""
    monkeypatch.setenv("PATH", fake_ci_status(tmp_path / "quiet-ci")["PATH"])
    return tmp_path / "quiet-ci"


RED = "CI is failing on main: ci.yml (check) at b227171, 2 h ago: https://example/1\n"


@pytest.mark.parametrize("mode", [[], ["--local"]], ids=["push", "local"])
def test_failing_ci_on_the_branch_is_named_before_the_landing(setup, tmp_path, mode):
    origin, main, wt, _ = setup
    env = fake_ci_status(tmp_path / "red-ci", RED)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, *mode, env=env)

    assert done.returncode == 0, done.stderr  # a warning, never a refusal
    landed = git(main, "rev-parse", "main") if mode else remote_main(origin)
    assert landed == new
    assert done.stdout.startswith(RED)
    calls = (tmp_path / "red-ci/calls.log").read_text()
    assert calls == "--remote origin --branch main\n"


def test_nothing_to_land_asks_nothing_of_ci(setup, quiet_ci):
    _, _, wt, _ = setup
    assert ship(wt, "--local").returncode == 0
    assert ship(wt).returncode == 0
    assert not (quiet_ci / "calls.log").exists()


def test_a_bad_ci_timeout_still_lands(setup, tmp_path):
    origin, _, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")
    done = ship(wt, env={"WT_SHIP_CI_TIMEOUT": "soon"})
    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new


def test_a_ci_status_with_no_answer_in_time_is_left_behind(setup, tmp_path):
    origin, _, wt, _ = setup
    env = fake_ci_status(tmp_path / "slow-ci", RED, sleep=30)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, env=env | {"WT_SHIP_CI_TIMEOUT": "0.5"})

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new
    assert "CI is failing" not in done.stdout
    # The leftover-process fixture fails the test if the sleeping ci-status outlived it.


def ship_env(session="s-mine", wait="10", **extra):
    """wt-ship's environment: short waits, so a test that waits by mistake ends."""
    return os.environ | {
        "CLAUDE_CODE_SESSION_ID": session,
        "WT_SHIP_WAIT": wait,
        "WT_SHIP_POLL": "0.05",
        **extra,
    }


def ship(cwd, *args, session="s-mine", wait="10", env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=cwd,
        env=ship_env(session, wait, **(env or {})),
        capture_output=True,
        text=True,
        check=False,
    )


def ship_in_background(cwd, logs, *args, session="s-mine", wait="10"):
    """Start wt-ship with its stdout and stderr going to files in the folder `logs`,
    which `proc.out()` and `proc.err()` read while it runs."""
    out, err = logs / "out", logs / "err"
    with out.open("w") as out_file, err.open("w") as err_file:
        proc = subprocess.Popen(
            [str(SCRIPT), *args],
            cwd=cwd,
            env=ship_env(session, wait),
            stdout=out_file,
            stderr=err_file,
        )
    proc.out, proc.err = out.read_text, err.read_text
    return proc


def wait_until(condition, what, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def configure(cwd):
    git(cwd, "config", "user.name", "t")
    git(cwd, "config", "user.email", "t@example.com")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A bare origin, a clone that pushed one commit with a worktree beside it, and a second clone, made
    once: each test copies them."""
    folder = tmp_path_factory.mktemp("template")
    origin = folder / "origin.git"
    git(folder, "init", "-q", "--bare", "-b", "main", str(origin))
    main = folder / "repo"
    git(folder, "clone", "-q", str(origin), str(main))
    configure(main)
    commit(main, "README", "start\n", "start")
    git(main, "push", "-q", "origin", "main")
    git(main, "worktree", "add", "-q", "--detach", str(folder / "repo-wt-topic"))
    other = folder / "other"
    git(folder, "clone", "-q", str(origin), str(other))
    configure(other)
    return folder


@pytest.fixture
def setup(template, tmp_path):
    shutil.copytree(template, tmp_path, symlinks=True, dirs_exist_ok=True)
    origin, main, wt, other = (
        tmp_path / name for name in ("origin.git", "repo", "repo-wt-topic", "other")
    )
    for clone in (main, other):
        git(clone, "remote", "set-url", "origin", str(origin))
    # The copies still name the template's paths
    git(main, "worktree", "repair", str(wt))
    return origin, main, wt, other


def remote_main(origin):
    return git(origin, "rev-parse", "main")


def test_it_pushes_the_worktrees_commits_and_prints_a_notice(setup):
    origin, _, wt, _ = setup
    old = remote_main(origin)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new
    assert f"{old[:7]}..{new[:7]}" in done.stdout
    assert "Add a" in done.stdout and "a.py" in done.stdout
    assert "behind origin/main" in done.stdout  # the main tree hasn't pulled
    assert wt.exists()


def test_it_rebases_onto_another_sessions_push_first(setup):
    origin, _, wt, other = setup
    theirs = commit(other, "b.py", "y = 2\n", "Add b")
    git(other, "push", "-q", "origin", "main")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert git(origin, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]
    assert git(origin, "rev-parse", "main~1") == theirs


def test_a_rebase_conflict_stops_with_the_rebase_in_progress(setup):
    origin, _, wt, other = setup
    commit(other, "README", "theirs\n", "Theirs")
    git(other, "push", "-q", "origin", "main")
    before = remote_main(origin)
    commit(wt, "README", "ours\n", "Ours")

    done = ship(wt)

    assert done.returncode == 1
    assert "conflicts --continue" in done.stderr, done.stderr
    assert "git rebase --continue" not in done.stderr, done.stderr
    assert remote_main(origin) == before
    assert (Path(git(wt, "rev-parse", "--absolute-git-dir")) / "rebase-merge").exists()


def test_leftover_conflict_markers_are_refused(setup):
    origin, _, wt, _ = setup
    before = remote_main(origin)
    commit(wt, "a.py", "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> abc\n", "Oops")

    done = ship(wt)

    assert done.returncode == 1 and "conflict marker" in done.stderr
    assert remote_main(origin) == before


def test_a_failing_check_stops_the_push(setup):
    origin, main, wt, _ = setup
    before = remote_main(origin)
    install_hook(main, "agent-pre-commit", "echo broken; exit 3")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt)

    assert done.returncode == 1 and "broken" in done.stdout + done.stderr
    assert remote_main(origin) == before


def test_the_default_check_is_the_repos_agent_pre_commit(setup):
    origin, main, wt, _ = setup
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    (hooks / "agent-pre-commit").write_text("#!/bin/sh\necho hook ran; exit 1\n")
    (hooks / "agent-pre-commit").chmod(0o755)
    before = remote_main(origin)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt)

    assert done.returncode == 1 and "hook ran" in done.stdout + done.stderr
    assert remote_main(origin) == before


GATE = SCRIPT.parent / "agent-commit-gate"


def pass_the_check(main, cwd):
    """Run the repo's agent check through the gate in `cwd`, as a commit's own check
    or a landing does, so the tree there goes on the record of passes."""
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    run = subprocess.run(
        [str(GATE), "--run", str(hooks / "agent-pre-commit")], cwd=cwd, check=False
    )
    assert run.returncode == 0


def test_the_recheck_gets_the_main_tip_it_rebased_onto_as_check_base(setup):
    _, main, wt, _ = setup
    seen = main.parent / "bases"
    install_hook(main, "agent-pre-commit", f'echo "${{CHECK_BASE:-none}}" >> "{seen}"')
    commit(main, "b.py", "y = 2\n", "Add b")
    # The main tree's tip passes the check, as it does when it lands.
    pass_the_check(main, main)
    tip = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", env={"PATH": path_without(GATE.name)})

    assert done.returncode == 0, done.stderr
    assert seen.read_text().split() == ["none", tip]
    assert f"given CHECK_BASE {tip[:12]}" in done.stderr


def test_a_push_that_loses_one_race_rebases_and_retries(setup):
    origin, main, wt, other = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    # Another session pushes between wt-ship's fetch and its push.
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    flag = hooks.parent / "raced"
    (hooks / "pre-push").write_text(
        f'#!/bin/sh\n[ -e "{flag}" ] && exit 0\ntouch "{flag}"\n'
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX\n"
        f'cd "{other}" && echo z >> c.py && git add c.py && git commit -qm Race'
        " && git push -q origin main\n"
    )
    (hooks / "pre-push").chmod(0o755)

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert git(origin, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Race",
        "start",
    ]
    notice = done.stdout.split("Notice for other sessions:")[1]
    assert "Add a" in notice and "Race" not in notice and "c.py" not in notice


def racing_pre_push(main, other, races):
    """A pre-push hook in which another session pushes first, `races` times."""
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    count = hooks.parent / "races"
    (hooks / "pre-push").write_text(
        f'#!/bin/sh\nn=$(cat "{count}" 2>/dev/null || echo 0)\n'
        f'[ "$n" -ge {races} ] && exit 0\necho $((n + 1)) > "{count}"\n'
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX\n"
        f'cd "{other}" && echo "$n" >> c.py && git add c.py && git commit -qm "Race $n"'
        " && git push -q origin main\n"
    )
    (hooks / "pre-push").chmod(0o755)


def test_a_push_that_keeps_losing_races_keeps_rebasing_until_it_lands(setup):
    origin, main, wt, other = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    racing_pre_push(main, other, races=3)

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert git(origin, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Race 2",
        "Race 1",
        "Race 0",
        "start",
    ]


def test_a_push_that_never_wins_stops_after_its_tries(setup):
    origin, main, wt, other = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    racing_pre_push(main, other, races=1000)

    done = ship(wt)

    assert done.returncode == 5
    assert "won the race" in done.stderr and "run `wt-ship` again" in done.stderr, (
        done.stderr
    )
    assert "Add a" not in git(origin, "log", "--format=%s", "main")


def test_uncommitted_changes_are_refused(setup):
    _, _, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    (wt / "a.py").write_text("changed\n")

    done = ship(wt)

    assert done.returncode == 1 and "uncommitted" in done.stderr


def test_nothing_to_push_says_so(setup):
    _, _, wt, _ = setup

    done = ship(wt)

    assert done.returncode == 0 and "nothing to push" in done.stdout


def test_remove_removes_the_worktree_after_the_push(setup):
    origin, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--remove")

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new and not wt.exists()
    assert str(wt) not in git(main, "worktree", "list")


def test_remove_is_refused_in_the_main_tree(setup):
    _, main, _, _ = setup
    commit(main, "a.py", "x = 1\n", "Add a")

    done = ship(main, "--remove")

    assert done.returncode == 1 and "main tree" in done.stderr
    # From the main tree, the way on is -C, not a cd into the worktree.
    assert "-C" in done.stderr, done.stderr


def test_a_push_refused_for_another_reason_isnt_retried(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    count = hooks.parent / "pushes"
    (hooks / "pre-push").write_text(f'#!/bin/sh\necho x >> "{count}"\nexit 1\n')
    (hooks / "pre-push").chmod(0o755)

    done = ship(wt)

    assert done.returncode == 1
    assert count.read_text().count("x") == 1


# --local: land on the main tree's branch, in a repo that doesn't push


def main_head(main):
    return git(main, "rev-parse", "main")


def flag(main):
    return main / "CLAUDE_WORKING"


def test_local_fast_forwards_the_main_tree_and_releases_its_claim(setup):
    origin, main, wt, _ = setup
    pushed = remote_main(origin)
    commit(main, "tracked.txt", "kept\n", "Track a file")
    # The user's own edits, to other files: tracked and modified, staged, untracked.
    (main / "tracked.txt").write_text("kept\nthe user's edit\n")
    (main / "staged.txt").write_text("staged\n")
    git(main, "add", "staged.txt")
    (main / "notes.txt").write_text("the user's own edit\n")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    new = git(wt, "rev-parse", "HEAD")
    assert main_head(main) == new
    assert (main / "a.py").read_text() == "x = 1\n"  # the work tree moved too
    assert (main / "tracked.txt").read_text() == "kept\nthe user's edit\n"
    assert "staged.txt" in git(main, "diff", "--cached", "--name-only")
    assert (main / "notes.txt").exists()
    assert not flag(main).exists()
    assert "CLAUDE_WORKING" in (main / ".git/info/exclude").read_text()
    assert remote_main(origin) == pushed  # nothing pushed
    assert "Add a" in done.stdout.split("Notice for other sessions:")[1]
    assert wt.exists()


def test_the_notice_names_directories_with_counts_not_every_file(setup):
    _, _, wt, _ = setup
    for path in ("bin", "skills/build"):
        (wt / path).mkdir(parents=True, exist_ok=True)
    commit(wt, "bin/one", "1\n", "One")
    commit(wt, "bin/two", "2\n", "Two")
    commit(wt, "skills/build/SKILL.md", "s\n", "Skill")
    commit(wt, "README.md", "r\n", "Readme")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    notice = done.stdout.split("Notice for other sessions:")[1]
    assert "Files: README.md, bin/ (2), skills/build/ (1)." in notice, notice


def test_local_rebases_onto_what_the_main_tree_got_meanwhile(setup):
    _, main, wt, _ = setup
    theirs = commit(main, "b.py", "y = 2\n", "Add b")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]
    assert git(main, "rev-parse", "main~1") == theirs


def moving_check(main, moves):
    """Install a check that lands another session's commit in the main tree on its
    first `moves` runs, and counts its runs in a file beside the main tree, which it
    returns."""
    ran = main.parent / "checks"
    install_hook(
        main,
        "agent-pre-commit",
        f'echo x >> "{ran}"; [ "$(wc -l < "{ran}")" -gt {moves} ] && exit 0; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm "Race $(wc -l < "{ran}")"',
    )
    return ran


def test_local_keeps_rebasing_while_the_main_tree_moves(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    ran = moving_check(main, moves=3)

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines()[0] == "Add a"
    assert ran.read_text().count("x") == 4
    assert not flag(main).exists()


def test_local_stops_after_its_tries_and_says_what_moved_the_branch(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    ran = moving_check(main, moves=1000)

    done = ship(wt, "--local", "--remove")

    assert done.returncode == 5, done.stderr
    assert ran.read_text().count("x") == 10
    assert "wt-ship --local --remove" in done.stderr and "Race 10" in done.stderr, (
        done.stderr
    )
    assert "Add a" not in git(main, "log", "--format=%s", "main")
    assert not flag(main).exists()


def test_local_rebases_when_the_main_tree_moves_once_during_the_check(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    ran = main.parent / "checks"
    # The first check lands another session's commit in the main tree.
    install_hook(
        main,
        "agent-pre-commit",
        f'echo x >> "{ran}"; [ "$(wc -l < "{ran}")" -gt 1 ] && exit 0; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm Race',
    )

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Race",
        "start",
    ]
    assert ran.read_text().count("x") == 2
    assert not flag(main).exists()


def test_local_leaves_a_main_tree_another_session_claimed(setup):
    _, main, wt, _ = setup
    before = main_head(main)
    flag(main).write_text("s-other 2026-11-07T01:02:03+00:00\n")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert "s-other" in done.stderr and "2026-11-07T01:02:03" in done.stderr
    assert main_head(main) == before
    assert flag(main).read_text() == "s-other 2026-11-07T01:02:03+00:00\n"


def test_local_keeps_a_claim_this_session_holds(setup):
    _, main, wt, _ = setup
    flag(main).write_text("s-mine 2026-11-07T01:02:03+00:00\n")
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert flag(main).read_text() == "s-mine 2026-11-07T01:02:03+00:00\n"


def test_local_stops_when_the_main_trees_changes_touch_the_same_files(setup):
    _, main, wt, _ = setup
    log = logging_check(main)
    before = main_head(main)
    (main / "README").write_text("start\nthe user's edit\n")
    commit(wt, "README", "ours\n", "Ours")

    done = ship(wt, "--local", "--remove")

    assert done.returncode == 4, done.stderr
    assert "ask the user" in done.stderr and "README" in done.stderr
    # It stops before the checks, so nobody waits through a run that can't land.
    assert not log.exists()
    assert main_head(main) == before
    assert (main / "README").read_text() == "start\nthe user's edit\n"
    assert not flag(main).exists()
    assert wt.exists()  # --remove only after a landing


def test_local_remove_removes_the_worktree_once_landed(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--remove")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new and not wt.exists()


def test_local_with_nothing_to_land_says_so(setup):
    _, _, wt, _ = setup

    done = ship(wt, "--local")

    assert done.returncode == 0 and "nothing to land" in done.stdout


@pytest.mark.parametrize(
    "args", [["--remote", "up"], ["--branch", "dev"]], ids=["remote", "branch"]
)
def test_local_refuses_another_target(setup, args):
    _, main, wt, _ = setup
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", *args)

    assert done.returncode == 1 and "--local" in done.stderr
    assert main_head(main) == before


def test_local_takes_branch_naming_the_main_trees_own(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--branch", "main")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_local_refuses_a_detached_main_tree(setup):
    _, main, wt, _ = setup
    git(main, "checkout", "-q", "--detach")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 1 and "detached" in done.stderr


def install_hook(main, name, body):
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    (hooks / name).write_text(f"#!/bin/sh\n{body}\n")
    (hooks / name).chmod(0o755)


def test_local_merges_while_it_holds_the_claim(setup):
    _, main, wt, _ = setup
    seen = main.parent / "flag-at-merge"
    install_hook(main, "post-merge", f'cat "{main}/CLAUDE_WORKING" > "{seen}"')
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert seen.read_text().startswith("s-mine ")
    # The holder's host and process, so a later session can tell it's gone.
    fields = dict(f.split("=", 1) for f in seen.read_text().split()[2:])
    assert fields["host"] == socket.gethostname()
    assert int(fields["pid"]) > 0 and fields["start"]


def test_local_keeps_the_worktree_when_the_branch_doesnt_hold_head_after(setup):
    _, main, wt, _ = setup
    # Something undoes the merge at once, as a misbehaving hook could.
    install_hook(
        main,
        "post-merge",
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE; git reset -q --hard HEAD~1",
    )
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--remove")

    assert done.returncode == 1 and "doesn't hold" in done.stderr
    assert wt.exists() and not flag(main).exists()


def test_local_lands_when_a_tag_shares_the_branchs_name(setup):
    _, main, wt, _ = setup
    git(main, "tag", "main", "HEAD")
    commit(main, "b.py", "y = 2\n", "Add b")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "refs/heads/main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]


def test_local_failing_check_says_nothing_was_landed(setup):
    _, main, wt, _ = setup
    before = main_head(main)
    install_hook(main, "agent-pre-commit", "exit 3")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert "nothing was landed" in done.stderr and "push" not in done.stderr
    # It names the commit, and says to run it again.
    assert "Add a" in done.stderr and "`wt-ship --local` again" in done.stderr
    assert main_head(main) == before
    assert not flag(main).exists()


def test_local_stopped_during_the_retry_releases_the_claim(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    ran, started = main.parent / "checks", main.parent / "retry-started"
    # The first check lands another commit in the main tree, forcing the retry under
    # the claim; the retry's check waits to be stopped.
    install_hook(
        main,
        "agent-pre-commit",
        f'echo x >> "{ran}"; if [ "$(wc -l < "{ran}")" -gt 1 ]; then'
        f' touch "{started}"; sleep 30; exit 0; fi; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm Race',
    )
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": "s-mine"}
    run = subprocess.Popen(
        [str(SCRIPT), "--local"],
        cwd=wt,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert flag(main).exists()
    run.send_signal(signal.SIGTERM)
    run.communicate(timeout=20)

    assert run.returncode != 0
    assert not flag(main).exists()


def test_local_is_refused_in_the_main_tree(setup):
    _, main, _, _ = setup
    commit(main, "a.py", "x = 1\n", "Add a")

    done = ship(main, "--local")

    assert done.returncode == 1 and "linked worktree" in done.stderr
    assert "-C" in done.stderr, done.stderr


def test_local_replays_only_its_own_commits_when_the_branch_was_rewritten(setup):
    _, main, wt, _ = setup
    # The worktree starts at an unpushed commit of the main tree's.
    old = commit(main, "b.py", "y = 2\n", "Add b")
    git(wt, "checkout", "-q", "--detach", old)
    commit(wt, "a.py", "x = 1\n", "Add a")
    # Another session rewrites that commit before anything is pushed.
    (main / "b.py").write_text("y = 3\n")
    git(main, "commit", "-q", "-a", "--amend", "-m", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]
    assert (main / "b.py").read_text() == "y = 3\n"


def test_local_names_its_own_landed_commit_the_branch_dropped_and_leaves_it_out(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    assert ship(wt, "--local").returncode == 0
    # The main tree drops it, then gains another commit.
    git(main, "reset", "-q", "--hard", "HEAD~1")
    commit(main, "d.py", "w = 4\n", "Add d")
    commit(wt, "c.py", "z = 3\n", "Add c")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == [
        "Add c",
        "Add d",
        "start",
    ]
    skipped = done.stdout.split("Not replaying", 1)[1].split("\n\n")[0]
    assert "Add a" in skipped and "Add c" not in skipped


def test_local_leaves_out_what_the_branch_dropped_even_with_nothing_after(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    assert ship(wt, "--local").returncode == 0
    git(main, "reset", "-q", "--hard", "HEAD~1")
    commit(wt, "c.py", "z = 3\n", "Add c")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == ["Add c", "start"]
    assert "Not replaying" in done.stdout and "Add a" in done.stdout


def test_local_folds_fixups_into_the_commits_they_fix(setup):
    _, main, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    with open(wt / "a.py", "a") as file:
        file.write("y = 2\n")
    git(wt, "commit", "-q", "-a", f"--fixup={first}")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == ["Add a", "start"]
    assert (main / "a.py").read_text() == "x = 1\ny = 2\n"


# Waiting for the main tree: holders and waiters are real processes, so their
# liveness is judged from real PIDs.


def start_time(pid):
    """A process's start time, field 22 of /proc/PID/stat, after the command name."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def holder_flag(proc, host=None):
    host = host or socket.gethostname()
    return (
        f"s-other 2026-11-07T01:02:03+00:00 host={host} pid={proc.pid}"
        f" start={start_time(proc.pid)}\n"
    )


@pytest.fixture
def holder():
    """A live process to name in a flag, killed when the test ends."""
    procs = []

    def start():
        proc = subprocess.Popen(["sleep", "60"])
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


def queue(main):
    """The queue's entries: its lock and half-written entries start with a dot."""
    folder = main / ".git" / "wt-ship-queue"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if not p.name.startswith("."))


def queued_sessions(main):
    """The sessions in the queue; an entry may go while it's being read."""
    sessions = []
    for entry in queue(main):
        try:
            sessions.append(entry.read_text().split()[0])
        except (FileNotFoundError, IndexError):
            pass
    return sessions


def finish(proc):
    proc.wait(timeout=30)
    return proc.returncode, proc.out(), proc.err()


def waits_to_land(proc):
    """Wait for wt-ship to say it waits to land. Only then is it in its wait loop, its
    queue entry in hand: it joins the queue before it first looks at the flag, so a
    flag freed in that gap meets a run that never waits, and a signal there can stop
    it before it holds the entry it would remove."""
    wait_until(lambda: "Waiting to land" in proc.out(), "wt-ship to say it waits")


def test_local_clears_a_flag_whose_process_is_gone_and_lands(setup, holder):
    _, main, wt, _ = setup
    gone = holder()
    text = holder_flag(gone)
    gone.kill()
    gone.wait()
    flag(main).write_text(text)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert not flag(main).exists()


def test_local_waits_while_another_landing_holds_the_flag_then_lands(
    setup, holder, background
):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local")
    waits_to_land(waiting)
    flag(main).unlink()  # that landing finished
    status, out, err = finish(waiting)

    assert status == 0, err
    assert main_head(main) == new
    assert "s-other" in out, out
    assert queue(main) == []
    assert not flag(main).exists()


def test_local_waits_for_a_holder_that_dies_then_lands(setup, holder, background):
    _, main, wt, _ = setup
    alive = holder()
    flag(main).write_text(holder_flag(alive))
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local")
    wait_until(lambda: queue(main), "the waiter to join the queue")
    alive.kill()
    alive.wait()
    status, _, err = finish(waiting)

    assert status == 0, err
    assert main_head(main) == new


def test_local_never_clears_a_flag_from_another_host(setup, holder):
    _, main, wt, _ = setup
    gone = holder()
    text = holder_flag(gone, host="some-other-machine")
    gone.kill()
    gone.wait()
    flag(main).write_text(text)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert flag(main).read_text() == text
    assert queue(main) == []


def test_no_wait_stops_at_once_on_a_held_flag(setup, holder):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    started = time.monotonic()
    done = ship(wt, "--local", "--no-wait", wait="30")

    assert done.returncode == 3, done.stderr
    assert time.monotonic() - started < 10
    assert queue(main) == []


def test_waiters_land_in_the_order_they_came(setup, holder, background):
    _, main, wt, _ = setup
    second_wt = main.parent / "repo-wt-second"
    git(main, "worktree", "add", "-q", "--detach", str(second_wt))
    flag(main).write_text(holder_flag(holder()))
    first = commit(wt, "a.py", "x = 1\n", "First")
    commit(second_wt, "b.py", "y = 1\n", "Second")

    one = background(wt, "--local", session="s-one")
    wait_until(lambda: len(queue(main)) == 1, "the first waiter to join")
    two = background(second_wt, "--local", session="s-two")
    wait_until(lambda: len(queue(main)) == 2, "the second waiter to join")
    flag(main).unlink()

    for proc in (one, two):
        status, _, err = finish(proc)
        assert status == 0, err
    assert git(main, "log", "--format=%s", "-2").splitlines() == ["Second", "First"]
    assert git(main, "rev-parse", "main~1") == first
    assert queue(main) == []


def test_a_killed_waiter_doesnt_hold_up_the_queue(setup, holder, background):
    _, main, wt, _ = setup
    second_wt = main.parent / "repo-wt-second"
    git(main, "worktree", "add", "-q", "--detach", str(second_wt))
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "First")
    new = commit(second_wt, "b.py", "y = 1\n", "Second")

    killed = background(wt, "--local", session="s-one")
    wait_until(lambda: len(queue(main)) == 1, "the first waiter to join")
    killed.kill()  # no chance to clean up: its entry stays behind
    killed.wait()
    assert queued_sessions(main) == ["s-one"]
    two = background(second_wt, "--local", session="s-two")
    # The second waiter drops the killed one's entry as it joins.
    wait_until(lambda: queued_sessions(main) == ["s-two"], "the dead entry to go")
    flag(main).unlink()
    status, _, err = finish(two)

    assert status == 0, err
    assert main_head(main) == new
    assert queue(main) == []


def test_a_waiter_stopped_while_waiting_leaves_the_queue(setup, holder, background):
    _, main, wt, _ = setup
    text = holder_flag(holder())
    flag(main).write_text(text)
    commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local")
    waits_to_land(waiting)
    waiting.send_signal(signal.SIGTERM)
    status, _, _ = finish(waiting)

    assert status != 0
    assert queue(main) == []
    assert flag(main).read_text() == text


@pytest.fixture
def background(tmp_path_factory):
    """Start wt-ship in the background; every run is killed and reaped at the end."""
    procs = []

    def start(cwd, *args, session="s-mine", wait="10"):
        logs = tmp_path_factory.mktemp("ship")
        proc = ship_in_background(cwd, logs, *args, session=session, wait=wait)
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def queue_entry(main, proc, session="s-ahead"):
    """A queue entry for a live process, as a waiter ahead of this one writes it."""
    folder = main / ".git" / "wt-ship-queue"
    folder.mkdir(exist_ok=True)
    entry = folder / f"{time.time_ns() - 10**9:020d}-{proc.pid}"
    entry.write_text(
        f"{session} 2026-11-07T01:00:00+00:00 host={socket.gethostname()}"
        f" pid={proc.pid} start={start_time(proc.pid)}\n"
    )
    return entry


def test_a_flag_naming_a_reused_pid_is_cleared(setup, holder):
    _, main, wt, _ = setup
    alive = holder()
    # The PID is live, but the process holding it started at another time.
    flag(main).write_text(
        f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()}"
        f" pid={alive.pid} start=1\n"
    )
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_no_wait_still_clears_a_flag_whose_process_is_gone(setup, holder):
    _, main, wt, _ = setup
    gone = holder()
    text = holder_flag(gone)
    gone.kill()
    gone.wait()
    flag(main).write_text(text)
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--no-wait")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_a_flag_missing_a_field_is_waited_on_not_misread(setup, holder):
    _, main, wt, _ = setup
    gone = holder()
    # No start=, and a field wt-ship doesn't write: it can't judge this one.
    text = f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()} pid={gone.pid} note=x\n"
    gone.kill()
    gone.wait()
    flag(main).write_text(text)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert flag(main).read_text() == text


def test_another_run_of_this_session_holding_the_flag_is_waited_on(setup, holder):
    # A subagent, or a rerun after a Bash timeout, shares the session's id.
    _, main, wt, _ = setup
    text = holder_flag(holder()).replace("s-other", "s-mine", 1)
    flag(main).write_text(text)
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert main_head(main) == before
    assert flag(main).read_text() == text


def test_no_wait_doesnt_jump_the_queue(setup, holder):
    _, main, wt, _ = setup
    entry = queue_entry(main, holder())
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--no-wait")

    assert done.returncode == 3, done.stderr
    assert main_head(main) == before
    assert str(entry) in done.stderr and "s-ahead" in done.stderr


def test_giving_up_behind_a_waiter_names_its_entry(setup, holder):
    _, main, wt, _ = setup
    entry = queue_entry(main, holder())
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert str(entry) in done.stderr and "s-ahead" in done.stderr
    assert "2026-11-07T01:00:00" in done.stderr
    assert [e.name for e in queue(main)] == [entry.name]


def test_a_waiter_stopped_with_ctrl_c_leaves_quietly(setup, holder, background):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local")
    waits_to_land(waiting)
    waiting.send_signal(signal.SIGINT)
    status, _, err = finish(waiting)

    assert status == 128 + signal.SIGINT
    assert "Traceback" not in err
    assert queue(main) == []


def test_a_queue_it_cant_use_fails_in_one_line(setup, holder):
    _, main, wt, _ = setup
    (main / ".git" / "wt-ship-queue").write_text("not a folder\n")
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 1
    assert done.stderr.count("\n") == 1, done.stderr
    assert "wt-ship-queue" in done.stderr


def test_files_in_the_queue_that_arent_entries_are_ignored(setup):
    _, main, wt, _ = setup
    folder = main / ".git" / "wt-ship-queue"
    (folder / "0000-sub").mkdir(parents=True)
    (folder / "README").write_text("")
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.3")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_the_flag_goes_only_while_it_holds_this_runs_text(setup):
    # Should another session's text replace this run's flag, leaving deletes nothing.
    _, main, wt, _ = setup
    theirs = "s-other 2026-11-07T01:02:03+00:00\n"
    install_hook(main, "post-merge", f"printf '{theirs}' > \"{main}/CLAUDE_WORKING\"")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert flag(main).read_text() == theirs


def test_waiting_says_whom_it_waits_on_once_per_change(setup, holder):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", wait="0.5")

    assert done.returncode == 3
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("Waiting")]
    assert len(lines) == 1, done.stdout
    assert "s-other" in lines[0] and "2026-11-07T01:02:03" in lines[0]


# -C: a call that starts outside the worktree keeps its shell out of the folder
# --remove deletes.


def test_c_lands_and_removes_the_worktree_from_outside_it(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(main, "-C", "../repo-wt-topic", "--local", "--remove")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert not wt.exists()


def test_c_pushes_from_outside_the_worktree(setup):
    origin, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(main.parent, "-C", str(wt))

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new


def test_c_naming_no_folder_is_a_usage_error(setup):
    _, main, _, _ = setup

    done = ship(main, "-C", "../nowhere", "--local")

    assert done.returncode == 2
    assert "../nowhere" in done.stderr
    assert done.stderr.count("\n") == 1, done.stderr


def test_c_naming_a_file_is_a_usage_error(setup):
    _, main, _, _ = setup

    done = ship(main, "-C", "README", "--local")

    assert done.returncode == 2
    assert "README" in done.stderr
    assert done.stderr.count("\n") == 1, done.stderr


def test_a_stop_says_to_run_the_same_command_again(setup):
    _, main, wt, _ = setup
    flag(main).write_text("s-other 2026-11-07T01:02:03+00:00\n")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(main, "-C", "../repo-wt-topic", "--local", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert "`wt-ship -C ../repo-wt-topic --local` again" in done.stderr


def test_the_check_waits_its_turn_in_heavys_queue(setup, holder, background):
    _, main, wt, _ = setup
    install_hook(main, "agent-pre-commit", "exit 0")
    alive = holder()
    lane = Path(os.environ["HEAVY_DIR"]) / "normal"
    lane.mkdir(parents=True)
    (lane / "slot").write_text(
        f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()}"
        f" pid={alive.pid} start={start_time(alive.pid)}\ncwd=/x\ncmd=cargo test\n"
    )
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    shipping = background(wt, "--local")
    wait_until(
        lambda: any(p.name[:1].isdigit() for p in lane.iterdir()), "the check to queue"
    )
    assert main_head(main) != new
    alive.kill()
    alive.wait()
    status, _, err = finish(shipping)

    assert status == 0, err
    assert main_head(main) == new


# Every commit it lands is checked, not only HEAD: each earlier commit is checked out in
# turn in the worktree, oldest first, unless its tree passed the check already.


def logging_check(main, body=""):
    """Install a check that appends the subject of the commit it sees to a file beside
    the main tree, which it returns, then runs `body`."""
    log = main.parent / "checked"
    install_hook(main, "agent-pre-commit", f'git log -1 --format=%s >> "{log}"\n{body}')
    return log


def test_local_checks_each_commit_it_lands_oldest_first(setup):
    _, main, wt, _ = setup
    log = logging_check(main)
    for name in "abc":
        commit(wt, f"{name}.py", "x = 1\n", f"Add {name}")
    # Main moves, so the rebase gives each commit a tree no check has seen.
    commit(main, "m.py", "m = 1\n", "Meanwhile")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert log.read_text().splitlines() == ["Add a", "Add b", "Add c"]
    assert git(main, "log", "--format=%s", "main").splitlines()[0] == "Add c"


def test_a_push_checks_each_commit_it_pushes(setup):
    origin, main, wt, _ = setup
    log = logging_check(main)
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert log.read_text().splitlines() == ["Add a", "Add b"]
    assert git(origin, "log", "--format=%s", "main").splitlines()[0] == "Add b"


def fails_at(subject):
    return f'test "$(git log -1 --format=%s)" != "{subject}" || exit 3'


def test_a_failing_earlier_commit_stops_the_landing_and_names_it(setup):
    _, main, wt, _ = setup
    install_hook(main, "agent-pre-commit", fails_at("Add a"))
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    tip = commit(wt, "b.py", "y = 2\n", "Add b")
    before = main_head(main)

    done = ship(wt, "--local")

    assert done.returncode == 1, done.stderr
    assert f"{first[:12]} Add a" in done.stderr, done.stderr
    assert f"--fixup={first[:12]}" in done.stderr
    assert "nothing was landed" in done.stderr
    assert main_head(main) == before
    # The worktree is back at its tip, with nothing changed.
    assert git(wt, "rev-parse", "HEAD") == tip
    assert git(wt, "status", "--porcelain") == ""


@pytest.mark.parametrize("upstream_passed", [True, False])
def test_a_failure_says_when_upstreams_tip_has_no_pass_either(setup, upstream_passed):
    _, main, wt, _ = setup
    install_hook(main, "agent-pre-commit", fails_at("Add a"))
    if upstream_passed:
        pass_the_check(main, main)
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 1, done.stderr
    assert ("no pass" in done.stderr) is not upstream_passed, done.stderr


@pytest.fixture
def blocking_check():
    """Install a check that, the first time it sees the commit with a subject, writes
    its PID to a file `started` beside the main tree and waits for a file `go` there:
    `blocking_check(main, subject)` returns both paths. Only the first time, so another
    check of that commit, such as a commit's own, can't wait behind it. Every `go` is
    touched when the test ends, passed or failed, so no check outlives it."""
    gos = []

    def install(main, subject):
        started, go = main.parent / "started", main.parent / "go"
        once = main.parent / "blocked-once"
        install_hook(
            main,
            "agent-pre-commit",
            f'if [ "$(git log -1 --format=%s)" = "{subject}" ] &&'
            f' mkdir "{once}" 2>/dev/null; then echo $$ > "{started}.part";'
            f' mv "{started}.part" "{started}";'
            f' while [ ! -e "{go}" ]; do sleep 0.05; done; fi',
        )
        gos.append(go)
        return started, go

    yield install
    for go in gos:
        go.touch()


@pytest.mark.parametrize("branch", [None, "topic"])
def test_a_stop_during_an_earlier_commits_check_puts_the_tip_back(
    setup, background, blocking_check, branch
):
    _, main, wt, _ = setup
    started, go = blocking_check(main, "Add a")
    if branch:
        git(wt, "checkout", "-q", "-b", branch)
    commit(wt, "a.py", "x = 1\n", "Add a")
    tip = commit(wt, "b.py", "y = 2\n", "Add b")

    shipping = background(wt, "--local")
    wait_until(started.exists, "the earlier commit's check to start")
    shipping.send_signal(signal.SIGTERM)
    status, _, _ = finish(shipping)
    check = int(started.read_text())

    assert status != 0
    # The stop reached the check too, not only wt-ship.
    wait_until(lambda: not alive(check), "the stopped check to end")
    assert git(wt, "rev-parse", "HEAD") == tip
    assert git(wt, "status", "--porcelain") == ""
    if branch:
        assert git(wt, "symbolic-ref", "HEAD") == f"refs/heads/{branch}"
    go.touch()
    # Nothing it left stops the next run.
    done = ship(wt, "--local")
    assert done.returncode == 0, done.stderr
    assert main_head(main) == tip


def gate_commit(repo):
    """Commit as an agent does, through the gate's line in the pre-commit hook, with
    this checkout's gate first on PATH. The commit is empty: staging a file would
    change the index under the check wt-ship is running, which the gate refuses. A
    commit the gate lets through waits for heavy's lane behind that check, so the
    timeout fails it rather than hang, and stops the gate with git: they run in a
    process group of their own."""
    env = os.environ | {
        "AI_AGENT": "test-agent",
        "PATH": os.pathsep.join([str(GATE.parent), os.environ["PATH"]]),
    }
    proc = subprocess.Popen(
        ["git", "commit", "-q", "--allow-empty", "-m", "Meanwhile"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_a_commit_while_an_earlier_commit_is_checked_is_refused(
    setup, background, blocking_check
):
    _, main, wt, _ = setup
    started, go = blocking_check(main, "Add a")
    installed = subprocess.run(
        [str(GATE), "--install", str(main)], capture_output=True, check=False
    )
    assert installed.returncode == 0, installed.stderr
    commit(wt, "a.py", "x = 1\n", "Add a")
    tip = commit(wt, "b.py", "y = 2\n", "Add b")

    shipping = background(wt, "--local")
    wait_until(started.exists, "the earlier commit's check to start")
    refused = gate_commit(wt)
    go.touch()
    status, _, err = finish(shipping)

    assert refused.returncode != 0
    assert "COMMIT REFUSED" in refused.stderr and "wt-ship" in refused.stderr
    assert status == 0, err
    assert main_head(main) == tip


def test_a_run_killed_mid_check_leaves_the_next_one_naming_the_tip(
    setup, background, blocking_check
):
    _, main, wt, _ = setup
    started, go = blocking_check(main, "Add a")
    commit(wt, "a.py", "x = 1\n", "Add a")
    tip = commit(wt, "b.py", "y = 2\n", "Add b")
    before = main_head(main)
    shipping = background(wt, "--local")
    wait_until(started.exists, "the earlier commit's check to start")
    shipping.kill()  # SIGKILL: no chance to put the tip back
    shipping.wait()
    go.touch()

    done = ship(wt, "--local")

    assert done.returncode == 1, done.stderr
    assert tip[:12] in done.stderr and "git checkout --force" in done.stderr
    assert main_head(main) == before


def keep_the_check_in_the_tree(main):
    """Keep the check in the main tree's branch: core.hooksPath .githooks, which
    holds an executable agent-pre-commit that passes."""
    git(main, "config", "core.hooksPath", ".githooks")
    (main / ".githooks").mkdir()
    (main / ".githooks" / "agent-pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (main / ".githooks" / "agent-pre-commit").chmod(0o755)
    git(main, "add", ".githooks")
    git(main, "commit", "-qm", "Add the check")


@pytest.mark.parametrize("where", [".", "sub"])
def test_a_commit_that_drops_the_check_kept_in_the_tree_fails(setup, where):
    _, main, wt, _ = setup
    keep_the_check_in_the_tree(main)
    git(wt, "checkout", "-q", "--detach", "main")
    git(wt, "rm", "-q", ".githooks/agent-pre-commit")
    git(wt, "commit", "-qm", "Drop the check")
    (wt / "sub").mkdir()
    commit(wt, "sub/a.py", "x = 1\n", "Add a")
    before = main_head(main)

    # Run from a subdirectory too, where git reads paths relative to it.
    done = ship(wt / where, "--local")

    assert done.returncode == 1, done.stderr
    assert "Drop the check" in done.stderr, done.stderr
    assert main_head(main) == before


def keep_the_check_behind_a_link(main):
    """Keep the check in the main tree's branch as filedrawer does: core.hooksPath
    .githooks, where agent-pre-commit is a link to ../bin/agent-pre-commit."""
    git(main, "config", "core.hooksPath", ".githooks")
    (main / "bin").mkdir()
    (main / "bin" / "agent-pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (main / "bin" / "agent-pre-commit").chmod(0o755)
    (main / ".githooks").mkdir()
    (main / ".githooks" / "agent-pre-commit").symlink_to("../bin/agent-pre-commit")
    git(main, "add", "bin", ".githooks")
    git(main, "commit", "-qm", "Add the check")


def delete_the_target(wt):
    git(wt, "rm", "-q", "bin/agent-pre-commit")


def unexecute_the_target(wt):
    (wt / "bin" / "agent-pre-commit").chmod(0o644)
    git(wt, "add", "bin/agent-pre-commit")


@pytest.mark.parametrize("breaking", [delete_the_target, unexecute_the_target])
def test_a_commit_that_breaks_the_link_to_the_check_fails(setup, breaking):
    _, main, wt, _ = setup
    keep_the_check_behind_a_link(main)
    git(wt, "checkout", "-q", "--detach", "main")
    breaking(wt)
    git(wt, "commit", "-qm", "Break the check")
    commit(wt, "a.py", "x = 1\n", "Add a")
    before = main_head(main)

    done = ship(wt, "--local")

    assert done.returncode == 1, done.stderr
    assert "Break the check" in done.stderr, done.stderr
    assert main_head(main) == before


def test_a_commit_that_keeps_the_link_to_the_check_lands_checked(setup):
    _, main, wt, _ = setup
    keep_the_check_behind_a_link(main)
    git(wt, "checkout", "-q", "--detach", "main")
    tip = commit(wt, "bin/agent-pre-commit", "exit 0\n", "Change the check")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "Checking with agent-pre-commit the 1 commit" in done.stdout, done.stdout
    assert main_head(main) == tip


def test_a_later_link_to_the_hooks_folder_doesnt_hide_an_earlier_drop(setup):
    _, main, wt, _ = setup
    keep_the_check_in_the_tree(main)
    git(wt, "checkout", "-q", "--detach", "main")
    git(wt, "rm", "-q", ".githooks/agent-pre-commit")
    git(wt, "commit", "-qm", "Drop the check")
    # The tip keeps a copy of the check, through a link at the hooks folder.
    (wt / "elsewhere").mkdir()
    (wt / "elsewhere" / "agent-pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (wt / "elsewhere" / "agent-pre-commit").chmod(0o755)
    (wt / ".githooks").symlink_to("elsewhere")
    git(wt, "add", "elsewhere", ".githooks")
    git(wt, "commit", "-qm", "Move the hooks elsewhere")
    before = main_head(main)

    done = ship(wt, "--local")

    assert done.returncode == 1, done.stderr
    assert "Drop the check" in done.stderr, done.stderr
    assert main_head(main) == before


def test_a_commit_that_moves_the_check_behind_a_link_lands_checked(setup):
    _, main, wt, _ = setup
    keep_the_check_in_the_tree(main)
    git(wt, "checkout", "-q", "--detach", "main")
    (wt / "bin").mkdir()
    git(wt, "mv", ".githooks/agent-pre-commit", "bin/agent-pre-commit")
    (wt / ".githooks" / "agent-pre-commit").symlink_to("../bin/agent-pre-commit")
    git(wt, "add", ".githooks")
    git(wt, "commit", "-qm", "Move the check behind a link")
    tip = git(wt, "rev-parse", "HEAD")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "Checking with agent-pre-commit the 1 commit" in done.stdout, done.stdout
    assert main_head(main) == tip


def test_commits_that_passed_are_not_checked_out_again(setup):
    _, main, wt, _ = setup
    log = logging_check(main)
    for name in "ab":
        commit(wt, f"{name}.py", "x = 1\n", f"Add {name}")
        pass_the_check(main, wt)  # as the commit's own check does
    log.unlink()

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "Checking with" not in done.stdout
    assert not log.exists()


def test_a_second_run_waits_for_one_checking_the_same_worktree(
    setup, background, blocking_check
):
    _, main, wt, _ = setup
    started, go = blocking_check(main, "Add a")
    commit(wt, "a.py", "x = 1\n", "Add a")
    tip = commit(wt, "b.py", "y = 2\n", "Add b")
    shipping = background(wt, "--local")
    wait_until(started.exists, "the earlier commit's check to start")

    second = ship(wt, "--local")
    go.touch()
    status, _, err = finish(shipping)

    assert second.returncode == 1
    assert "Another wt-ship" in second.stderr, second.stderr
    assert status == 0, err
    assert main_head(main) == tip


def test_each_commits_check_gets_the_commit_before_as_check_base(setup):
    _, main, wt, _ = setup
    bases = main.parent / "bases"
    install_hook(main, "agent-pre-commit", f'echo "${{CHECK_BASE:-none}}" >> "{bases}"')
    # The main tree's tip passed the check, as it does when it lands.
    pass_the_check(main, main)
    start = main_head(main)
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert bases.read_text().split() == ["none", start, first]


def test_every_check_runs_at_the_top_of_the_worktree(setup):
    _, main, wt, _ = setup
    seen = main.parent / "seen"
    install_hook(
        main,
        "agent-pre-commit",
        f'echo "$(pwd -P) $(git log -1 --format=%s)" >> "{seen}"',
    )
    (wt / "sub").mkdir()
    commit(wt, "sub/a.py", "x = 1\n", "Add a")
    commit(wt, "sub/b.py", "y = 2\n", "Add b")

    done = ship(wt / "sub", "--local")

    assert done.returncode == 0, done.stderr
    top = wt.resolve()
    assert seen.read_text().splitlines() == [f"{top} Add a", f"{top} Add b"]


def test_each_check_sees_its_commits_files_whatever_the_last_check_left(setup):
    _, main, wt, _ = setup
    seen = main.parent / "seen"
    # Each check reads the work tree, then leaves it dirty.
    install_hook(main, "agent-pre-commit", f'cat state >> "{seen}"; echo junk >> state')
    for name in "abc":
        (wt / "state").write_text(f"{name}\n")
        git(wt, "add", "state")
        git(wt, "commit", "-qm", f"State {name}")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert seen.read_text().splitlines() == ["a", "b", "c"]
    assert "the work tree holds that commit" in done.stdout


def test_an_earlier_commit_without_the_check_kept_in_the_tree_has_none_to_pass(setup):
    _, main, wt, _ = setup
    git(main, "config", "core.hooksPath", ".githooks")
    commit(wt, "a.py", "x = 1\n", "Add a")
    # The branch adds the check, which the main tree's branch doesn't have.
    (wt / ".githooks").mkdir()
    (wt / ".githooks" / "agent-pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (wt / ".githooks" / "agent-pre-commit").chmod(0o755)
    git(wt, "add", ".githooks")
    git(wt, "commit", "-qm", "Add the check")
    tip = git(wt, "rev-parse", "HEAD")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "nothing to check" in done.stdout
    assert main_head(main) == git(wt, "rev-parse", "HEAD") == tip


def alive(pid):
    """Whether a process runs under `pid`, a zombie counting as ended."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


def test_an_inherited_commit_mark_doesnt_spare_a_landing_the_full_check(setup):
    _, main, wt, _ = setup
    install_hook(main, "agent-pre-commit", "exit 3")
    install_hook(main, "agent-commit-check", "exit 0")
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")
    before = main_head(main)

    done = ship(wt, "--local", env={"AGENT_CHECK_AT_COMMIT": "1"})

    assert done.returncode == 1, done.stderr
    assert main_head(main) == before


# --local checks only while it holds the main tree, so each commit is checked once per
# landing, however long it waited.


def test_local_runs_every_check_while_it_holds_the_main_tree(setup):
    _, main, wt, _ = setup
    seen = main.parent / "seen"
    install_hook(
        main,
        "agent-pre-commit",
        # This run's session, s-mine, holds the main tree's flag.
        f'if grep -q "^s-mine " "{main}/CLAUDE_WORKING" 2>/dev/null; then echo held;'
        f' else echo free; fi >> "{seen}"',
    )
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert seen.read_text().split() == ["held", "held"]


def test_a_landing_that_waited_while_main_moved_checks_each_commit_once(
    setup, holder, background
):
    _, main, wt, _ = setup
    log = logging_check(main)
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")
    alive = holder()
    flag(main).write_text(holder_flag(alive))
    shipping = background(wt, "--local")
    waits_to_land(shipping)
    commit(main, "m.py", "m = 1\n", "Meanwhile")  # the holder's landing
    alive.kill()
    alive.wait()

    status, _, err = finish(shipping)

    assert status == 0, err
    assert log.read_text().splitlines() == ["Add a", "Add b"]
    assert git(main, "log", "--format=%s", "main").splitlines()[:3] == [
        "Add b",
        "Add a",
        "Meanwhile",
    ]


def test_a_waiter_keeps_waiting_while_the_holder_changes(setup, holder, background):
    _, main, wt, _ = setup
    first, second, third = holder(), holder(), holder()
    flag(main).write_text(holder_flag(first))
    tip = commit(wt, "a.py", "x = 1\n", "Add a")
    shipping = background(wt, "--local", wait="8")
    waits_to_land(shipping)
    # Each handover comes 3 s after the last, 5 s before the wait would end, and the
    # last holder leaves at 10 s: past the first wait's 8 s, 4 s before the last
    # restarted one ends, so only a wait that restarts lands, with room to spare.
    # fixed sleep: the first handover's moment
    time.sleep(3)
    flag(main).write_text(holder_flag(second))
    # fixed sleep: the second handover's moment
    time.sleep(3)
    flag(main).write_text(holder_flag(third))
    # fixed sleep: the last holder keeps the flag past the first wait's deadline
    time.sleep(4)
    third.kill()
    third.wait()

    status, _, err = finish(shipping)

    assert status == 0, err
    assert main_head(main) == tip


# A landing's checks that need heavy's queue run in one job at its front, so no other
# session's job comes between them; checks that need no queue run outside it.


def hold_the_lane(holder):
    """Put a live process in heavy's normal lane slot; return the lane folder."""
    alive = holder()
    lane = Path(os.environ["HEAVY_DIR"]) / "normal"
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "slot").write_text(
        f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()}"
        f" pid={alive.pid} start={start_time(alive.pid)}\ncwd=/x\ncmd=cargo test\n"
    )
    return alive, lane


def queue_asking_check(main, log, asks=None):
    """Install a check that asks for heavy's queue, noting `ask` in `asks` each time,
    and in the queue notes its job's slot in `log`."""
    asks = asks or main.parent / "asks"
    install_hook(
        main,
        "agent-pre-commit",
        f'if [ -n "${{CHECK_QUEUE_EXIT:-}}" ]; then echo ask >> "{asks}";'
        ' exit "$CHECK_QUEUE_EXIT"; fi\n'
        f'echo "$HEAVY_SLOT" >> "{log}"',
    )


def test_a_landing_whose_checks_need_no_queue_lands_while_the_lane_is_held(
    setup, holder
):
    _, main, wt, _ = setup
    hold_the_lane(holder)
    # It names CHECK_QUEUE_EXIT and passes outside the queue, as a docs-only change's.
    install_hook(main, "agent-pre-commit", ': "${CHECK_QUEUE_EXIT:-}"; exit 0')
    commit(wt, "a.md", "a\n", "Add a")
    tip = commit(wt, "b.md", "b\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == tip


def test_a_landings_checks_that_need_the_queue_run_in_one_heavy_job(setup):
    _, main, wt, _ = setup
    slots = main.parent / "slots"
    queue_asking_check(main, slots)
    for name in "abc":
        commit(wt, f"{name}.py", "x = 1\n", f"Add {name}")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    seen = slots.read_text().splitlines()
    assert len(seen) == 3 and len(set(seen)) == 1, seen
    # Each asked for the queue once: inside the job the gate runs it straight away.
    assert (main.parent / "asks").read_text().split() == ["ask"] * 3


def test_a_landings_job_joins_heavys_queue_ahead_of_ordinary_waiters(
    setup, holder, background
):
    _, main, wt, _ = setup
    alive, lane = hold_the_lane(holder)
    waiter = holder()
    (lane / f"{time.time_ns():020d}-{waiter.pid}").write_text(
        f"s-waiting 2026-11-07T01:02:04+00:00 host={socket.gethostname()}"
        f" pid={waiter.pid} start={start_time(waiter.pid)}\ncwd=/y\ncmd=cargo build\n"
    )
    queue_asking_check(main, main.parent / "slots")
    tip = commit(wt, "a.py", "x = 1\n", "Add a")

    shipping = background(wt, "--local")

    def entries():
        return sorted(p.name for p in lane.iterdir() if p.name[:1].isdigit())

    wait_until(lambda: len(entries()) == 2, "the landing to queue")
    assert entries()[0].endswith(f"-{shipping.pid}"), entries()
    alive.kill()
    alive.wait()
    status, _, err = finish(shipping)
    assert status == 0, err
    assert main_head(main) == tip


def test_the_oldest_failing_commit_is_named_whichever_round_finds_it(setup):
    _, main, wt, _ = setup
    # a needs the queue and fails in it; b fails outside it, in the first round.
    install_hook(
        main,
        "agent-pre-commit",
        "subject=$(git log -1 --format=%s)\n"
        'if [ -n "${CHECK_QUEUE_EXIT:-}" ]; then\n'
        '  [ "$subject" = "Add a" ] && exit "$CHECK_QUEUE_EXIT"\n'
        '  [ "$subject" = "Add b" ] && exit 3\n'
        "  exit 0\n"
        "fi\n"
        '[ "$subject" != "Add a" ]',
    )
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 2\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert f"{first[:12]} Add a" in done.stderr, done.stderr


def test_local_stops_before_its_checks_when_an_untracked_file_is_in_the_way(setup):
    _, main, wt, _ = setup
    log = logging_check(main)
    before = main_head(main)
    (main / "new.py").write_text("the user's draft\n")  # untracked in the main tree
    commit(wt, "new.py", "x = 1\n", "Add new")

    done = ship(wt, "--local")

    assert done.returncode == 4, done.stderr
    assert "new.py" in done.stderr and "ask the user" in done.stderr
    assert not log.exists()
    assert main_head(main) == before
    assert (main / "new.py").read_text() == "the user's draft\n"


def staged_only_edit(main):
    """A staged edit to README whose work tree is back as HEAD has it."""
    (main / "README").write_text("start\nstaged\n")
    git(main, "add", "README")
    (main / "README").write_text("start\n")


def staged_rename_away(main):
    git(main, "mv", "README", "README.old")


@pytest.mark.parametrize("main_change", [staged_only_edit, staged_rename_away])
def test_local_sees_a_staged_change_to_a_file_it_lands_before_its_checks(
    setup, main_change
):
    _, main, wt, _ = setup
    log = logging_check(main)
    main_change(main)
    commit(wt, "README", "ours\n", "Ours")

    done = ship(wt, "--local")

    assert done.returncode == 4, done.stderr
    assert "README" in done.stderr
    assert not log.exists()


def test_local_sees_the_old_path_of_a_file_the_landing_renames(setup):
    _, main, wt, _ = setup
    log = logging_check(main)
    (main / "README").write_text("start\nthe user's edit\n")
    git(wt, "mv", "README", "README.md")
    git(wt, "commit", "-qm", "Rename README")

    done = ship(wt, "--local")

    assert done.returncode == 4, done.stderr
    assert not log.exists()


def cite(wt, path, cited, length=7):
    """Commit in `wt` with a message naming commit `cited` by its first `length` hex
    digits, as an agent naming the commit it follows up does."""
    return commit(
        wt,
        path,
        "y = 2\n",
        f"Follow up on {cited[:length]}\n\nAs {cited[:length]}'s test asked.",
    )


@pytest.mark.parametrize("length", [7, 12, 40])
@pytest.mark.parametrize("args", [(), ("--local",)])
def test_a_message_citing_an_unlanded_commit_is_refused_before_any_check(
    setup, args, length
):
    origin, main, wt, _ = setup
    ran = main.parent / "ran"
    install_hook(main, "agent-pre-commit", f'echo check >> "{ran}"')
    before = remote_main(origin), main_head(main)
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    citing = cite(wt, "b.py", first, length)

    done = ship(wt, *args)

    assert done.returncode == 1
    [line] = [ln for ln in done.stderr.splitlines() if first[:length] in ln]
    assert citing[:12] in line and f"build-state reword {citing[:12]}" in line
    assert not ran.exists()  # refused before any check ran
    assert (remote_main(origin), main_head(main)) == before


def test_a_rebase_that_rewrites_the_cited_commit_is_refused_too(setup):
    _, main, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    cite(wt, "b.py", first)
    commit(main, "c.py", "z = 3\n", "Theirs")  # so the landing rebases, rewriting both

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert first[:7] in done.stderr and "build-state reword" in done.stderr
    assert main_head(main) != git(wt, "rev-parse", "HEAD")


def test_hex_words_that_name_no_unlanded_commit_pass(setup):
    _, main, wt, _ = setup
    landed = main_head(main)
    tree = git(main, "rev-parse", "HEAD^{tree}")
    commit(
        wt,
        "a.py",
        "x = 1\n",
        f"Add a\n\nAfter {landed[:7]} and {landed}, which main holds; not deadbeef1,"
        f" a decade added, cafe, nor the tree {tree[:7]}.",
    )

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr


def picked_from_a_side_branch(wt):
    """A commit on branch `side`, which the branch it lands on doesn't hold, cherry-picked
    into `wt` with -x; its ID."""
    source = commit(wt, "side.py", "s = 1\n", "Fix on the side")
    git(wt, "branch", "side")
    git(wt, "reset", "-q", "--hard", "HEAD~1")
    git(wt, "cherry-pick", "-x", "side")
    return source


def test_the_line_cherry_pick_x_adds_doesnt_count_as_naming_a_commit(setup):
    _, main, wt, _ = setup
    source = picked_from_a_side_branch(wt)
    assert f"(cherry picked from commit {source})" in git(
        wt, "log", "-1", "--format=%B"
    )

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == git(wt, "rev-parse", "HEAD")


def test_a_commit_picked_twice_carries_both_lines_and_lands(setup):
    _, _, wt, _ = setup
    first = picked_from_a_side_branch(wt)
    # Picked again from a branch that had picked it: git adds a line for each pick.
    second = commit(wt, "other.py", "o = 1\n", "Another fix on the side")
    git(wt, "branch", "side2")
    git(wt, "reset", "-q", "--hard", "HEAD~1")
    message = (
        "Fix on the side\n\n"
        f"(cherry picked from commit {first})\n(cherry picked from commit {second})"
    )
    git(wt, "commit", "-q", "--amend", "-m", message)

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "message",
    [
        "Fix on the side\n\nBackport of {short}.\n\n(cherry picked from commit {id})",
        "Fix on the side\n\n  (cherry picked from commit {id})",
        "Fix on the side\n\n(cherry picked from commit {id}) and more",
    ],
)
def test_the_picked_id_on_another_line_or_one_unlike_gits_still_counts(setup, message):
    _, _, wt, _ = setup
    source = picked_from_a_side_branch(wt)
    git(
        wt, "commit", "-q", "--amend", "-m", message.format(short=source[:7], id=source)
    )

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert source[:7] in done.stderr and "build-state reword" in done.stderr
    assert "git tag" not in done.stderr  # a side branch holds the picked commit


def made_and_dropped(wt):
    """A commit made in `wt` and reset away, so that no ref holds it; its ID."""
    source = commit(wt, "side.py", "s = 1\n", "Fix on the side")
    git(wt, "reset", "-q", "--hard", "HEAD~1")
    return source


def test_a_pick_whose_source_no_ref_holds_is_refused(setup):
    _, _, wt, _ = setup
    source = made_and_dropped(wt)
    git(wt, "cherry-pick", "-x", source)

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert source[:7] in done.stderr and "build-state reword" in done.stderr
    # The other way out, for a source that should last.
    assert "(cherry picked from commit ID)" in done.stderr and "git tag" in done.stderr


@pytest.mark.parametrize("holder", ["branch", "its-remote-twin", "origin-HEAD-to-it"])
def test_a_pick_whose_source_only_a_checked_out_branch_holds_is_refused(
    setup, tmp_path, holder
):
    _, main, wt, _ = setup
    source = made_and_dropped(wt)
    # Another session works on branch theirs, and its landing rebases it.
    start = source if holder == "branch" else "main"
    git(main, "worktree", "add", "-q", "-b", "theirs", str(tmp_path / "theirs"), start)
    if holder != "branch":
        git(main, "update-ref", "refs/remotes/origin/theirs", source)  # pushed for CI
    if holder == "origin-HEAD-to-it":
        git(
            main,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/theirs",
        )
    git(wt, "cherry-pick", "-x", source)

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert source[:7] in done.stderr
    assert "git tag" not in done.stderr  # a tag would keep an old copy, not this one


@pytest.mark.parametrize("ref", ["refs/tags/fix-v1", "refs/remotes/origin/release"])
def test_a_pick_whose_source_a_tag_or_a_remote_branch_holds_lands(setup, ref):
    _, main, wt, _ = setup
    source = made_and_dropped(wt)
    git(wt, "update-ref", ref, source)
    git(wt, "cherry-pick", "-x", source)

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == git(wt, "rev-parse", "HEAD")


def test_a_pick_line_naming_a_commit_of_the_landing_counts_though_a_ref_holds_it(
    setup,
):
    _, _, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    git(wt, "tag", "backup")
    commit(wt, "b.py", "y = 1\n", f"Add b\n\n(cherry picked from commit {first})")

    done = ship(wt, "--local")

    assert done.returncode == 1
    assert first[:7] in done.stderr
    assert "git tag" not in done.stderr  # only a reword helps


def test_a_pick_line_naming_the_landings_commit_before_its_rebase_counts(setup):
    _, main, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 1\n", f"Add b\n\n(cherry picked from commit {first})")
    git(wt, "update-ref", "refs/remotes/origin/ci-topic", "HEAD")  # pushed for CI
    commit(main, "m.py", "m = 1\n", "Another session's landing")

    # The rebase onto main copies `first`; the CI ref keeps the old one.
    done = ship(wt, "--local")

    assert done.returncode == 1
    assert first[:7] in done.stderr


BUILD_STATE = SCRIPT.parent / "build-state"


def test_the_reword_commands_it_lists_work_in_the_order_listed(setup):
    _, _, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    second = cite(wt, "b.py", first)
    third = commit(wt, "c.py", "z = 3\n", f"Fix {first[:7]} and {second[:7]}")

    done = ship(wt, "--local")

    assert done.returncode == 1
    fixes = [ln for ln in done.stderr.splitlines() if ln.startswith("  ")]
    assert [ln.split()[0] for ln in fixes] == [third[:12], second[:12]]
    assert first[:7] in fixes[0] and second[:7] in fixes[0]
    for line in fixes:
        sha = line.rsplit(maxsplit=1)[-1]
        reword = subprocess.run(
            [str(BUILD_STATE), "reword", sha],
            input="Reworded\n",
            cwd=wt,
            capture_output=True,
            text=True,
            check=False,
        )
        assert reword.returncode == 0, reword.stderr


@pytest.mark.parametrize(
    "words", ["{id6} only", "x{id7} and {id7}_ and {id7}x"], ids=["six", "not alone"]
)
def test_a_cited_id_counts_only_from_7_digits_and_standing_alone(setup, words):
    _, _, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    message = words.format(id6=first[:6], id7=first[:7])
    commit(wt, "b.py", "y = 2\n", f"Follow up\n\n{message}")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr


def fake_issue_todo(folder, unticked):
    """A folder holding an `issue-todo` whose `unticked N...` prints `unticked[N]`, each
    item as `#N TEXT`, and fails for a number it lacks; it logs each call to calls.log."""
    folder.mkdir(exist_ok=True)
    script = folder / "issue-todo"
    items = {
        n: "".join(f"#{n} {text}\n" for text in texts) for n, texts in unticked.items()
    }
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(folder / 'calls.log')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"items = {items!r}\n"
        "args = sys.argv[2:]\n"
        "if args[0] == '--repo':\n"
        "    args = args[2:]\n"
        "for n in args:\n"
        "    if int(n) not in items:\n"
        "        sys.exit('gh: Could not resolve to an issue')\n"
        "    sys.stdout.write(items[int(n)])\n"
    )
    script.chmod(0o755)
    return {"PATH": f"{folder}:{os.environ['PATH']}"}


def test_a_landing_that_closes_an_issue_lists_its_unticked_items(setup, tmp_path):
    _, main, wt, _ = setup
    env = fake_issue_todo(tmp_path / "fakes", {46: ["The stamp", "The page"], 43: []})
    commit(wt, "a.py", "x = 1\n", "Add a\n\nCloses #46")
    commit(wt, "b.py", "y = 2\n", "Add b\n\nfixes: #43, and owner/other#9")

    done = ship(wt, "--local", env=env)

    assert done.returncode == 0, done.stderr
    closer = git(main, "log", "--format=%h", "-1", "--grep=Closes #46")
    assert (
        f"#46, which {closer} closes, has 2 unticked items:\n"
        "  [ ] The stamp\n"
        "  [ ] The page\n"
        f"Tick each that this landing did, with `issue-todo tick 46 MATCH --note {closer}`,"
        " and leave the rest.\n"
    ) in done.stdout
    assert "#43" not in done.stdout.split("Notice for other sessions:")[0]
    assert (tmp_path / "fakes/calls.log").read_text() == "unticked 46\nunticked 43\n"


def test_a_landing_that_closes_nothing_asks_nothing(setup, tmp_path):
    _, _, wt, _ = setup
    env = fake_issue_todo(tmp_path / "fakes", {})
    commit(wt, "a.py", "x = 1\n", "Add a, as in #46")

    done = ship(wt, "--local", env=env)

    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "fakes/calls.log").exists()


def test_a_failed_lookup_says_so_and_still_lands(setup, tmp_path):
    _, main, wt, _ = setup
    env = fake_issue_todo(tmp_path / "fakes", {})
    commit(wt, "a.py", "x = 1\n", "Add a\n\nResolves #99")

    done = ship(wt, "--local", env=env)

    assert done.returncode == 0, done.stderr
    assert git(main, "rev-parse", "HEAD") == git(wt, "rev-parse", "HEAD")
    assert (
        "Couldn't check #99's task-list items: gh: Could not resolve to an issue"
        in done.stdout
    )


def test_the_lookup_names_the_github_repo_of_the_remote(setup, tmp_path):
    _, main, wt, _ = setup
    env = fake_issue_todo(tmp_path / "fakes", {46: []})
    git(main, "remote", "set-url", "origin", "git@github.com:acme/widgets.git")
    commit(wt, "a.py", "x = 1\n", "Add a\n\nCloses #46")

    done = ship(wt, "--local", env=env)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "fakes/calls.log").read_text() == (
        "unticked --repo acme/widgets 46\n"
    )


def test_no_issue_todo_on_path_says_so_and_still_lands(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a\n\nCloses #5")

    done = ship(wt, "--local", env={"PATH": path_without("issue-todo")})

    assert done.returncode == 0, done.stderr
    assert git(main, "rev-parse", "HEAD") == git(wt, "rev-parse", "HEAD")
    assert "Couldn't check #5's task-list items: no issue-todo on PATH" in done.stdout


def test_a_slow_lookup_is_stopped_with_its_children_within_one_deadline(
    setup, tmp_path
):
    _, _, wt, _ = setup
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    pid_file = fakes / "child.pid"
    script = fakes / "issue-todo"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        f"open({str(fakes / 'calls.log')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    commit(wt, "a.py", "x = 1\n", "Add a\n\nCloses #1, closes #2")
    env = {
        "PATH": f"{fakes}:{os.environ['PATH']}",
        "WT_SHIP_UNTICKED_TIMEOUT": "1",
    }

    done = ship(wt, "--local", env=env)

    assert done.returncode == 0, done.stderr
    assert "Couldn't check #1's task-list items: no answer within 1 s" in done.stdout
    assert "Couldn't check #2's task-list items: no answer within 1 s" in done.stdout
    assert (fakes / "calls.log").read_text() == "unticked 1\n"  # #2 had no time left
    child = int(pid_file.read_text())
    wait_until(
        lambda: not Path(f"/proc/{child}").exists(), "the lookup's child to end", 5
    )


def test_a_push_lists_unticked_items_too(setup, tmp_path):
    _, _, wt, _ = setup
    env = fake_issue_todo(tmp_path / "fakes", {5: ["Left"]})
    commit(wt, "a.py", "x = 1\n", "Add a\n\nCloses #5")

    done = ship(wt, env=env)

    assert done.returncode == 0, done.stderr
    assert "#5, which" in done.stdout and "  [ ] Left\n" in done.stdout


FEATURE = "".join(f"feature line {n}\n" for n in range(1, 5))


def refined_feature(wt, message="Refine the feature"):
    """A commit that adds a feature, one that changes only its lines, and the latter."""
    commit(wt, "feature.py", FEATURE, "Add the feature")
    path = wt / "feature.py"
    path.write_text(path.read_text().replace("feature line 2\n", "feature line two\n"))
    git(wt, "commit", "-qam", message)
    return git(wt, "rev-parse", "HEAD")


@pytest.mark.parametrize("local", [True, False])
def test_a_commit_that_only_refines_an_earlier_one_is_warned_of_and_lands(setup, local):
    origin, main, wt, _ = setup
    refined_feature(wt)

    done = ship(wt, *(["--local"] if local else []))

    assert done.returncode == 0, done.stderr
    warning = done.stdout.index("only refine an earlier commit")
    assert warning < done.stdout.index("Notice for other sessions:")
    assert "Refine the feature" in done.stdout[warning:].splitlines()[1]
    upstream = "refs/heads/main" if local else "origin/main"
    assert f"build-state fold --upstream {upstream}" in done.stdout
    landed = main if local else origin
    assert git(landed, "log", "--format=%s", "-2", "main").splitlines() == [
        "Refine the feature",
        "Add the feature",
    ]


def test_the_keep_trailer_silences_the_warning(setup):
    _, _, wt, _ = setup
    refined_feature(wt, "A change of its own\n\nFold-check: keep")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "only refine an earlier commit" not in done.stdout


def test_the_warning_comes_once_though_a_lost_race_rebases_again(setup):
    _, main, wt, other = setup
    refined_feature(wt)
    racing_pre_push(main, other, 1)

    done = ship(wt)

    assert done.returncode == 0, done.stderr
    assert "Another push won the race" in done.stdout
    assert done.stdout.count("only refine an earlier commit") == 1


@pytest.mark.parametrize("local", [True, False])
def test_the_warning_comes_before_any_check(setup, local):
    _, main, wt, _ = setup
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    # A check that fails stops the landing, so a warning after it would never show.
    (hooks / "agent-pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "agent-pre-commit").chmod(0o755)
    refined_feature(wt)

    done = ship(wt, *(["--local"] if local else []))

    assert done.returncode == 1
    assert "only refine an earlier commit" in done.stdout


def test_the_warning_comes_before_the_wait_for_the_main_tree(setup, holder):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    refined_feature(wt)

    done = ship(wt, "--local", "--no-wait")

    assert done.returncode == 3, done.stderr
    assert "only refine an earlier commit" in done.stdout


def test_the_warning_says_how_to_give_a_commit_the_trailer(setup):
    _, _, wt, _ = setup
    refined_feature(wt)

    done = ship(wt, "--local")

    assert "`build-state reword SHA`" in done.stdout


def test_local_says_to_remind_the_user_to_push(setup):
    _, _, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    commit(wt, "b.py", "y = 1\n", "Add b")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "main is 2 commits ahead of origin/main: remind the user to push" in (
        done.stdout
    )


def test_local_says_when_origin_moved_too(setup):
    _, _, wt, other = setup
    commit(other, "c.py", "z = 1\n", "Theirs")
    git(other, "push", "-q", "origin", "main")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "main is 1 commit ahead of origin/main and 1 behind" in done.stdout


def test_local_says_nothing_of_pushing_without_a_remote_copy(setup):
    _, main, wt, _ = setup
    git(main, "remote", "remove", "origin")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "push" not in done.stdout.split("Notice for other sessions:")[1]


def test_local_says_when_it_cant_fetch_to_tell(setup):
    _, main, wt, _ = setup
    git(main, "remote", "set-url", "origin", str(main.parent / "gone.git"))
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local")

    assert done.returncode == 0, done.stderr
    assert "Couldn't fetch origin" in done.stdout


def test_local_gives_up_on_a_fetch_that_hangs(setup):
    _, main, wt, _ = setup
    git(main, "config", "protocol.ext.allow", "always")
    git(main, "remote", "set-url", "origin", "ext::sleep 30")
    commit(wt, "a.py", "x = 1\n", "Add a")

    started = time.monotonic()
    done = ship(wt, "--local", env={"WT_SHIP_PUSH_FETCH_SECONDS": "1"})

    assert done.returncode == 0, done.stderr
    assert "no answer in 1 seconds" in done.stdout
    assert time.monotonic() - started < 20
