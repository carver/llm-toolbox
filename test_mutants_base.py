"""bin/mutants-base against a throwaway repo, with gh answered by a fake on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "mutants-base"
WORKFLOW = ".github/workflows/mutants.yml"

# A fake gh: answers `gh api --paginate --slurp PATH` from $FAKE_GH_ANSWERS, a JSON
# map of PATH to the list of pages; an unknown PATH or $FAKE_GH_FAIL fails like gh.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:4] != ["api", "--paginate", "--slurp"]:
    print(f"unknown command {sys.argv[1:]}", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAKE_GH_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_GH_SLEEP"]))
if os.environ.get("FAKE_GH_FAIL"):
    print("HTTP 403: Resource not accessible by integration", file=sys.stderr)
    sys.exit(1)
answers = json.load(open(os.environ["FAKE_GH_ANSWERS"]))
path = sys.argv[-1]
if path not in answers:
    print(f"HTTP 404: {path}", file=sys.stderr)
    sys.exit(1)
print(json.dumps(answers[path]))
"""


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A repo whose second commit adds the workflow, then two more commits."""
    repo = tmp_path_factory.mktemp("template")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "README", "start")
    commit(repo, WORKFLOW, "Add the workflow")
    commit(repo, "a.py", "Add a")
    commit(repo, "b.py", "Add b")
    return repo


@pytest.fixture
def repo(template, tmp_path):
    shutil.copytree(template, tmp_path / "repo", symlinks=True)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text(FAKE_GH)
    (fake / "gh").chmod(0o755)
    return tmp_path / "repo"


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo, path, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    with open(repo / path, "a") as file:
        file.write(f"{message}\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


RUNS = "repos/o/r/actions/workflows/mutants.yml/runs?branch=main&status=success&per_page=100"


def answer(repo, runs, artifacts):
    """gh's answers: `runs` is (id, event, head) newest first; `artifacts` maps a run
    id to its artifact names, `name!` for an expired one."""
    answers = {
        RUNS: [
            {
                "workflow_runs": [
                    {"id": run_id, "event": event, "head_sha": head}
                    for run_id, event, head in runs
                ]
            }
        ]
    }
    for run_id, names in artifacts.items():
        answers[f"repos/o/r/actions/runs/{run_id}/artifacts"] = [
            {
                "artifacts": [
                    {"name": name.rstrip("!"), "expired": name.endswith("!")}
                    for name in names
                ]
            }
        ]
    path = repo.parent / "answers.json"
    path.write_text(json.dumps(answers))
    return path


def base(repo, *args, answers=None, **env):
    done = subprocess.run(
        [str(SCRIPT), "--repo", "o/r", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ
        | {
            "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(answers or answer(repo, [], {})),
            **env,
        },
    )
    return done


def output(done):
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_the_base_is_the_newest_complete_runs_head(repo):
    newer, older = git(repo, "rev-parse", "HEAD~1"), git(repo, "rev-parse", "HEAD~2")
    answers = answer(
        repo,
        [(3, "schedule", newer), (2, "schedule", older)],
        {3: ["mutants", "mutants-covered"], 2: ["mutants", "mutants-covered"]},
    )

    done = base(repo, answers=answers)

    assert done.returncode == 0, done.stderr
    assert output(done) == {"base": newer, "skip": "false"}


def test_a_partial_run_an_expired_marker_and_a_push_run_are_passed_over(repo):
    covered = git(repo, "rev-parse", "HEAD~2")
    answers = answer(
        repo,
        [
            (5, "schedule", git(repo, "rev-parse", "HEAD")),
            (4, "push", git(repo, "rev-parse", "HEAD")),
            (3, "workflow_dispatch", git(repo, "rev-parse", "HEAD~1")),
            (2, "workflow_dispatch", covered),
        ],
        {
            5: ["mutants"],
            4: ["mutants-covered"],
            3: ["mutants-covered!"],
            2: ["mutants-covered"],
        },
    )

    assert output(base(repo, answers=answers))["base"] == covered


def test_a_run_whose_head_left_the_history_is_passed_over(repo):
    gone = git(repo, "commit-tree", "-m", "rewritten away", "HEAD^{tree}")
    answers = answer(repo, [(2, "schedule", gone)], {2: ["mutants-covered"]})

    done = base(repo, answers=answers)

    assert done.returncode == 0, done.stderr
    assert output(done)["base"] == git(repo, "rev-parse", "HEAD~2")


def test_with_no_complete_run_it_is_the_commit_that_added_the_workflow(repo):
    done = base(repo)

    assert done.returncode == 0, done.stderr
    assert output(done)["base"] == git(repo, "rev-parse", "HEAD~2")


def test_a_complete_run_at_head_means_skip(repo):
    head = git(repo, "rev-parse", "HEAD")
    answers = answer(repo, [(2, "schedule", head)], {2: ["mutants-covered"]})

    assert output(base(repo, answers=answers)) == {"base": head, "skip": "true"}


def test_an_override_is_taken_as_given(repo):
    first = git(repo, "rev-parse", "HEAD~3")

    done = base(repo, "--override", "HEAD~3", FAKE_GH_FAIL="1")

    assert output(done) == {"base": first, "skip": "false"}


def test_a_gh_error_fails_rather_than_guessing(repo):
    done = base(repo, FAKE_GH_FAIL="1")

    assert done.returncode == 1
    assert done.stdout == ""
    assert "403" in done.stderr and "Traceback" not in done.stderr


def test_a_gh_call_that_hangs_fails_in_time(repo):
    done = base(repo, FAKE_GH_SLEEP="30", MUTANTS_BASE_GH_SECONDS="1")

    assert done.returncode == 1
    assert "took past 1 seconds" in done.stderr


def test_a_workflow_no_commit_added_fails(repo):
    answers = repo.parent / "other.json"
    runs = RUNS.replace("mutants.yml", "other.yml")
    answers.write_text(json.dumps({runs: [{"workflow_runs": []}]}))

    done = base(repo, "--workflow", "other.yml", answers=answers)

    assert done.returncode == 1
    assert "no commit in HEAD's history adds .github/workflows/other.yml" in done.stderr


def test_without_a_repo_it_is_a_usage_error(repo):
    done = subprocess.run(
        [str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"},
    )

    assert done.returncode == 2


def test_in_a_github_job_the_repo_comes_from_its_environment(repo):
    done = subprocess.run(
        [str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ
        | {
            "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(answer(repo, [], {})),
            "GITHUB_REPOSITORY": "o/r",
        },
    )

    assert done.returncode == 0, done.stderr
    assert output(done)["base"] == git(repo, "rev-parse", "HEAD~2")
