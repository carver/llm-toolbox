"""bin/build-state, run against a throwaway repo."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import path_without

SCRIPT = Path(__file__).resolve().parent / "bin" / "build-state"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A repo on main with one commit, made once: each test copies it."""
    repo = tmp_path_factory.mktemp("template")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "README", "start")
    return repo


@pytest.fixture
def repo(template, tmp_path):
    shutil.copytree(template, tmp_path, symlinks=True, dirs_exist_ok=True)
    return tmp_path


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def commit(repo, path, message, *flags):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    with open(repo / path, "a") as file:
        file.write(f"{message}\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message, *flags)
    return git(repo, "rev-parse", "HEAD").strip()


def build_state(repo, *args, env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        env=os.environ | (env or {}),
        capture_output=True,
        text=True,
        check=False,
    )


def subjects(repo, since):
    return git(repo, "log", "--format=%s", "--reverse", f"{since}..HEAD").splitlines()


def test_finish_folds_each_fixup_into_its_own_commit_and_keeps_the_rest(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    assert build_state(repo, "start").returncode == 0
    parser = commit(repo, "parser.py", "Parse dates")
    commit(repo, "cli.py", "Add --since")
    commit(repo, "parser.py", "fix parser", f"--fixup={parser}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["Parse dates", "Add --since"]
    assert git(repo, "show", "--format=", "--name-only", "HEAD~1").split() == [
        "parser.py"
    ]
    assert "Parse dates" in done.stdout and "Add --since" in done.stdout


def test_finish_fails_and_names_what_changed_when_another_ref_or_the_config_moved(repo):
    git(repo, "config", "remote.origin.url", "https://user:s3cret@example.com/r.git")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    before = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "branch", "other")
    git(repo, "config", "remote.origin.url", "https://user:n3w@example.com/r.git")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "refs/heads/other" in done.stderr and "remote.origin.url" in done.stderr
    output = done.stdout + done.stderr
    assert "s3cret" not in output and "n3w" not in output
    assert git(repo, "rev-parse", "HEAD").strip() == before  # nothing folded
    # The state is kept for another look.
    assert build_state(repo, "finish").returncode == 1


def test_start_refuses_while_a_build_is_open_unless_forced(repo):
    build_state(repo, "start")
    first_base = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "a.py", "Work")

    refused = build_state(repo, "start")
    forced = build_state(repo, "start", "--force")

    assert refused.returncode == 1 and first_base in refused.stderr
    assert (
        forced.returncode == 0
        and git(repo, "rev-parse", "HEAD").strip() in forced.stdout
    )


CODE_REVIEW = Path.home() / ".agents/skills/code-review/SKILL.md"
STANDARDS_BRIEF = "Report every standard broken. Under 400 words."
SPEC_BRIEF = "Report: (a) what's missing; (b) scope creep. Under 400 words."


# A stand-in for code-review's SKILL.md, laid out like the real one.
CODE_REVIEW_TEXT = f"""### 3. Identify the standards sources

Search the repo for every file that documents how code should be written.

### 4. Spawn both sub-agents in parallel

**Standards sub-agent prompt** should include:

- The full diff command and commit list.
- The brief: "{STANDARDS_BRIEF}"

**Spec sub-agent prompt** should include:

- The diff command and commit list.
- The brief: "{SPEC_BRIEF}"
"""


def code_review_skill(tmp_path, text=CODE_REVIEW_TEXT):
    path = tmp_path / "code-review.md"
    path.write_text(text)
    return path


def test_review_prompt_carries_the_build_and_both_code_review_briefs(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    commit(repo, "AGENTS.md", "Note the house rules")
    commit(repo, "parser.py", "Parse dates")
    brief = tmp_path / "brief.md"
    brief.write_text("Parse dates.\n")

    shown = build_state(
        repo, "review-prompt", str(brief), "--skill", str(code_review_skill(tmp_path))
    )

    assert shown.returncode == 0, shown.stderr
    # What it prints is the short prompt to pass on: it names the file that holds the
    # whole prompt, so the main agent neither reads nor retypes it.
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    prompt_file = git_dir / "build-review-prompt.md"
    assert str(prompt_file) in shown.stdout and "Read" in shown.stdout
    assert len(shown.stdout) < 400, shown.stdout
    prompt = prompt_file.read_text()
    assert "versions the repo pins" in prompt
    head = git(repo, "rev-parse", "HEAD").strip()
    assert str(repo) in prompt and f"git diff {base}...{head}" in prompt
    assert "Parse dates" in prompt and "Note the house rules" in prompt
    assert str(brief) in prompt
    assert f'"{STANDARDS_BRIEF}"' in prompt and f'"{SPEC_BRIEF}"' in prompt
    assert (
        prompt.index(STANDARDS_BRIEF)
        < prompt.index("\n## Spec\n")
        < prompt.index(SPEC_BRIEF)
    )
    assert "AGENTS.md" in prompt and "CONTRIBUTING.md" not in prompt
    assert "coding/standards.md" in prompt


@pytest.mark.parametrize(
    "text, named",
    [
        (CODE_REVIEW_TEXT.replace(f'- The brief: "{SPEC_BRIEF}"\n', ""), "Spec"),
        # A brief moved out of its section mustn't be taken from the next one.
        (
            CODE_REVIEW_TEXT.replace(
                f'- The brief: "{STANDARDS_BRIEF}"\n',
                '\n**Security sub-agent prompt** should include:\n\n- The brief: "Hunt CVEs."\n',
            ),
            "Standards",
        ),
        (
            CODE_REVIEW_TEXT.replace("### 3. Identify", "### 3. Find"),
            "standards sources",
        ),
    ],
)
def test_review_prompt_fails_naming_the_skill_when_it_changed(
    repo, tmp_path, text, named
):
    build_state(repo, "start")
    skill = code_review_skill(tmp_path, text)
    (tmp_path / "brief.md").write_text("Parse dates.\n")

    shown = build_state(
        repo, "review-prompt", str(tmp_path / "brief.md"), "--skill", str(skill)
    )

    assert shown.returncode == 1 and str(skill) in shown.stderr
    assert named in shown.stderr and shown.stdout == ""


def test_review_prompt_says_when_code_review_is_missing(repo, tmp_path):
    build_state(repo, "start")
    (tmp_path / "brief.md").write_text("Parse dates.\n")
    skill = tmp_path / "nowhere.md"

    shown = build_state(
        repo, "review-prompt", str(tmp_path / "brief.md"), "--skill", str(skill)
    )

    assert shown.returncode == 1 and f"{skill} not found" in shown.stderr


def test_review_prompt_refuses_a_brief_that_doesnt_exist(repo, tmp_path):
    build_state(repo, "start")
    skill = code_review_skill(tmp_path)

    shown = build_state(repo, "review-prompt", "breif.md", "--skill", str(skill))

    assert shown.returncode == 1 and "breif.md" in shown.stderr
    assert shown.stdout == ""


def test_finish_removes_the_review_prompt_file_with_the_state(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    (tmp_path / "brief.md").write_text("Parse dates.\n")
    skill = code_review_skill(tmp_path)
    build_state(
        repo, "review-prompt", str(tmp_path / "brief.md"), "--skill", str(skill)
    )
    prompt_file = (
        Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
        / "build-review-prompt.md"
    )
    assert prompt_file.is_file()

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert not prompt_file.exists()


def test_review_prompt_without_start_says_so(repo, tmp_path):
    skill = code_review_skill(tmp_path)

    shown = build_state(repo, "review-prompt", "brief.md", "--skill", str(skill))

    assert shown.returncode == 1 and "build-state start" in shown.stderr


@pytest.mark.skipif(not CODE_REVIEW.is_file(), reason="code-review isn't installed")
def test_review_prompt_finds_both_briefs_in_the_installed_code_review(repo, tmp_path):
    """code-review is third-party: this catches an update that moves its briefs."""
    build_state(repo, "start")
    (tmp_path / "brief.md").write_text("Parse dates.\n")

    shown = build_state(repo, "review-prompt", str(tmp_path / "brief.md"))

    assert shown.returncode == 0, shown.stderr
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    prompt = (git_dir / "build-review-prompt.md").read_text()
    assert prompt.count("Under 400 words") == 2


def test_finish_without_start_says_so(repo):
    done = build_state(repo, "finish")

    assert done.returncode == 1 and "build-state start" in done.stderr


def test_finish_lists_the_test_files_changed_since_the_refactor_base(repo):
    build_state(repo, "start")
    commit(repo, "tests/test_parser.py", "Test parsing")
    commit(repo, "test_cli.py", "Test the CLI")
    assert build_state(repo, "refactor").returncode == 0
    commit(repo, "parser.py", "Tidy the parser")
    commit(repo, "tests/test_parser.py", "Tidy the parser's test")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    listed = {
        line.split("|")[0].strip() for line in done.stdout.splitlines() if "|" in line
    }
    assert listed == {"tests/test_parser.py"}


def test_finish_refuses_uncommitted_changes(repo):
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    (repo / "a.py").write_text("half done\n")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "uncommitted" in done.stderr
    assert (repo / "a.py").read_text() == "half done\n"


def test_a_fixup_that_conflicts_leaves_the_branch_as_it_was(repo):
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    commit(repo, "a.py", "Second")
    commit(repo, "a.py", "fix first", f"--fixup={first}")
    before = git(repo, "rev-parse", "HEAD").strip()

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "CONFLICT" in done.stderr
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert git(repo, "status", "--porcelain") == ""


def test_a_conflicting_fold_names_the_later_commit_that_changed_the_fixs_files(repo):
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    second = commit(repo, "a.py", "Second")
    commit(repo, "b.py", "Elsewhere")
    fix = commit(repo, "a.py", "fix first", f"--fixup={first}")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert f"{fix[:12]} fixup! First conflicts in a.py" in done.stderr
    assert f"{second[:12]} Second" in done.stderr
    assert f"echo 'fixup! {second[:12]}' | build-state reword {fix[:12]}" in done.stderr
    assert "Elsewhere" not in done.stderr  # it changes no file in the conflict
    assert "hint:" not in done.stderr  # git's, which say to continue an aborted rebase


def commit_files(repo, message, *paths, flags=()):
    """A commit that adds a line to each of `paths`."""
    for path in paths:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        with open(repo / path, "a") as file:
            file.write(f"{message}\n")
    git(repo, "add", *paths)
    git(repo, "commit", "-q", "-m", message, *flags)
    return git(repo, "rev-parse", "HEAD").strip()


def test_the_command_a_conflicting_fold_prints_lets_it_fold_from_a_subfolder(repo):
    # diff.relative would print the conflicting files relative to sub/.
    git(repo, "config", "diff.relative", "true")
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    first = commit_files(repo, "First", "a.py", "sub/b.py")
    commit_files(repo, "Second", "a.py")
    third = commit_files(repo, "Third", "sub/b.py")
    fix = commit_files(repo, "fix", "a.py", "sub/b.py", flags=[f"--fixup={first}"])
    commit_files(repo, "Later", "a.py")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    sub = repo / "sub"
    path = {"PATH": f"{SCRIPT.parent}:{os.environ['PATH']}"}

    def run(command):
        return subprocess.run(
            command,
            shell=True,
            cwd=sub,
            env=os.environ | path,
            capture_output=True,
            text=True,
            check=False,
        )

    failed = run("build-state finish")
    commands = [
        line.strip() for line in failed.stderr.splitlines() if "| build-state" in line
    ]

    # Third is the last commit before the fix to change its files, so the fix
    # applies on top of it.
    assert commands == [f"echo 'fixup! {third[:12]}' | build-state reword {fix[:12]}"]
    reworded = run(commands[0])
    done = run("build-state finish")
    assert reworded.returncode == 0, reworded.stderr
    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["First", "Second", "Third", "Later"]
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree


def test_a_conflicting_squash_is_told_to_keep_its_message(repo):
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    second = commit(repo, "a.py", "Second")
    commit(repo, "a.py", "more words", f"--squash={first}")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert f"`squash! {second[:12]}`" in done.stderr
    assert "echo" not in done.stderr  # it would replace the squash's own words


def test_show_prints_the_recorded_bases(repo):
    build_state(repo, "start")
    base = git(repo, "rev-parse", "HEAD").strip()
    refactor_base = commit(repo, "a.py", "Work")
    build_state(repo, "refactor")

    shown = build_state(repo, "show")

    assert shown.returncode == 0
    assert (
        f"base {base}" in shown.stdout
        and f"refactor base {refactor_base}" in shown.stdout
    )


def test_a_squash_commit_folds_without_opening_an_editor(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    first = commit(repo, "a.py", "First")
    commit(repo, "a.py", "more", f"--squash={first}")

    done = subprocess.run(
        [str(SCRIPT), "finish"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"GIT_EDITOR": "false", "EDITOR": "false"},
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["First"]


def test_a_fixup_for_a_commit_before_the_base_fails_finish(repo):
    old = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    commit(repo, "README", "fix start", f"--fixup={old}")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "fixup! start" in done.stderr


def test_the_work_branchs_upstream_may_move(repo):
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")
    git(repo, "config", "remote.origin.url", "/nowhere")
    git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # as a push would

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_a_push_doesnt_trip_a_symbolic_ref_to_the_upstream(repo):
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")
    git(repo, "config", "remote.origin.url", "/nowhere")
    git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # as a push would

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_repointing_a_symbolic_ref_counts_as_a_change(repo):
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/other", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    build_state(repo, "start")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/other")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "refs/remotes/origin/HEAD" in done.stderr


def advance(repo, ref):
    """Move `ref` to a new commit on top of where it is, as another session's merge or push would."""
    parent = git(repo, "rev-parse", ref).strip()
    tree = git(repo, "rev-parse", f"{parent}^{{tree}}").strip()
    new = git(repo, "commit-tree", tree, "-p", parent, "-m", "elsewhere").strip()
    git(repo, "update-ref", ref, new)


def test_in_a_detached_worktree_the_branch_it_lands_on_may_move(repo):
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    advance(repo, "refs/heads/main")
    advance(repo, "refs/remotes/origin/main")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout and "rebase" in done.stdout


def test_a_local_branch_behind_the_base_moves_with_its_remote_twin(repo):
    old = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "b.py", "Upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    git(repo, "update-ref", "refs/heads/main", old)  # the local main is behind the base
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    advance(repo, "refs/remotes/origin/main")  # a fetch of another session's push
    git(repo, "update-ref", "refs/heads/main", "refs/remotes/origin/main")  # a pull

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout


def test_in_a_detached_worktree_another_ref_moving_still_fails(repo):
    git(repo, "checkout", "-q", "--detach")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "branch", "other", "main")

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "refs/heads/other" in done.stderr


@pytest.fixture
def worktree(repo, tmp_path):
    """A detached worktree beside the main tree, which keeps main checked out."""
    path = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(path))
    return path


def test_a_worktree_lands_on_the_main_trees_branch_not_another_at_the_base(
    repo, worktree
):
    git(repo, "branch", "feature")  # at the base too
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    advance(repo, "refs/heads/feature")

    done = build_state(worktree, "finish")

    assert done.returncode == 1 and "refs/heads/feature" in done.stderr


def test_a_second_build_in_a_worktree_still_lands_on_main(repo, worktree):
    commit(worktree, "a.py", "First build, not merged yet")
    build_state(worktree, "start")  # no branch points at this base
    commit(worktree, "b.py", "Second build")
    advance(repo, "refs/heads/main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout


def test_main_moving_before_start_still_makes_it_the_target(repo, worktree):
    advance(repo, "refs/heads/main")  # between `worktree add` and start
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    advance(repo, "refs/heads/main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout


def test_no_rebase_note_once_head_holds_the_moved_branch(repo, worktree):
    build_state(worktree, "start")
    advance(repo, "refs/heads/main")
    commit(worktree, "a.py", "Work")
    git(worktree, "rebase", "-q", "main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "moved since start" not in done.stdout


def test_a_build_on_a_branch_has_no_targets_beyond_its_branch(repo, tmp_path):
    path = tmp_path / "topic"
    git(repo, "worktree", "add", "-q", "-b", "topic", str(path))
    build_state(path, "start")
    commit(path, "a.py", "Work")
    advance(repo, "refs/heads/main")

    done = build_state(path, "finish")

    assert done.returncode == 1 and "refs/heads/main" in done.stderr


def test_in_a_detached_build_a_config_change_still_fails(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    git(repo, "config", "core.something", "new")

    done = build_state(worktree, "finish")

    assert done.returncode == 1 and "core.something" in done.stderr


def test_start_base_records_an_earlier_commit(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "a.py", "Work done before start")

    started = build_state(repo, "start", "--base", "HEAD~1")

    assert started.returncode == 0, started.stderr
    assert build_state(repo, "show").stdout.splitlines() == [f"base {base}"]


def test_start_base_refuses_a_commit_head_doesnt_hold(repo):
    git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "b.py", "Elsewhere")
    git(repo, "checkout", "-q", "main")

    refused = build_state(repo, "start", "--base", side)

    assert refused.returncode == 1 and "ancestor" in refused.stderr
    assert build_state(repo, "show").returncode == 1


def test_a_stash_doesnt_trip_finish(repo):
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    (repo / "a.py").write_text("in progress\n")
    git(repo, "stash", "-q")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_another_worktrees_branch_may_move(repo, tmp_path):
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "prose", str(other))
    git(repo, "update-ref", "refs/remotes/origin/prose", "prose")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    commit(other, "b.py", "Another session's work")
    advance(repo, "refs/remotes/origin/prose")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_a_worktree_added_during_the_build_may_move_its_branch(repo, tmp_path):
    build_state(repo, "start")
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "prose", str(other))
    commit(other, "b.py", "Another session's work")
    commit(repo, "a.py", "Work")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_finish_refuses_a_leftover_conflict_marker(repo):
    build_state(repo, "start")
    commit(repo, "a.py", "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> abc (Work)")
    before = git(repo, "rev-parse", "HEAD").strip()

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "conflict marker" in done.stderr and "a.py" in done.stderr
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert build_state(repo, "show").returncode == 0  # the build stays open


def test_finish_ignores_other_whitespace_errors(repo):
    build_state(repo, "start")
    commit(repo, "a.py", "trailing space   ")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def test_finish_fails_when_folding_changes_the_tree(repo):
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    before = git(repo, "rev-parse", "HEAD").strip()
    # A rebase that alters the last commit stands in for a fold gone wrong.
    hooks = repo / ".git" / "hooks"
    (hooks / "post-rewrite").write_text(
        '#!/bin/sh\n[ "$1" = rebase ] || exit 0\n'
        "echo extra >> b.py && git add b.py && git commit -q --amend --no-edit\n"
    )
    (hooks / "post-rewrite").chmod(0o755)

    done = build_state(repo, "finish")

    assert done.returncode == 1 and "tree" in done.stderr
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert build_state(repo, "show").returncode == 0


def review_prompt_for(repo, tmp_path):
    """The whole review prompt: the file the printed pointer names."""
    (tmp_path / "brief.md").write_text("Parse dates.\n")
    skill = code_review_skill(tmp_path)
    shown = build_state(
        repo, "review-prompt", str(tmp_path / "brief.md"), "--skill", str(skill)
    )
    assert shown.returncode == 0, shown.stderr
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    return (git_dir / "build-review-prompt.md").read_text()


def test_review_prompt_pins_the_commit_under_review(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    reviewed = commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path)

    assert f"git diff {base}...{reviewed}" in prompt
    assert f"git show {reviewed}:" in prompt
    assert "...HEAD" not in prompt


def test_review_prompt_quotes_the_standards_sources_section(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path)

    assert (
        "Search the repo for every file that documents how code should be written."
        in prompt
    )
    assert "Spawn both sub-agents" not in prompt  # the quote stops at the next section
    assert "Read `### 3" not in prompt


def test_review_prompt_names_mutate_check_and_allows_the_repos_build(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path)

    assert "mutate-check" in prompt
    assert "build" in prompt and "ignore" in prompt


GATE = SCRIPT.parent / "agent-commit-gate"


def pass_the_agent_check(repo):
    """Run the repo's agent check through agent-commit-gate, as an agent's commit does,
    so the tree it checks goes on the gate's record of passes."""
    hook = repo / ".git" / "hooks" / "agent-pre-commit"
    run = subprocess.run(
        [str(GATE), "--run", str(hook)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr


def says_the_check_passed(prompt):
    return "already passed" in prompt and "whole test suite" in prompt


def test_review_prompt_says_when_the_agent_check_passed_the_commits_tree(
    repo, tmp_path
):
    agent_check(repo, "exit 0")
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    pass_the_agent_check(repo)

    prompt = review_prompt_for(repo, tmp_path)

    assert says_the_check_passed(prompt)
    assert "mutate-check" in prompt


def no_agent_check(repo):
    commit(repo, "parser.py", "Parse dates")


def no_pass_recorded(repo):
    agent_check(repo, "exit 0")
    commit(repo, "parser.py", "Parse dates")


def another_tree_passed(repo):
    agent_check(repo, "exit 0")
    pass_the_agent_check(repo)  # the base's tree
    commit(repo, "parser.py", "Parse dates")


def the_check_changed_since(repo):
    agent_check(repo, "exit 0")
    commit(repo, "parser.py", "Parse dates")
    pass_the_agent_check(repo)
    agent_check(repo, "exit 0  # a later version of the check")


def the_check_cant_be_read(repo):
    agent_check(repo, "exit 0")
    commit(repo, "parser.py", "Parse dates")
    pass_the_agent_check(repo)
    (repo / ".git" / "hooks" / "agent-pre-commit").chmod(0o100)  # run, not read


@pytest.mark.parametrize(
    "setup",
    [
        no_agent_check,
        no_pass_recorded,
        another_tree_passed,
        the_check_changed_since,
        pytest.param(
            the_check_cant_be_read,
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file"),
        ),
    ],
)
def test_review_prompt_says_nothing_of_a_pass_the_record_doesnt_show(
    repo, tmp_path, setup
):
    build_state(repo, "start")
    setup(repo)

    prompt = review_prompt_for(repo, tmp_path)

    assert not says_the_check_passed(prompt)
    assert "mutate-check" in prompt


def test_a_worktree_removed_during_the_build_still_may_have_moved_its_branch(
    repo, tmp_path
):
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "prose", str(other))
    build_state(repo, "start")
    commit(other, "b.py", "Another session's work, merged and its worktree gone")
    git(repo, "worktree", "remove", str(other))
    commit(repo, "a.py", "Work")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr


def agent_check(repo, body):
    hook = repo / ".git" / "hooks" / "agent-pre-commit"
    hook.write_text(
        f'#!/bin/sh\necho checked >> "$(git rev-parse --git-dir)/checked"\n{body}\n'
    )
    hook.chmod(0o755)
    return repo / ".git" / "checked"


def test_finish_gives_each_commit_it_checks_the_one_before_as_check_base(repo):
    bases = repo / ".git" / "bases"
    agent_check(repo, f'echo "${{CHECK_BASE:-none}}" >> "{bases}"')
    start = git(repo, "rev-parse", "HEAD").strip()
    # The commit the build starts from passed the check when it was made.
    pass_the_agent_check(repo)
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish", env={"PATH": path_without(GATE.name)})

    assert done.returncode == 0, done.stderr
    folded_work = git(repo, "rev-parse", "HEAD~1").strip()
    assert bases.read_text().split() == ["none", start, folded_work]


def test_finish_checks_the_folded_result_when_it_folded_a_fix(repo):
    checked = agent_check(repo, "exit 0")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert checked.read_text().split() == ["checked"]


def test_finish_says_the_work_tree_holds_each_commit_it_checks(repo):
    # An agent that reads the tree meanwhile would see that commit, not HEAD.
    agent_check(repo, "exit 0")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert "not the folded tip" in done.stdout, done.stdout
    assert "read or change nothing" in done.stdout, done.stdout


def test_a_failing_check_after_folding_keeps_the_build_open(repo):
    agent_check(
        repo, "git show :a.py | grep -q bad && { echo a.py is bad; exit 1; }; exit 0"
    )
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "bad", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "a.py is bad" in done.stdout + done.stderr
    assert "--fixup" in done.stderr and "earlier" in done.stderr
    assert build_state(repo, "show").returncode == 0
    assert subjects(repo, "HEAD~2") == ["Work", "More"]  # folded, not undone


def test_finish_checks_each_commit_the_fold_rewrote_and_no_other(repo):
    checked = agent_check(repo, 'git write-tree >> "$(git rev-parse --git-dir)/trees"')
    trees = repo / ".git" / "trees"
    build_state(repo, "start")
    commit(repo, "x.py", "Before")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert trees.read_text().split() == [
        git(repo, "rev-parse", f"HEAD~{n}^{{tree}}").strip() for n in (1, 0)
    ]
    assert checked.read_text().split() == ["checked", "checked"]


@pytest.mark.parametrize(
    "reads",
    [
        "git show :a.py | grep -q uses-c && ! git cat-file -e :c.py 2>/dev/null",
        "grep -q uses-c a.py && ! [ -e c.py ]",
    ],
    ids=["the index", "the work tree"],
)
def test_a_fold_that_breaks_an_earlier_commit_names_it_and_leaves_head(repo, reads):
    # a.py's fix uses c.py, which only the later commit adds
    agent_check(repo, f"{reads} && {{ echo a.py needs c.py; exit 1; }}; exit 0")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "c.py", "Add c")
    commit(repo, "a.py", "uses-c", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    broken = git(repo, "rev-parse", "--short=12", "HEAD~1").strip()
    assert f"{broken} Work" in done.stderr
    assert "a.py needs c.py" in done.stdout + done.stderr
    assert subjects(repo, "HEAD~2") == ["Work", "Add c"]  # folded, not undone
    assert git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert git(repo, "status", "--porcelain") == ""
    assert build_state(repo, "show").returncode == 0


def test_a_rewritten_commit_whose_tree_passed_before_isnt_checked_again(repo):
    agent_check(repo, 'git write-tree >> "$(git rev-parse --git-dir)/trees"')
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    commit(repo, "b.py", "More")  # its tree is the one it had before the fold

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    trees = (repo / ".git" / "trees").read_text().split()
    assert trees == [git(repo, "rev-parse", "HEAD~1^{tree}").strip()]


def test_each_commit_is_checked_on_a_clean_tree_whatever_a_check_leaves(repo):
    agent_check(
        repo,
        '[ -z "$(git status --porcelain --untracked-files=no)" ] || '
        "{ echo dirty; exit 1; }; echo formatted >> a.py",
    )
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stdout + done.stderr
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_finish_keeps_a_branch_that_a_tag_shares_its_name_with(repo):
    agent_check(repo, "exit 0")
    git(repo, "tag", "main")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    assert build_state(repo, "finish").returncode == 0
    assert git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/main"


def test_a_detached_build_ends_detached_at_its_folded_tip(repo):
    agent_check(repo, "exit 0")
    git(repo, "checkout", "-q", "--detach")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert subjects(repo, "HEAD~2") == ["Work", "More"]


def test_the_check_runs_at_the_top_of_the_tree(repo):
    agent_check(repo, "[ -f README ] || { echo not at the top; exit 1; }")
    (repo / "sub").mkdir()
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo / "sub", "finish")

    assert done.returncode == 0, done.stdout + done.stderr


def test_finish_stopped_mid_check_leaves_head_on_its_branch(repo):
    started = repo / ".git" / "started"
    sleeper = repo / ".git" / "sleeper"
    agent_check(repo, f'sleep 30 & echo $! > "{sleeper}"; touch "{started}"; wait')
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    finish = subprocess.Popen(
        [str(SCRIPT), "finish"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()
    assert (
        git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    )  # at the first commit

    finish.terminate()
    finish.wait(timeout=10)

    assert git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert git(repo, "status", "--porcelain") == ""
    # the check stopped too, so it can't touch the tree after
    pid = int(sleeper.read_text())
    deadline = time.monotonic() + 5
    while running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not running(pid)


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split(") ")[1].startswith("Z"))


def test_finish_runs_no_check_without_a_fix_to_fold(repo):
    checked = agent_check(repo, "exit 0")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")

    done = build_state(repo, "finish")
    assert done.returncode == 0
    assert not checked.exists()
    assert "folded tip" not in done.stdout


def test_finish_finds_the_agent_check_from_a_subdirectory(repo):
    checked = agent_check(repo, "exit 0")
    (repo / "sub").mkdir()
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo / "sub", "finish")

    assert done.returncode == 0, done.stderr
    assert checked.read_text().split() == ["checked"]


def test_review_prompt_recommends_a_coding_standards_file_when_the_repo_has_none(
    repo, tmp_path
):
    build_state(repo, "start")
    commit(repo, "a.py", "Add a")
    brief = tmp_path / "brief.md"
    brief.write_text("The brief.\n")
    skill = code_review_skill(tmp_path)
    without = build_state(repo, "review-prompt", str(brief), "--skill", str(skill))
    assert without.returncode == 0, without.stderr
    assert "CODING_STANDARDS.md" in without.stderr
    assert (
        "CODING_STANDARDS.md" not in without.stdout
    )  # the reviewer's prompt is unchanged
    commit(repo, "CODING_STANDARDS.md", "The house rules")
    again = build_state(repo, "review-prompt", str(brief), "--skill", str(skill))
    assert again.stderr == ""


def trees(repo, count):
    return git(repo, "log", "--format=%T", f"-{count}").split()


def test_reword_replaces_one_commits_message_and_keeps_every_tree(repo, tmp_path):
    commit(repo, "a", "first")
    second = commit(repo, "b", "second")
    commit(repo, "c", "third")
    before = trees(repo, 4)
    message = tmp_path / "message"
    message.write_text("second, reworded\n\nWith a body.\n")
    result = build_state(repo, "reword", second, str(message))
    assert result.returncode == 0, result.stderr
    assert trees(repo, 4) == before
    assert subjects(repo, "HEAD~3") == ["first", "second, reworded", "third"]
    assert git(repo, "log", "-1", "--format=%b", "HEAD~1").strip() == "With a body."


def test_reword_takes_the_message_from_stdin_and_rewords_head_too(repo):
    commit(repo, "a", "first")
    result = subprocess.run(
        [str(SCRIPT), "reword", "HEAD"],
        cwd=repo,
        input="first, reworded\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert subjects(repo, "HEAD~1") == ["first, reworded"]


def test_reword_refuses_with_uncommitted_changes(repo, tmp_path):
    sha = commit(repo, "a", "first")
    (repo / "a").write_text("unsaved work\n")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", sha, str(message))
    assert result.returncode == 1
    assert "uncommitted" in result.stderr.lower()
    assert subjects(repo, "HEAD~1") == ["first"]
    assert (repo / "a").read_text() == "unsaved work\n"


def test_reword_refuses_a_pushed_commit(repo, tmp_path):
    sha = commit(repo, "a", "first")
    git(tmp_path, "init", "-q", "--bare", str(tmp_path / "origin.git"))
    git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(repo, "push", "-q", "origin", "main")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", sha, str(message))
    assert result.returncode == 1
    assert "pushed" in result.stderr
    assert subjects(repo, "HEAD~1") == ["first"]


def test_reword_refuses_a_commit_outside_heads_history(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "other")
    elsewhere = commit(repo, "a", "on another branch")
    git(repo, "checkout", "-q", "main")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", elsewhere, str(message))
    assert result.returncode == 1
    assert "HEAD" in result.stderr


def test_reword_refuses_when_merges_follow_the_commit(repo, tmp_path):
    first = commit(repo, "a", "first")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "b", "on the side")
    git(repo, "checkout", "-q", "main")
    commit(repo, "c", "on main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", first, str(message))
    assert result.returncode == 1
    assert "merge" in result.stderr.lower()


def test_reword_keeps_a_first_line_that_starts_with_a_hash(repo, tmp_path):
    sha = commit(repo, "a", "first")
    commit(repo, "b", "second")
    message = tmp_path / "message"
    message.write_text("#123: fix the bug\n")
    assert build_state(repo, "reword", sha, str(message)).returncode == 0
    assert subjects(repo, "HEAD~2")[0] == "#123: fix the bug"


def test_reword_refuses_while_a_later_fixup_names_the_commit(repo, tmp_path):
    sha = commit(repo, "a", "first")
    commit(repo, "a", "fixup! first")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", sha, str(message))
    assert result.returncode == 1
    assert "fixup!" in result.stderr
    assert subjects(repo, "HEAD~2") == ["first", "fixup! first"]


def test_reword_works_whatever_the_rebase_config(repo, tmp_path):
    sha = commit(repo, "a", "first")
    commit(repo, "b", "second")
    git(repo, "config", "rebase.abbreviateCommands", "true")
    git(repo, "config", "rebase.autoSquash", "true")
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", sha, str(message))
    assert result.returncode == 0, result.stderr
    assert subjects(repo, "HEAD~2") == ["reworded", "second"]


def test_reword_leaves_a_rebase_in_progress_alone(repo, tmp_path):
    sha = commit(repo, "a", "first")
    commit(repo, "a", "second")
    git(repo, "checkout", "-q", "-b", "other", "HEAD~2")
    commit(repo, "a", "conflicting")
    subprocess.run(
        ["git", "rebase", "-q", "main"], cwd=repo, capture_output=True, check=False
    )
    message = tmp_path / "message"
    message.write_text("reworded\n")
    result = build_state(repo, "reword", sha, str(message))
    assert result.returncode == 1
    assert "rebase" in result.stderr
    assert (repo / ".git" / "rebase-merge").exists()  # still the user's, in progress


def test_finish_folds_onto_where_head_meets_main_when_main_was_rewritten(
    repo, worktree
):
    """Another session rewrote main under the build (here, amending its tip), and the
    worktree was rebased onto the new main: the recorded base is in neither."""
    base = git(worktree, "rev-parse", "HEAD").strip()
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    commit(worktree, "a.py", "fixup! Work", "--allow-empty")
    git(repo, "commit", "-q", "--amend", "-m", "start, reworded elsewhere")
    advance(repo, "refs/heads/main")
    git(worktree, "rebase", "-q", "--onto", "main", base)

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "rewritten" in done.stdout, done.stdout
    assert subjects(worktree, "main") == ["Work"]


def test_finish_says_so_when_the_base_is_gone_and_head_meets_no_landing_branch(
    repo, worktree
):
    base = git(worktree, "rev-parse", "HEAD").strip()
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    # The work is rebuilt on a root of its own, so HEAD shares no history with the
    # base or with main, the branch it lands on.
    tree = git(worktree, "rev-parse", "HEAD^{tree}").strip()
    root = git(worktree, "commit-tree", tree, "-m", "a new root").strip()
    git(worktree, "rebase", "-q", "--onto", root, base)

    done = build_state(worktree, "finish")

    assert done.returncode == 1
    assert base[:12] in done.stderr and "meets no branch" in done.stderr, done.stderr


def test_a_build_on_a_branch_folds_from_its_upstream_when_that_was_rewritten(repo):
    commit(repo, "m.py", "Middle")
    git(repo, "checkout", "-q", "-b", "work", "--track", "main")
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fixup! Work", "--allow-empty")
    # Another session rewrites main's tip; the work is rebased onto the new one.
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--amend", "-m", "Middle, reworded elsewhere")
    git(repo, "checkout", "-q", "work")
    git(repo, "rebase", "-q", "--onto", "main", base)

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert "where HEAD meets main" in done.stdout, done.stdout
    assert subjects(repo, "main") == ["Work"]


def test_a_branch_at_the_base_that_isnt_the_landing_branch_leaves_the_fold_alone(
    repo, tmp_path
):
    # dev and main both sit at the base, a commit after start, so both share start
    # with HEAD; only main is rewritten.
    commit(repo, "m.py", "Middle")
    git(repo, "branch", "dev")  # sorts before main
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    base = git(worktree, "rev-parse", "HEAD").strip()
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    commit(worktree, "a.py", "fixup! Work", "--allow-empty")
    git(repo, "commit", "-q", "--amend", "-m", "Middle, reworded elsewhere")
    advance(repo, "refs/heads/main")
    git(worktree, "rebase", "-q", "--onto", "main", base)

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "where HEAD meets main" in done.stdout, done.stdout
    assert subjects(worktree, "main") == ["Work"]
