"""bin/build-state, run against a throwaway repo."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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


def build_state(repo, *args):
    return subprocess.run(
        [str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, check=False
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
    prompt = shown.stdout
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
    assert shown.stdout.count("Under 400 words") == 2


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
    (tmp_path / "brief.md").write_text("Parse dates.\n")
    skill = code_review_skill(tmp_path)
    return build_state(
        repo, "review-prompt", str(tmp_path / "brief.md"), "--skill", str(skill)
    )


def test_review_prompt_pins_the_commit_under_review(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    reviewed = commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path).stdout

    assert f"git diff {base}...{reviewed}" in prompt
    assert f"git show {reviewed}:" in prompt
    assert "...HEAD" not in prompt


def test_review_prompt_quotes_the_standards_sources_section(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path).stdout

    assert (
        "Search the repo for every file that documents how code should be written."
        in prompt
    )
    assert "Spawn both sub-agents" not in prompt  # the quote stops at the next section
    assert "Read `### 3" not in prompt


def test_review_prompt_names_mutate_check_and_allows_the_repos_build(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path).stdout

    assert "mutate-check" in prompt
    assert "build" in prompt and "ignore" in prompt


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


def test_finish_checks_the_folded_result_when_it_folded_a_fix(repo):
    checked = agent_check(repo, "exit 0")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert checked.read_text().split() == ["checked"]


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


def test_finish_runs_no_check_without_a_fix_to_fold(repo):
    checked = agent_check(repo, "exit 0")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")

    assert build_state(repo, "finish").returncode == 0
    assert not checked.exists()


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
