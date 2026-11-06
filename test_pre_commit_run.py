"""bin/pre-commit-run: the pre-commit framework, quiet for agents, and its installer."""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import without_repo_vars

SCRIPT = Path(__file__).resolve().parent / "bin" / "pre-commit-run"
# A stand-in for uvx: `pre-commit run` prints what the real one does on a pass, and fails
# when $PRE_COMMIT_FAILS is set; each call goes to $UVX_LOG.
FAKE_UVX = """#!/bin/sh
echo "$*" >> "$UVX_LOG"
# A formatter hook: rewrites $PRE_COMMIT_REWRITES to "tidy" when it isn't, and fails
# as pre-commit does when a hook changes files.
if [ -n "${PRE_COMMIT_REWRITES:-}" ] && [ "$(cat "$PRE_COMMIT_REWRITES")" != tidy ]; then
  echo tidy > "$PRE_COMMIT_REWRITES"
  echo "ruff format..............................................................Failed"
  echo "- hook id: ruff-format"
  echo "- files were modified by this hook"
  exit 1
fi
echo "ruff format..............................................................Passed"
if [ -n "${PRE_COMMIT_FAILS:-}" ]; then echo "a.py:1:1: F401 unused import"; exit 1; fi
exit 0
"""


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=without_repo_vars(os.environ),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", str(repo))
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    git(repo, "add", ".pre-commit-config.yaml")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uvx").write_text(FAKE_UVX)
    (fake / "uvx").chmod(0o755)
    return repo


def env_for(repo, agent=False, **extra):
    """The environment with the stand-in uvx first on PATH, as a person (or an agent)."""
    environ = without_repo_vars(os.environ)
    environ.pop("AI_AGENT", None)
    environ["PATH"] = f"{repo.parent / 'fakebin'}{os.pathsep}{environ['PATH']}"
    environ["UVX_LOG"] = str(repo.parent / "uvx.log")
    if agent:
        environ["AI_AGENT"] = "test-agent"
    return environ | extra


def run(repo, *args, agent=False, **env):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env_for(repo, agent, **env),
    )


def uvx_calls(repo):
    log = repo.parent / "uvx.log"
    return log.read_text().splitlines() if log.exists() else []


def last_line(done):
    return done.stderr.rstrip().splitlines()[-1]


def test_a_person_sees_pre_commits_output_as_it_runs(repo):
    done = run(repo)

    assert done.returncode == 0, done.stderr
    assert "Passed" in done.stdout + done.stderr


def test_an_agents_pass_prints_nothing(repo):
    done = run(repo, agent=True)

    assert done.returncode == 0, done.stderr
    assert done.stdout + done.stderr == ""
    assert uvx_calls(repo) == ["pre-commit@4.7.0 run"]


@pytest.mark.parametrize("agent", [True, False])
def test_a_failure_shows_the_output_and_ends_with_commit_refused(repo, agent):
    done = run(repo, agent=agent, PRE_COMMIT_FAILS="1")

    assert done.returncode == 1
    assert "F401" in done.stdout + done.stderr
    assert last_line(done).startswith("COMMIT REFUSED")


def test_an_unstaged_config_is_refused_before_pre_commit_runs(repo):
    (repo / ".pre-commit-config.yaml").write_text("repos: [changed]\n")

    done = run(repo, agent=True)

    assert done.returncode == 1
    assert ".pre-commit-config.yaml" in done.stderr and "stage" in done.stderr
    assert last_line(done).startswith("COMMIT REFUSED")
    assert uvx_calls(repo) == []


def test_without_a_config_it_does_nothing(repo):
    git(repo, "rm", "-q", "--cached", ".pre-commit-config.yaml")
    (repo / ".pre-commit-config.yaml").unlink()

    done = run(repo, agent=True)

    assert done.returncode == 0 and done.stdout + done.stderr == ""
    assert uvx_calls(repo) == []


def test_the_version_can_be_chosen(repo):
    run(repo, agent=True, PRE_COMMIT_VERSION="4.8.0")

    assert uvx_calls(repo) == ["pre-commit@4.8.0 run"]


def hook_of(repo):
    return Path(
        repo, git(repo, "rev-parse", "--git-path", "hooks").strip(), "pre-commit"
    )


def test_install_copies_the_script_and_creates_the_hook(repo):
    done = run(repo, "--install", str(repo))

    assert done.returncode == 0, done.stderr
    assert (repo / "bin" / "pre-commit-run").read_bytes() == SCRIPT.read_bytes()
    assert os.access(repo / "bin" / "pre-commit-run", os.X_OK)
    hook = hook_of(repo)
    assert os.access(hook, os.X_OK)
    assert hook.read_text().startswith("#!/bin/sh\n")
    assert "bin/pre-commit-run" in hook.read_text()


def test_install_adds_the_line_after_an_existing_hooks_shebang_once(repo):
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    hook.chmod(0o755)

    run(repo, "--install", str(repo))
    run(repo, "--install", str(repo))

    lines = hook.read_text().splitlines()
    assert lines[0] == "#!/bin/sh" and lines[-1] == "echo mine"
    assert sum("bin/pre-commit-run" in line for line in lines) == 1


def test_install_refuses_a_hook_that_isnt_a_shell_script(repo):
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env python3\nprint('mine')\n")

    done = run(repo, "--install", str(repo))

    assert done.returncode == 1 and "shell" in done.stderr
    assert hook.read_text() == "#!/usr/bin/env python3\nprint('mine')\n"


def test_an_installed_hook_refuses_a_failing_commit(repo):
    run(repo, "--install", str(repo))
    (repo / "a.py").write_text("import os\n")
    git(repo, "add", "a.py")
    environ = env_for(
        repo,
        PRE_COMMIT_FAILS="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
    )

    done = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "x"],
        capture_output=True,
        text=True,
        check=False,
        env=environ,
    )

    assert done.returncode != 0 and "COMMIT REFUSED" in done.stderr


def test_help_prints_the_usage(repo):
    done = run(repo, "-h")

    assert done.returncode == 0 and "--install REPO" in done.stdout


@pytest.mark.parametrize(
    "old",
    [
        "uvx pre-commit@4.7.0 run || exit",
        "pre-commit run",
        "  uvx pre-commit run --all-files",
    ],
)
def test_install_replaces_a_hooks_own_pre_commit_run(repo, old):
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/usr/bin/env bash\n# mine\n{old}\necho after\n")

    done = run(repo, "--install", str(repo))

    assert done.returncode == 0, done.stderr
    lines = hook.read_text().splitlines()
    assert lines[:2] == ["#!/usr/bin/env bash", "# mine"] and lines[-1] == "echo after"
    assert sum("bin/pre-commit-run" in line for line in lines) == 1
    assert old.strip() not in hook.read_text()


GENERATED = """#!/usr/bin/env bash
# File generated by pre-commit: https://pre-commit.com
# ID: 138fd403232d2ddd5efb44317e38bf03
ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)
HERE="$(cd "$(dirname "$0")" && pwd)"
ARGS+=(--hook-dir "$HERE" -- "$@")
if [ -x "$INSTALL_PYTHON" ]; then
    exec "$INSTALL_PYTHON" -mpre_commit "${ARGS[@]}"
elif command -v pre-commit > /dev/null; then
    exec pre-commit "${ARGS[@]}"
fi
"""


def test_install_replaces_a_hook_pre_commit_install_wrote(repo):
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(GENERATED)

    done = run(repo, "--install", str(repo))

    assert done.returncode == 0, done.stderr
    assert (
        "pre_commit" not in hook.read_text()
        and "exec pre-commit" not in hook.read_text()
    )
    assert "bin/pre-commit-run" in hook.read_text()


def test_install_refuses_a_run_line_continued_onto_the_next(repo):
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    text = "#!/bin/sh\nuvx pre-commit run \\\n    --all-files\n"
    hook.write_text(text)

    done = run(repo, "--install", str(repo))

    assert done.returncode == 1 and "by hand" in done.stderr
    assert hook.read_text() == text


def test_a_second_install_leaves_the_copy_alone(repo):
    run(repo, "--install", str(repo))

    again = run(repo, "--install", str(repo))

    assert "copied" not in again.stdout


def test_a_persons_run_streams_pre_commits_own_output(repo):
    done = run(repo)

    assert "Passed" in done.stdout  # pre-commit's own stdout, not replayed on stderr


# A formatter hook that rewrites files: an agent's fully staged files are restaged.


def staged_ugly(repo, name="a.py"):
    (repo / name).write_text("ugly\n")
    git(repo, "add", "--", f":(literal){name}")
    return repo / name


def test_an_agents_staged_file_a_hook_rewrote_is_restaged_and_rechecked(repo):
    target = staged_ugly(repo)
    done = run(repo, agent=True, PRE_COMMIT_REWRITES=str(target))
    assert done.returncode == 0, done.stderr
    assert git(repo, "show", ":a.py") == "tidy\n"
    assert "a.py" in done.stderr and "staged" in done.stderr
    assert len(uvx_calls(repo)) == 2


def test_a_rewritten_file_that_had_unstaged_changes_is_refused(repo):
    target = staged_ugly(repo)
    target.write_text("ugly, and more not for this commit\n")
    done = run(repo, agent=True, PRE_COMMIT_REWRITES=str(target))
    assert done.returncode == 1
    assert last_line(done).startswith("COMMIT REFUSED")
    assert git(repo, "show", ":a.py") == "ugly\n"


def test_a_rewritten_file_that_isnt_staged_is_refused(repo):
    staged_ugly(repo)
    other = repo / "b.py"
    other.write_text("ugly\n")
    git(repo, "add", "b.py")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    done = run(repo, agent=True, PRE_COMMIT_REWRITES=str(other))
    assert done.returncode == 1
    assert "b.py" not in git(repo, "diff", "--cached", "--name-only")


def test_a_persons_rewritten_file_is_left_for_them_to_stage(repo):
    target = staged_ugly(repo)
    done = run(repo, PRE_COMMIT_REWRITES=str(target))
    assert done.returncode == 1
    assert git(repo, "show", ":a.py") == "ugly\n"


def test_another_failure_after_restaging_is_refused(repo):
    target = staged_ugly(repo)
    done = run(repo, agent=True, PRE_COMMIT_REWRITES=str(target), PRE_COMMIT_FAILS="1")
    assert done.returncode == 1
    assert "F401" in done.stderr
    assert len(uvx_calls(repo)) == 2


def test_a_name_with_glob_characters_stages_only_itself(repo):
    left_out = repo / "a1.py"
    left_out.write_text("committed\n")
    git(repo, "add", "a1.py")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a1")
    left_out.write_text("not for this commit\n")
    target = staged_ugly(repo, "a[1].py")
    done = run(repo, agent=True, PRE_COMMIT_REWRITES=str(target))
    assert done.returncode == 0, done.stderr
    assert git(repo, "show", ":a[1].py") == "tidy\n"
    assert git(repo, "show", ":a1.py") == "committed\n"


def test_a_commit_of_named_paths_is_refused_not_restaged(repo):
    # `git commit PATH` commits from a temporary next-index; staging there would
    # leave the real index unformatted, so the next commit would undo it.
    target = staged_ugly(repo)
    next_index = repo / ".git" / "next-index-1234.lock"
    next_index.write_bytes((repo / ".git" / "index").read_bytes())
    done = run(
        repo,
        agent=True,
        PRE_COMMIT_REWRITES=str(target),
        GIT_INDEX_FILE=str(next_index),
    )
    assert done.returncode == 1
    assert len(uvx_calls(repo)) == 1


def test_a_rewrite_alongside_a_file_with_unstaged_changes_leaves_the_index_alone(repo):
    # A fake hook that rewrites both a fully staged file and a partly staged one.
    staged_ugly(repo, "a.py")
    partly = staged_ugly(repo, "b.py")
    partly.write_text("ugly, and more\n")
    hook = repo.parent / "fakebin" / "uvx"
    hook.write_text(
        '#!/bin/sh\necho "$*" >> "$UVX_LOG"\n'
        'if [ "$(cat a.py)" != tidy ]; then echo tidy > a.py; echo tidy > b.py;'
        ' echo "- files were modified by this hook"; exit 1; fi\nexit 0\n'
    )
    done = run(repo, agent=True)
    assert done.returncode == 1
    assert git(repo, "show", ":a.py") == "ugly\n", "nothing was restaged"
    assert len(uvx_calls(repo)) == 1


def test_a_hook_that_rewrites_an_untracked_file_is_refused(repo):
    # A formatter that runs over the whole tree, as cargo fmt does, can rewrite a file
    # git doesn't track; pre-commit doesn't stash those, and nothing may change them.
    target = staged_ugly(repo)
    untracked = repo / "notes.py"
    untracked.write_text("mine\n")
    hook = repo.parent / "fakebin" / "uvx"
    hook.write_text(
        '#!/bin/sh\necho "$*" >> "$UVX_LOG"\n'
        f'if [ "$(cat {target})" != tidy ]; then echo tidy > {target}; echo tidy > {untracked};'
        ' echo "- files were modified by this hook"; exit 1; fi\nexit 0\n'
    )
    done = run(repo, agent=True)
    assert done.returncode == 1
    assert git(repo, "show", ":a.py") == "ugly\n", "nothing was restaged"
    assert len(uvx_calls(repo)) == 1
