"""bin/build-state, run against a throwaway repo."""

import json
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


def build_state(repo, *args, env=None, reviewed=True):
    """Run build-state in `repo`. Before a finish, unless `reviewed` is False, record a
    review of HEAD and a refactor base at HEAD where the build has none, as /build's
    steps would: most tests here are about folding, not about the order of steps."""
    if args[:1] == ("finish",) and reviewed:
        record_review_steps(repo)
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        env=os.environ | (env or {}),
        capture_output=True,
        text=True,
        check=False,
    )


def record_review_steps(repo):
    state = Path(git(repo, "rev-parse", "--absolute-git-dir").strip()) / (
        "build-state.json"
    )
    if not state.is_file():
        return
    head = git(repo, "rev-parse", "HEAD").strip()
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    recorded = json.loads(state.read_text())
    for sha_key, tree_key in (
        ("reviewed", "reviewed_tree"),
        ("refactor_base", "refactor_tree"),
    ):
        if sha_key not in recorded:
            recorded |= {sha_key: head, tree_key: tree}
    state.write_text(json.dumps(recorded))


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


def test_finish_ignores_a_tag_made_meanwhile_off_the_work(repo):
    # As CI tags a release on main and a fetch brings the tag in.
    build_state(repo, "start")
    base = git(repo, "rev-parse", "HEAD").strip()
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    git(repo, "tag", "-a", "-m", "release", "release-v2", base)

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(repo, base) == ["Work"]


def test_finish_still_stops_for_a_tag_made_meanwhile_on_the_work(repo):
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "a.py", "fix work", f"--fixup={work}")
    git(repo, "tag", "on-work", work)

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "refs/tags/on-work" in done.stderr


def test_finish_still_stops_for_a_tag_deleted_meanwhile_from_the_work(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    work = commit(repo, "a.py", "Work")
    git(repo, "tag", "on-work", work)
    build_state(repo, "start", "--base", base)
    commit(repo, "a.py", "fix", f"--fixup={work}")
    git(repo, "tag", "-d", "on-work")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "refs/tags/on-work" in done.stderr


def test_finish_stops_for_a_deleted_annotated_tag_on_the_work_even_once_pruned(repo):
    # Its tag object is gone, so where it pointed can't be read: it counts.
    base = git(repo, "rev-parse", "HEAD").strip()
    work = commit(repo, "a.py", "Work")
    git(repo, "tag", "-a", "-m", "x", "on-work", work)
    build_state(repo, "start", "--base", base)
    commit(repo, "a.py", "fix", f"--fixup={work}")
    git(repo, "tag", "-d", "on-work")
    git(repo, "gc", "-q", "--prune=now")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "refs/tags/on-work" in done.stderr


def test_finish_ignores_a_tag_on_main_ahead_of_the_base(repo, worktree):
    build_state(worktree, "start")
    work = commit(worktree, "a.py", "Work")
    commit(worktree, "a.py", "fix", f"--fixup={work}")
    advance(repo, "refs/heads/main")
    git(repo, "tag", "release", "main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr


def test_finish_ignores_a_tag_on_main_after_main_was_rewritten(repo, worktree):
    # The fold starts where HEAD meets the rewritten main, not at the recorded base.
    base = git(worktree, "rev-parse", "HEAD").strip()
    build_state(worktree, "start")
    work = commit(worktree, "a.py", "Work")
    commit(worktree, "a.py", "fix", f"--fixup={work}")
    git(repo, "commit", "-q", "--amend", "-m", "start, reworded elsewhere")
    advance(repo, "refs/heads/main")
    git(worktree, "rebase", "-q", "--onto", "main", base)
    git(repo, "tag", "-a", "-m", "release", "release-v2", "main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr


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


def test_start_refusal_says_to_review_refactor_and_finish_the_open_build(repo):
    build_state(repo, "start")
    refused = build_state(repo, "start")
    assert "review-prompt" in refused.stderr, refused.stderr
    assert "refactor" in refused.stderr, refused.stderr


def review(repo, tmp_path):
    (tmp_path / "brief.md").write_text("Parse dates.\n")
    shown = build_state(
        repo,
        "review-prompt",
        str(tmp_path / "brief.md"),
        "--skill",
        str(code_review_skill(tmp_path)),
    )
    assert shown.returncode == 0, shown.stderr


def refused_keeping_the_build(repo, done, *words):
    assert done.returncode == 1, done.stdout + done.stderr
    for word in words:
        assert word in done.stderr, done.stderr
    assert build_state(repo, "show").returncode == 0


def test_a_build_started_before_finish_checked_its_steps_finishes_as_before(repo):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    # The state an earlier build-state wrote, with no mark that it checks the steps.
    state = Path(git(repo, "rev-parse", "--absolute-git-dir").strip()) / (
        "build-state.json"
    )
    recorded = json.loads(state.read_text())
    del recorded["checks_steps"]
    state.write_text(json.dumps(recorded))

    done = build_state(repo, "finish", reviewed=False)

    assert done.returncode == 0, done.stdout + done.stderr


def test_finish_refuses_a_build_with_no_review(repo):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    build_state(repo, "refactor")

    done = build_state(repo, "finish", reviewed=False)

    refused_keeping_the_build(repo, done, "review-prompt")


def test_finish_refuses_a_reviewed_build_with_no_refactor_base(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)

    done = build_state(repo, "finish", reviewed=False)

    refused_keeping_the_build(repo, done, "refactor")


def test_finish_refuses_a_refactor_base_recorded_before_the_review(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    build_state(repo, "refactor")
    commit(repo, "parser.py", "Parse times")
    review(repo, tmp_path)

    done = build_state(repo, "finish", reviewed=False)

    refused_keeping_the_build(repo, done, "refactor")


def test_a_reword_after_the_review_needs_no_new_review(repo, tmp_path):
    build_state(repo, "start")
    first = commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)
    reworded = subprocess.run(
        [str(SCRIPT), "reword", first],
        cwd=repo,
        input="Parse ISO dates\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert reworded.returncode == 0, reworded.stderr
    build_state(repo, "refactor")

    done = build_state(repo, "finish", reviewed=False)

    assert done.returncode == 0, done.stdout + done.stderr


def test_finish_asks_for_a_new_review_when_the_reviewed_commit_was_rewritten(
    repo, tmp_path
):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)
    # A rewrite that changes the tree, as a rebase onto new work does.
    git(repo, "commit", "-q", "--amend", "--allow-empty", "-m", "Parse dates")
    (repo / "parser.py").write_text("changed\n")
    git(repo, "commit", "-q", "-a", "--amend", "-m", "Parse dates")
    build_state(repo, "refactor")

    done = build_state(repo, "finish", reviewed=False)

    refused_keeping_the_build(repo, done, "review-prompt", "rewritten")


def test_show_says_which_commit_the_review_covered(repo, tmp_path):
    build_state(repo, "start")
    sha = commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)
    assert f"reviewed {sha}" in build_state(repo, "show").stdout


def test_finish_refuses_a_commit_made_after_the_review_that_isnt_a_fix(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)
    commit(repo, "times.py", "Parse times")
    build_state(repo, "refactor")

    done = build_state(repo, "finish", reviewed=False)

    refused_keeping_the_build(repo, done, "Parse times")


def test_finish_takes_fixes_after_the_review_and_commits_after_the_refactor_base(
    repo, tmp_path
):
    build_state(repo, "start")
    first = commit(repo, "parser.py", "Parse dates")
    review(repo, tmp_path)
    commit(repo, "parser.py", "Fix", f"--fixup={first}")
    build_state(repo, "refactor")
    commit(repo, "helpers.py", "Name the date helpers")

    done = build_state(repo, "finish", reviewed=False)

    assert done.returncode == 0, done.stdout + done.stderr


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


def test_finish_lists_the_test_files_changed_since_the_refactor_base(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "tests/test_parser.py", "Test parsing")
    commit(repo, "test_cli.py", "Test the CLI")
    review(repo, tmp_path)
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
    record_review_steps(repo)
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
    record_review_steps(repo)

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


def test_a_fixup_of_an_earlier_builds_unpushed_commit_folds_into_it(repo, worktree):
    build_state(worktree, "start")
    first = commit(worktree, "a.py", "First")
    assert build_state(worktree, "finish").returncode == 0
    build_state(worktree, "start")
    commit(worktree, "b.py", "Second")
    commit(worktree, "a.py", "fix first", f"--fixup={first}")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(worktree, "main") == ["First", "Second"]
    assert "fix first" in (worktree / "a.py").read_text()
    assert git(worktree, "show", "HEAD~1:a.py") == "First\nfix first\n"
    assert "First" in done.stdout and "folding from" in done.stdout.lower()


def test_fixes_of_two_earlier_commits_and_of_the_builds_own_all_fold(repo, worktree):
    first = commit(worktree, "a.py", "First")
    second = commit(worktree, "b.py", "Second")
    build_state(worktree, "start")
    third = commit(worktree, "c.py", "Third")
    commit(worktree, "b.py", "fix second", f"--fixup={second}")
    commit(worktree, "a.py", "fix first", f"--fixup={first}")
    commit(worktree, "c.py", "fix third", f"--fixup={third}")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(worktree, "main") == ["First", "Second", "Third"]
    for rev, path, text in [
        ("HEAD~2", "a.py", "First\nfix first\n"),
        ("HEAD~1", "b.py", "Second\nfix second\n"),
        ("HEAD", "c.py", "Third\nfix third\n"),
    ]:
        assert git(worktree, "show", f"{rev}:{path}") == text


def test_a_fixup_of_a_commit_on_a_remote_tracking_ref_still_fails_finish(
    repo, worktree
):
    first = commit(worktree, "a.py", "First")
    git(worktree, "update-ref", "refs/remotes/origin/topic", first)
    build_state(worktree, "start")
    commit(worktree, "a.py", "fix first", f"--fixup={first}")

    done = build_state(worktree, "finish")

    assert done.returncode == 1
    assert "fixup! First" in done.stderr and "refs/remotes/origin/topic" in done.stderr


def test_a_fixup_naming_no_commit_fails_finish_saying_so(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "fixup! Nothing like this")

    done = build_state(worktree, "finish")

    assert done.returncode == 1
    assert "fixup! Nothing like this" in done.stderr and "no commit" in done.stderr


def test_a_merge_in_the_wider_range_stops_the_wider_fold(repo, worktree):
    first = commit(worktree, "a.py", "First")
    git(worktree, "checkout", "-q", "-b", "side", "HEAD~1")
    commit(worktree, "s.py", "Side")
    git(worktree, "checkout", "-q", "--detach", first)
    git(worktree, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    git(worktree, "branch", "-D", "side")
    build_state(worktree, "start")
    commit(worktree, "a.py", "fix first", f"--fixup={first}")

    done = build_state(worktree, "finish")

    assert done.returncode == 1 and "merge" in done.stderr


def test_a_fixup_of_a_commit_on_a_branch_still_fails_finish(repo, worktree):
    first = commit(worktree, "a.py", "First")
    git(worktree, "branch", "pushed")
    build_state(worktree, "start")
    commit(worktree, "a.py", "fix first", f"--fixup={first}")

    done = build_state(worktree, "finish")

    assert done.returncode == 1 and "fixup! First" in done.stderr


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

    # Reported, by name, as a remote-tracking ref: a fold doesn't read it.
    assert done.returncode == 0, done.stderr
    assert "refs/remotes/origin/HEAD" in reported(done)


def reported(done):
    """The line finish prints for what changed that it only reports."""
    return next((ln for ln in done.stdout.splitlines() if "tell the user" in ln), "")


def test_a_push_of_the_work_for_ci_is_reported_not_a_stop(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    # As `git push origin HEAD:refs/heads/ci-topic` would.
    git(worktree, "update-ref", "refs/remotes/origin/ci-topic", "HEAD")
    commit(worktree, "a.py", "Review fix")
    git(repo, "update-ref", "refs/remotes/origin/theirs", "main")  # and a fetch

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    line = reported(done)
    assert "refs/remotes/origin/ci-topic" in line
    assert "1 other remote-tracking ref off the work" in line


def test_deleting_the_ci_branch_is_reported_not_a_stop(repo, worktree):
    commit(worktree, "a.py", "Work")
    git(worktree, "update-ref", "refs/remotes/origin/ci-topic", "HEAD")  # pushed for CI
    build_state(worktree, "start", "--base", "HEAD~1")
    # As `git push -d origin ci-topic` would.
    git(worktree, "update-ref", "-d", "refs/remotes/origin/ci-topic")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "refs/remotes/origin/ci-topic" in reported(done)


def test_remote_refs_off_the_work_are_counted_not_named(repo, worktree):
    for name in ("one", "two"):
        git(repo, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    # A fetch in any worktree: two branches moved, one new.
    advance(repo, "refs/remotes/origin/one")
    advance(repo, "refs/remotes/origin/two")
    git(repo, "update-ref", "refs/remotes/origin/three", "main")
    git(repo, "config", "core.hooksPath", ".githooks")  # reported, but no ref

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    line = reported(done)
    assert "3 remote-tracking refs off the work" in line and "origin/one" not in line


def test_the_targets_remote_twin_moving_alone_is_named_as_moved(repo, worktree):
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    advance(repo, "refs/remotes/origin/main")  # a fetch of another session's push

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start: rebase onto it" in done.stdout
    assert not reported(done)


def test_a_build_on_a_branch_reports_another_remote_tracking_ref(repo):
    git(repo, "checkout", "-q", "-b", "work")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    advance(repo, "refs/remotes/origin/main")  # a fetch of another session's push

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert reported(done).endswith(": 1 remote-tracking ref off the work")


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
    assert "main moved since start" in done.stdout
    assert "rebase" not in done.stdout


def test_a_target_moved_back_is_still_named(repo, worktree):
    # A forked build, then main rewound to the fork point: a landing lost.
    commit(worktree, "own.py", "The owner's commit, not on main")
    fork = git(repo, "rev-parse", "main").strip()
    advance(repo, "refs/heads/main")
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    git(repo, "update-ref", "refs/heads/main", fork)

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout


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


def test_finish_keeps_a_branch_that_a_tag_shares_its_name_with(repo):
    git(repo, "tag", "main")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    assert build_state(repo, "finish").returncode == 0
    assert git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/main"


def test_a_detached_build_ends_detached_at_its_folded_tip(repo):
    git(repo, "checkout", "-q", "--detach")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert subjects(repo, "HEAD~2") == ["Work", "More"]


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


def says_not_to_run_the_suite(prompt):
    return "wt-ship" in prompt and "whole test suite" in prompt


def test_review_prompt_says_not_to_run_the_suite_when_the_repo_has_an_agent_check(
    repo, tmp_path
):
    agent_check(repo, "exit 0")
    build_state(repo, "start")
    # No pass on record: commits may run a light check, and wt-ship the full one.
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path)

    assert says_not_to_run_the_suite(prompt)
    assert "mutate-check" in prompt


def test_review_prompt_says_nothing_of_the_suite_without_an_agent_check(repo, tmp_path):
    build_state(repo, "start")
    commit(repo, "parser.py", "Parse dates")

    prompt = review_prompt_for(repo, tmp_path)

    assert not says_not_to_run_the_suite(prompt)
    assert "mutate-check" in prompt


def test_finish_runs_no_check_even_on_a_commit_its_fold_rewrote(repo):
    checked = agent_check(repo, "exit 1")
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    commit(repo, "b.py", "More")
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    assert subjects(repo, "HEAD~2") == ["Work", "More"]
    assert not checked.exists()


def test_a_base_forked_from_main_still_lands_on_main(repo, worktree):
    # The owner's commit in the worktree, and another session's landing on main: the
    # base is neither behind main nor ahead of it.
    commit(worktree, "own.py", "The owner's commit, not on main")
    advance(repo, "refs/heads/main")
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    advance(repo, "refs/heads/main")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "main moved since start" in done.stdout


def test_a_changed_hooks_path_is_reported_not_a_stop(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    git(repo, "config", "core.hooksPath", "/elsewhere/s3cret-hooks")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "core.hookspath" in done.stdout and "tell the user" in done.stdout
    assert "s3cret" not in done.stdout + done.stderr


def test_another_branchs_settings_are_reported_not_a_stop(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    git(repo, "config", "branch.topic.remote", "origin")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "branch.topic.remote" in done.stdout


def test_a_setting_of_every_branch_still_stops_finish(repo, worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Work")
    git(repo, "config", "branch.autoSetupMerge", "always")

    done = build_state(worktree, "finish")

    assert done.returncode == 1
    assert "branch.autosetupmerge" in done.stderr


def test_the_work_branchs_upstream_changed_since_start_still_stops_finish(repo):
    git(repo, "checkout", "-q", "-b", "work")
    git(repo, "config", "branch.work.merge", "refs/heads/main")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "config", "branch.work.merge", "refs/heads/other")

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "branch.work.merge" in done.stderr


def test_another_setting_of_the_work_branch_made_since_start_still_stops_finish(repo):
    git(repo, "checkout", "-q", "-b", "work")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "config", "branch.work.pushRemote", "elsewhere")  # where `git push` goes

    done = build_state(repo, "finish")

    assert done.returncode == 1
    assert "branch.work.pushremote" in done.stderr


@pytest.fixture
def origin(repo, tmp_path_factory):
    """A bare repo the work branch can be pushed to, as the remote origin."""
    path = tmp_path_factory.mktemp("origin") / "origin.git"
    git(repo, "init", "-q", "--bare", str(path))
    git(repo, "remote", "add", "origin", str(path))
    return path


def push_u(repo, branch, to="origin"):
    """`git push -u origin BRANCH`, or to another remote or a path, setting the
    branch's remote and merge alone: a global branch.autoSetupRebase would add its
    rebase."""
    git(repo, "-c", "branch.autoSetupRebase=never", "push", "-q", "-u", to, branch)


def test_a_push_u_of_the_work_for_ci_is_reported_not_a_stop(repo, origin):
    git(repo, "checkout", "-q", "-b", "work")
    base = git(repo, "rev-parse", "HEAD").strip()
    build_state(repo, "start")
    work = commit(repo, "a.py", "Work")
    push_u(repo, "work")  # the workspace's PR flow
    commit(repo, "a.py", "fix work", f"--fixup={work}")

    done = build_state(repo, "finish")

    assert done.returncode == 0, done.stderr
    line = reported(done)
    assert "branch.work.remote" in line and "branch.work.merge" in line
    assert "refs/remotes/origin/work" in line
    assert subjects(repo, base) == ["Work"]


def test_an_upstream_the_work_branch_got_since_start_isnt_read_for_its_base(
    repo, origin
):
    git(repo, "checkout", "-q", "-b", "work")
    before = commit(repo, "b.py", "Before the build")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    push_u(repo, "work")
    # The base leaves HEAD's history. origin/work, the CI push, wasn't rewritten.
    git(repo, "rebase", "-q", "--onto", "main", before)

    done = build_state(repo, "finish")

    # As without the push: no upstream at start, so nothing to fold onto.
    assert done.returncode == 1
    assert "meets no branch" in done.stderr, done.stderr
    assert "rewritten" not in done.stdout


def assert_the_upstream_keys_stop(done, why):
    """finish stopped, naming the work branch's remote and merge, each with `why`."""
    assert done.returncode == 1, done.stdout
    for key in ("branch.work.remote", "branch.work.merge"):
        line = next((ln for ln in done.stderr.splitlines() if key in ln), "")
        assert why in line, done.stderr


def test_a_push_u_to_a_path_still_stops_finish(repo, tmp_path_factory):
    path = tmp_path_factory.mktemp("elsewhere") / "elsewhere.git"
    git(repo, "init", "-q", "--bare", str(path))
    git(repo, "checkout", "-q", "-b", "work")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    # As a test leaking into the repo might: the next bare `git push` goes there.
    push_u(repo, "work", to=str(path))

    done = build_state(repo, "finish")

    assert_the_upstream_keys_stop(
        done, "its upstream maps to no remote-tracking ref, as with a path or URL"
    )
    assert str(path) not in done.stdout + done.stderr  # a config value


def test_a_push_u_whose_remote_branch_was_deleted_since_still_stops_finish(
    repo, origin
):
    git(repo, "checkout", "-q", "-b", "work")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    push_u(repo, "work")
    git(repo, "push", "-q", "origin", "--delete", "work")  # the PR closed

    done = build_state(repo, "finish")

    assert_the_upstream_keys_stop(
        done, "its upstream, refs/remotes/origin/work, doesn't exist"
    )


def test_a_local_upstream_set_since_start_still_stops_finish(repo):
    git(repo, "checkout", "-q", "-b", "work")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    git(repo, "branch", "-q", "--set-upstream-to=main")  # remote ".", no push

    done = build_state(repo, "finish")

    assert_the_upstream_keys_stop(
        done, "its upstream, refs/heads/main, isn't a remote-tracking ref of its remote"
    )


def test_a_push_u_tracked_under_another_remotes_name_still_stops_finish(repo, origin):
    git(repo, "remote", "add", "fork", str(origin))
    git(repo, "config", "remote.fork.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(repo, "checkout", "-q", "-b", "work")
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    push_u(repo, "work", to="fork")

    done = build_state(repo, "finish")

    assert_the_upstream_keys_stop(
        done,
        "its upstream, refs/remotes/origin/work, isn't a remote-tracking ref of its"
        " remote",
    )


def test_a_push_u_over_a_remote_branch_there_at_start_still_stops_finish(repo, origin):
    git(repo, "checkout", "-q", "-b", "work")
    git(repo, "push", "-q", "origin", "work")  # before the build, without -u
    build_state(repo, "start")
    commit(repo, "a.py", "Work")
    push_u(repo, "work")

    done = build_state(repo, "finish")

    assert_the_upstream_keys_stop(
        done, "its upstream, refs/remotes/origin/work, was there at start"
    )


def write_commit(repo, message, files):
    for path, text in files.items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_fold_folds_a_refinement_into_its_target_and_keeps_the_rest(worktree):
    commit(worktree, "a.py", "Add a")
    commit(worktree, "b.py", "Add b")
    tree = git(worktree, "rev-parse", "HEAD^{tree}")
    commit(worktree, "a.py", "Refine a\n\nThe second line of a was wrong.")

    done = build_state(worktree, "fold")

    assert done.returncode == 0, done.stderr
    assert subjects(worktree, "main") == ["Add a", "Add b"]
    assert git(worktree, "rev-parse", "HEAD^{tree}") != tree
    assert git(worktree, "show", "HEAD~1:a.py").startswith("Add a\nRefine a\n")
    assert "The second line of a was wrong." in done.stdout
    assert "build-state reword" in done.stdout


def test_fold_with_nothing_to_fold_says_so(worktree):
    commit(worktree, "a.py", "Add a")
    commit(worktree, "b.py", "Add b")

    done = build_state(worktree, "fold")

    assert done.returncode == 0, done.stderr
    assert "Nothing to fold" in done.stdout and "Folded" not in done.stdout
    assert subjects(worktree, "main") == ["Add a", "Add b"]


def test_fold_takes_the_upstream_it_is_given(repo, worktree):
    commit(worktree, "a.py", "Add a")
    other = git(worktree, "rev-parse", "HEAD").strip()
    commit(worktree, "a.py", "Refine a")

    done = build_state(worktree, "fold", "--upstream", other)

    assert done.returncode == 0, done.stderr
    assert "Nothing to fold" in done.stdout


def test_fold_refuses_uncommitted_changes(worktree):
    commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "Refine a")
    (worktree / "a.py").write_text("half done\n")

    done = build_state(worktree, "fold")

    assert done.returncode == 1 and "uncommitted" in done.stderr
    assert (worktree / "a.py").read_text() == "half done\n"


def test_fold_leaves_a_commit_that_uses_a_name_defined_since_its_target(worktree):
    commit(worktree, "a.py", "Add a")
    write_commit(worktree, "Add a helper", {"helper.py": "def shiny_helper():\n"})
    commit(worktree, "a.py", "shiny_helper()")

    done = build_state(worktree, "fold")

    assert done.returncode == 0, done.stderr
    assert subjects(worktree, "main") == ["Add a", "Add a helper", "shiny_helper()"]
    assert "shiny_helper" in done.stdout and "Left in place" in done.stdout


def test_a_fold_that_conflicts_changes_nothing_and_names_the_commits(worktree):
    main_lines = "".join(f"main {n}\n" for n in range(8))
    upstream = write_commit(worktree, "On main", {"a.py": main_lines})
    write_commit(worktree, "Add a", {"a.py": main_lines + "a1\na2\na3\na4\n"})
    # Mostly main's lines, so it stays, and it changes a3, next to a2.
    text = (worktree / "a.py").read_text()
    for n in range(8):
        text = text.replace(f"main {n}\n", f"main {n}!\n")
    write_commit(worktree, "Rework main", {"a.py": text.replace("a3\n", "a3!\n")})
    write_commit(
        worktree,
        "Fix a2",
        {"a.py": (worktree / "a.py").read_text().replace("a2\n", "a2!\n")},
    )
    before = git(worktree, "rev-parse", "HEAD")

    done = build_state(worktree, "fold", "--upstream", upstream)

    assert done.returncode == 1
    assert "Fix a2" in done.stderr and "Rework main" in done.stderr
    assert git(worktree, "rev-parse", "HEAD") == before
    assert git(worktree, "status", "--porcelain") == ""
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").strip())
    assert not (git_dir / "rebase-merge").exists()


def test_finish_warns_of_commits_that_only_refine_and_keeps_them(worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Add a")
    commit(worktree, "b.py", "Add b")
    commit(worktree, "a.py", "Refine a")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "Refine a" in done.stdout and "build-state fold" in done.stdout
    assert "wt-ship" in done.stdout
    assert subjects(worktree, "main") == ["Add a", "Add b", "Refine a"]


def test_finish_says_nothing_of_folds_when_none_are_due(worktree):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Add a")
    commit(worktree, "b.py", "Add b")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "build-state fold" not in done.stdout


def test_fold_refuses_while_a_rebase_is_in_progress(worktree):
    commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "Refine a")
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "rebase-merge").mkdir()

    done = build_state(worktree, "fold")

    assert done.returncode == 1 and "rebase is in progress" in done.stderr


def test_fold_refuses_a_merge_among_the_commits(worktree):
    commit(worktree, "a.py", "Add a")
    side = git(worktree, "rev-parse", "HEAD").strip()
    commit(worktree, "a.py", "Refine a")
    git(worktree, "checkout", "-q", "--detach", side)
    commit(worktree, "b.py", "Add b")
    git(worktree, "merge", "-q", "--no-ff", "-m", "Merge", "HEAD@{2}")
    before = git(worktree, "rev-parse", "HEAD")

    done = build_state(worktree, "fold")

    assert done.returncode == 1 and "merge" in done.stderr
    assert git(worktree, "rev-parse", "HEAD") == before


def test_fold_with_the_main_tree_detached_asks_for_the_upstream(repo, worktree):
    git(repo, "checkout", "-q", "--detach")
    commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "Refine a")

    done = build_state(worktree, "fold")

    assert done.returncode == 1 and "--upstream" in done.stderr


@pytest.mark.parametrize("origin_ahead", [True, False])
def test_fold_lands_on_origins_copy_of_the_branch_only_when_it_is_ahead(
    repo, worktree, origin_ahead
):
    added = commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "Refine a")
    if origin_ahead:
        # origin's main already holds "Add a", so "Refine a" is all that lands.
        git(repo, "update-ref", "refs/remotes/origin/main", added)
    else:
        side = git(repo, "commit-tree", "-m", "elsewhere", "HEAD^{tree}").strip()
        git(repo, "update-ref", "refs/remotes/origin/main", side)

    done = build_state(worktree, "fold")

    assert done.returncode == 0, done.stderr
    assert ("Nothing to fold" in done.stdout) is origin_ahead


def test_finish_warns_that_wt_ship_lands_a_commit_using_a_name_defined_since(
    worktree,
):
    build_state(worktree, "start")
    commit(worktree, "a.py", "Add a")
    write_commit(worktree, "Add a helper", {"helper.py": "def shiny_helper():\n"})
    commit(worktree, "a.py", "shiny_helper()")

    done = build_state(worktree, "finish")

    assert done.returncode == 0, done.stderr
    assert "`build-state fold` leaves them" in done.stdout
    assert "shiny_helper" in done.stdout
    assert "full check on each" not in done.stdout


def test_finish_with_the_main_tree_detached_judges_no_folds(repo, worktree):
    git(repo, "checkout", "-q", "--detach")
    build_state(worktree, "start")
    commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "Refine a")

    done = build_state(worktree, "finish")

    assert "build-state fold" not in done.stdout
    assert "Traceback" not in done.stderr


def test_fold_refuses_fixups_it_would_need_folded_first(worktree):
    added = commit(worktree, "a.py", "Add a")
    commit(worktree, "a.py", "fix a", f"--fixup={added}")
    commit(worktree, "a.py", "Refine a")

    done = build_state(worktree, "fold")

    assert done.returncode == 1 and "fixup!" in done.stderr
    assert "build-state finish" in done.stderr
