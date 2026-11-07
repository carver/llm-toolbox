"""wt-ship, against a bare remote, a main tree and a detached worktree beside it."""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

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


def ship(cwd, *args, session="s-mine"):
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session}
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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
    assert "conflicts" in done.stderr and "rebase --continue" in done.stderr
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
    assert "won the race" in done.stderr and "run wt-ship again" in done.stderr, (
        done.stderr
    )
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

    done = ship(wt, "--local", "--check", "true")

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
