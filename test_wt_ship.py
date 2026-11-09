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

    done = ship(wt, "--check", "true")

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

    done = ship(wt, "--check", "true")

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

    done = ship(wt, "--check", "true")

    assert done.returncode == 1
    assert "conflicts --continue" in done.stderr, done.stderr
    assert "git rebase --continue" not in done.stderr, done.stderr
    assert remote_main(origin) == before
    assert (Path(git(wt, "rev-parse", "--absolute-git-dir")) / "rebase-merge").exists()


def test_leftover_conflict_markers_are_refused(setup):
    origin, _, wt, _ = setup
    before = remote_main(origin)
    commit(wt, "a.py", "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> abc\n", "Oops")

    done = ship(wt, "--check", "true")

    assert done.returncode == 1 and "conflict marker" in done.stderr
    assert remote_main(origin) == before


def test_a_failing_check_stops_the_push(setup):
    origin, _, wt, _ = setup
    before = remote_main(origin)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "echo broken; exit 3")

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


def test_the_recheck_gets_the_main_tip_it_rebased_onto_as_check_base(setup):
    _, main, wt, _ = setup
    seen = main.parent / "bases"
    install_hook(main, "agent-pre-commit", f'echo "${{CHECK_BASE:-none}}" >> "{seen}"')
    hooks = Path(git(main, "rev-parse", "--absolute-git-dir")) / "hooks"
    commit(main, "b.py", "y = 2\n", "Add b")
    # The main tree's tip passes the check, as it does when it lands.
    passed = subprocess.run(
        [str(GATE), "--run", str(hooks / "agent-pre-commit")], cwd=main, check=False
    )
    assert passed.returncode == 0
    tip = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", env={"PATH": path_without(GATE.name)})

    assert done.returncode == 0, done.stderr
    assert seen.read_text().split() == ["none", tip]
    assert f"given CHECK_BASE {tip[:12]}" in done.stderr


def test_a_check_given_with_check_gets_no_check_base(setup):
    _, main, wt, _ = setup
    seen = main.parent / "base"
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(
        wt,
        "--local",
        "--check",
        f'echo "${{CHECK_BASE:-none}}" > "{seen}"',
        env={"CHECK_BASE": main_head(main)},
    )

    assert done.returncode == 0, done.stderr
    assert seen.read_text().split() == ["none"]


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

    done = ship(wt, "--check", "true")

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

    done = ship(wt, "--check", "true")

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

    done = ship(wt, "--check", "true")

    assert done.returncode == 5
    assert (
        "won the race" in done.stderr
        and "run `wt-ship --check true` again" in done.stderr
    ), done.stderr
    assert "Add a" not in git(origin, "log", "--format=%s", "main")


def test_uncommitted_changes_are_refused(setup):
    _, _, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    (wt / "a.py").write_text("changed\n")

    done = ship(wt, "--check", "true")

    assert done.returncode == 1 and "uncommitted" in done.stderr


def test_nothing_to_push_says_so(setup):
    _, _, wt, _ = setup

    done = ship(wt, "--check", "true")

    assert done.returncode == 0 and "nothing to push" in done.stdout


def test_remove_removes_the_worktree_after_the_push(setup):
    origin, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--check", "true", "--remove")

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new and not wt.exists()
    assert str(wt) not in git(main, "worktree", "list")


def test_remove_is_refused_in_the_main_tree(setup):
    _, main, _, _ = setup
    commit(main, "a.py", "x = 1\n", "Add a")

    done = ship(main, "--check", "true", "--remove")

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

    done = ship(wt, "--check", "true")

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

    done = ship(wt, "--local", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    notice = done.stdout.split("Notice for other sessions:")[1]
    assert "Files: README.md, bin/ (2), skills/build/ (1)." in notice, notice


def test_local_rebases_onto_what_the_main_tree_got_meanwhile(setup):
    _, main, wt, _ = setup
    theirs = commit(main, "b.py", "y = 2\n", "Add b")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]
    assert git(main, "rev-parse", "main~1") == theirs


def moving_check(main, moves):
    """A check that lands another session's commit in the main tree on its first
    `moves` runs, and counts its runs in a file beside the main tree."""
    ran = main.parent / "checks"
    check = (
        f'echo x >> "{ran}"; [ "$(wc -l < "{ran}")" -gt {moves} ] && exit 0; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm "Race $(wc -l < "{ran}")"'
    )
    return check, ran


def test_local_keeps_rebasing_while_the_main_tree_moves(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    check, ran = moving_check(main, moves=3)

    done = ship(wt, "--local", "--check", check)

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines()[0] == "Add a"
    assert ran.read_text().count("x") == 4
    assert not flag(main).exists()


def test_local_stops_after_its_tries_and_says_what_moved_the_branch(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    check, ran = moving_check(main, moves=1000)

    done = ship(wt, "--local", "--remove", "--check", check)

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
    check = (
        f'echo x >> "{ran}"; [ "$(wc -l < "{ran}")" -gt 1 ] && exit 0; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm Race'
    )

    done = ship(wt, "--local", "--check", check)

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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert "s-other" in done.stderr and "2026-11-07T01:02:03" in done.stderr
    assert main_head(main) == before
    assert flag(main).read_text() == "s-other 2026-11-07T01:02:03+00:00\n"


def test_local_keeps_a_claim_this_session_holds(setup):
    _, main, wt, _ = setup
    flag(main).write_text("s-mine 2026-11-07T01:02:03+00:00\n")
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert flag(main).read_text() == "s-mine 2026-11-07T01:02:03+00:00\n"


def test_local_stops_when_the_main_trees_changes_touch_the_same_files(setup):
    _, main, wt, _ = setup
    before = main_head(main)
    (main / "README").write_text("start\nthe user's edit\n")
    commit(wt, "README", "ours\n", "Ours")

    done = ship(wt, "--local", "--check", "true", "--remove")

    assert done.returncode == 4, done.stderr
    assert "ask the user" in done.stderr and "README" in done.stderr
    assert main_head(main) == before
    assert (main / "README").read_text() == "start\nthe user's edit\n"
    assert not flag(main).exists()
    assert wt.exists()  # --remove only after a landing


def test_local_remove_removes_the_worktree_once_landed(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", "--remove")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new and not wt.exists()


def test_local_with_nothing_to_land_says_so(setup):
    _, _, wt, _ = setup

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0 and "nothing to land" in done.stdout


@pytest.mark.parametrize(
    "args", [["--remote", "up"], ["--branch", "dev"]], ids=["remote", "branch"]
)
def test_local_refuses_another_target(setup, args):
    _, main, wt, _ = setup
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", *args)

    assert done.returncode == 1 and "--local" in done.stderr
    assert main_head(main) == before


def test_local_takes_branch_naming_the_main_trees_own(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--branch", "main", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_local_refuses_a_detached_main_tree(setup):
    _, main, wt, _ = setup
    git(main, "checkout", "-q", "--detach")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true", "--remove")

    assert done.returncode == 1 and "doesn't hold" in done.stderr
    assert wt.exists() and not flag(main).exists()


def test_local_lands_when_a_tag_shares_the_branchs_name(setup):
    _, main, wt, _ = setup
    git(main, "tag", "main", "HEAD")
    commit(main, "b.py", "y = 2\n", "Add b")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "refs/heads/main").splitlines() == [
        "Add a",
        "Add b",
        "start",
    ]


def test_local_failing_check_says_nothing_was_landed(setup):
    _, main, wt, _ = setup
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "exit 3")

    assert done.returncode == 1
    assert "nothing was landed" in done.stderr and "push" not in done.stderr
    assert main_head(main) == before


def test_local_stopped_during_the_retry_releases_the_claim(setup):
    _, main, wt, _ = setup
    commit(wt, "a.py", "x = 1\n", "Add a")
    ran, started = main.parent / "checks", main.parent / "retry-started"
    # The first check lands another commit in the main tree, forcing the retry under
    # the claim; the retry's check waits to be stopped.
    check = (
        f'echo x >> "{ran}"; if [ "$(wc -l < "{ran}")" -gt 1 ]; then'
        f' touch "{started}"; sleep 30; exit 0; fi; '
        "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX; "
        f'cd "{main}" && echo z >> c.py && git add c.py && git commit -qm Race'
    )
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": "s-mine"}
    run = subprocess.Popen(
        [str(SCRIPT), "--local", "--check", check],
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

    done = ship(main, "--local", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true")

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
    assert ship(wt, "--local", "--check", "true").returncode == 0
    # The main tree drops it, then gains another commit.
    git(main, "reset", "-q", "--hard", "HEAD~1")
    commit(main, "d.py", "w = 4\n", "Add d")
    commit(wt, "c.py", "z = 3\n", "Add c")

    done = ship(wt, "--local", "--check", "true")

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
    assert ship(wt, "--local", "--check", "true").returncode == 0
    git(main, "reset", "-q", "--hard", "HEAD~1")
    commit(wt, "c.py", "z = 3\n", "Add c")

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert git(main, "log", "--format=%s", "main").splitlines() == ["Add c", "start"]
    assert "Not replaying" in done.stdout and "Add a" in done.stdout


def test_local_folds_fixups_into_the_commits_they_fix(setup):
    _, main, wt, _ = setup
    first = commit(wt, "a.py", "x = 1\n", "Add a")
    with open(wt / "a.py", "a") as file:
        file.write("y = 2\n")
    git(wt, "commit", "-q", "-a", f"--fixup={first}")

    done = ship(wt, "--local", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert not flag(main).exists()


def test_local_waits_while_another_landing_holds_the_flag_then_lands(
    setup, holder, background
):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local", "--check", "true")
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

    waiting = background(wt, "--local", "--check", "true")
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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert flag(main).read_text() == text
    assert queue(main) == []


def test_no_wait_stops_at_once_on_a_held_flag(setup, holder):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    started = time.monotonic()
    done = ship(wt, "--local", "--no-wait", "--check", "true", wait="30")

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

    one = background(wt, "--local", "--check", "true", session="s-one")
    wait_until(lambda: len(queue(main)) == 1, "the first waiter to join")
    two = background(second_wt, "--local", "--check", "true", session="s-two")
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

    killed = background(wt, "--local", "--check", "true", session="s-one")
    wait_until(lambda: len(queue(main)) == 1, "the first waiter to join")
    killed.kill()  # no chance to clean up: its entry stays behind
    killed.wait()
    assert queued_sessions(main) == ["s-one"]
    two = background(second_wt, "--local", "--check", "true", session="s-two")
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

    waiting = background(wt, "--local", "--check", "true")
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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

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

    done = ship(wt, "--local", "--no-wait", "--check", "true")

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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert flag(main).read_text() == text


def test_another_run_of_this_session_holding_the_flag_is_waited_on(setup, holder):
    # A subagent, or a rerun after a Bash timeout, shares the session's id.
    _, main, wt, _ = setup
    text = holder_flag(holder()).replace("s-other", "s-mine", 1)
    flag(main).write_text(text)
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert main_head(main) == before
    assert flag(main).read_text() == text


def test_no_wait_doesnt_jump_the_queue(setup, holder):
    _, main, wt, _ = setup
    entry = queue_entry(main, holder())
    before = main_head(main)
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--no-wait", "--check", "true")

    assert done.returncode == 3, done.stderr
    assert main_head(main) == before
    assert str(entry) in done.stderr and "s-ahead" in done.stderr


def test_giving_up_behind_a_waiter_names_its_entry(setup, holder):
    _, main, wt, _ = setup
    entry = queue_entry(main, holder())
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 3, done.stderr
    assert str(entry) in done.stderr and "s-ahead" in done.stderr
    assert "2026-11-07T01:00:00" in done.stderr
    assert [e.name for e in queue(main)] == [entry.name]


def test_a_waiter_stopped_with_ctrl_c_leaves_quietly(setup, holder, background):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    waiting = background(wt, "--local", "--check", "true")
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

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 1
    assert done.stderr.count("\n") == 1, done.stderr
    assert "wt-ship-queue" in done.stderr


def test_files_in_the_queue_that_arent_entries_are_ignored(setup):
    _, main, wt, _ = setup
    folder = main / ".git" / "wt-ship-queue"
    (folder / "0000-sub").mkdir(parents=True)
    (folder / "README").write_text("")
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", wait="0.3")

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new


def test_the_flag_goes_only_while_it_holds_this_runs_text(setup):
    # Should another session's text replace this run's flag, leaving deletes nothing.
    _, main, wt, _ = setup
    theirs = "s-other 2026-11-07T01:02:03+00:00\n"
    install_hook(main, "post-merge", f"printf '{theirs}' > \"{main}/CLAUDE_WORKING\"")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true")

    assert done.returncode == 0, done.stderr
    assert flag(main).read_text() == theirs


def test_waiting_says_whom_it_waits_on_once_per_change(setup, holder):
    _, main, wt, _ = setup
    flag(main).write_text(holder_flag(holder()))
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(wt, "--local", "--check", "true", wait="0.5")

    assert done.returncode == 3
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("Waiting")]
    assert len(lines) == 1, done.stdout
    assert "s-other" in lines[0] and "2026-11-07T01:02:03" in lines[0]


# -C: a call that starts outside the worktree keeps its shell out of the folder
# --remove deletes.


def test_c_lands_and_removes_the_worktree_from_outside_it(setup):
    _, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(
        main, "-C", "../repo-wt-topic", "--local", "--remove", "--check", "true"
    )

    assert done.returncode == 0, done.stderr
    assert main_head(main) == new
    assert not wt.exists()


def test_c_pushes_from_outside_the_worktree(setup):
    origin, main, wt, _ = setup
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(main.parent, "-C", str(wt), "--check", "true")

    assert done.returncode == 0, done.stderr
    assert remote_main(origin) == new


def test_c_naming_no_folder_is_a_usage_error(setup):
    _, main, _, _ = setup

    done = ship(main, "-C", "../nowhere", "--local", "--check", "true")

    assert done.returncode == 2
    assert "../nowhere" in done.stderr
    assert done.stderr.count("\n") == 1, done.stderr


def test_c_naming_a_file_is_a_usage_error(setup):
    _, main, _, _ = setup

    done = ship(main, "-C", "README", "--local", "--check", "true")

    assert done.returncode == 2
    assert "README" in done.stderr
    assert done.stderr.count("\n") == 1, done.stderr


def test_a_stop_says_to_run_the_same_command_again(setup):
    _, main, wt, _ = setup
    flag(main).write_text("s-other 2026-11-07T01:02:03+00:00\n")
    commit(wt, "a.py", "x = 1\n", "Add a")

    done = ship(
        main, "-C", "../repo-wt-topic", "--local", "--check", "true", wait="0.3"
    )

    assert done.returncode == 3, done.stderr
    assert "`wt-ship -C ../repo-wt-topic --local --check true` again" in done.stderr


def test_a_check_given_with_check_waits_its_turn_in_heavys_queue(
    setup, holder, background
):
    _, main, wt, _ = setup
    alive = holder()
    lane = Path(os.environ["HEAVY_DIR"]) / "normal"
    lane.mkdir(parents=True)
    (lane / "slot").write_text(
        f"s-other 2026-11-07T01:02:03+00:00 host={socket.gethostname()}"
        f" pid={alive.pid} start={start_time(alive.pid)}\ncwd=/x\ncmd=cargo test\n"
    )
    new = commit(wt, "a.py", "x = 1\n", "Add a")

    shipping = background(wt, "--local", "--check", "true")
    wait_until(
        lambda: any(p.name[:1].isdigit() for p in lane.iterdir()), "the check to queue"
    )
    assert main_head(main) != new
    alive.kill()
    alive.wait()
    status, _, err = finish(shipping)

    assert status == 0, err
    assert main_head(main) == new
