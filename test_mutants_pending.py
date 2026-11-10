"""bin/mutants-pending against throwaway repos, with gh answered by a fake on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "mutants-pending"
REPO = "o/r"
RUNS = f"repos/{REPO}/actions/workflows/mutants.yml/runs?branch=main&status=completed&per_page=50"
COMMENTS = f"repos/{REPO}/issues/7/comments"
ISSUES = f"repos/{REPO}/issues?state=open&per_page=100"
WORKFLOW = f"repos/{REPO}/actions/workflows/mutants.yml"

# A fake gh: answers `gh api [--paginate --slurp] PATH` from $FAKE_GH_ANSWERS, a JSON
# map of PATH to its answer; logs each PATH to $FAKE_GH_LOG; an unknown PATH or
# $FAKE_GH_FAIL fails like gh, and $FAKE_GH_SLEEP stalls it.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys, time
if sys.argv[1] != "api":
    sys.exit(f"unknown command {sys.argv[1:]}")
time.sleep(float(os.environ.get("FAKE_GH_SLEEP", "0")))
path = sys.argv[-1]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(path + "\\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.exit("HTTP 403: Resource not accessible by integration")
answers = json.load(open(os.environ["FAKE_GH_ANSWERS"]))
if path not in answers:
    sys.exit(f"HTTP 404: {path}")
print(json.dumps(answers[path]))
"""

HEADER = "# Mutation testing, daily.\n#\n# Tracking issue: #7\nname: Mutants\n"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    repo = tmp_path_factory.mktemp("template")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/mutants.yml").write_text(HEADER)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Add the workflow")
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


def run_url(run_id):
    return f"https://github.com/{REPO}/actions/runs/{run_id}"


def a_run(run_id, conclusion="success", event="schedule", day="2026-11-10"):
    return {
        "id": run_id,
        "event": event,
        "conclusion": conclusion,
        "created_at": f"{day}T09:17:00Z",
        "html_url": run_url(run_id),
    }


def a_comment(body, association="OWNER"):
    return {"body": body, "author_association": association}


def answers(runs=(), artifacts=None, comments=(), issues=(), state="active", **extra):
    """gh's answers. `artifacts` maps a run id to its artifact names (`name!` for an
    expired one); a run left out has the `mutants` artifact."""
    artifacts = artifacts or {}
    found = {
        RUNS: {"workflow_runs": list(runs)},
        COMMENTS: [list(comments)],
        ISSUES: [list(issues)],
        WORKFLOW: {"state": state},
    }
    for run in runs:
        names = artifacts.get(run["id"], ["mutants"])
        found[f"repos/{REPO}/actions/runs/{run['id']}/artifacts"] = {
            "artifacts": [
                {"name": name.rstrip("!"), "expired": name.endswith("!")}
                for name in names
            ]
        }
    return found | extra


def pending(repo, found, *args, now="2026-11-11T00:00:00Z", **env):
    path = repo.parent / "answers.json"
    path.write_text(json.dumps(found))
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ
        | {
            "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(path),
            "FAKE_GH_LOG": str(repo.parent / "gh.log"),
            "MUTANTS_PENDING_NOW": now,
            **env,
        },
    )


def test_runs_after_the_last_triaged_one_are_pending(repo):
    found = answers(
        runs=[a_run(30), a_run(20), a_run(10)],
        comments=[a_comment(f"Counts...\nTriaged run: {run_url(20)}\nNone filed.")],
    )

    done = pending(repo, found)

    assert done.returncode == 0, done.stderr
    assert run_url(30) in done.stdout
    assert run_url(20) not in done.stdout and run_url(10) not in done.stdout
    assert "tracking issue #7" in done.stdout


def test_a_triage_line_from_a_stranger_counts_for_nothing(repo):
    found = answers(
        runs=[a_run(30)],
        comments=[a_comment(f"Triaged run: {run_url(30)}", "NONE")],
    )

    assert run_url(30) in pending(repo, found).stdout


def test_a_run_with_no_results_is_not_pending(repo):
    found = answers(runs=[a_run(30)], artifacts={30: []})

    done = pending(repo, found)

    assert done.returncode == 0, done.stderr
    assert "nothing pending" in done.stdout


def test_a_run_whose_results_expired_untriaged_is_listed_as_expired(repo):
    found = answers(runs=[a_run(20)], artifacts={20: ["mutants!"]})

    done = pending(repo, found)

    assert f"{run_url(20)} (its results expired untriaged)" in done.stdout


def test_open_tests_issues_from_collaborators_are_listed(repo):
    found = answers(
        issues=[
            {
                "number": 4,
                "title": "Tests: the queue's order",
                "author_association": "OWNER",
            },
            {
                "number": 5,
                "title": "Tests: from a stranger",
                "author_association": "NONE",
            },
            {"number": 6, "title": "Something else", "author_association": "OWNER"},
            {
                "number": 8,
                "title": "Tests: a PR",
                "author_association": "OWNER",
                "pull_request": {},
            },
        ]
    )

    done = pending(repo, found)

    assert "#4 Tests: the queue's order" in done.stdout
    assert "stranger" not in done.stdout and "#6" not in done.stdout
    assert "#8" not in done.stdout


def test_a_disabled_schedule_is_named_with_its_last_runs_age(repo):
    found = answers(
        runs=[a_run(20, day="2026-11-10")],
        comments=[a_comment(f"Triaged run: {run_url(20)}")],
        state="disabled_inactivity",
    )

    done = pending(repo, found)

    assert done.returncode == 0, done.stderr
    assert "disabled_inactivity" in done.stdout
    assert "last run 0 days ago" in done.stdout


def test_a_last_run_days_old_is_named(repo):
    found = answers(
        runs=[a_run(20, day="2026-11-05")],
        comments=[a_comment(f"Triaged run: {run_url(20)}")],
    )

    assert "last run 5 days ago" in pending(repo, found).stdout


def test_a_repo_without_the_workflow_prints_nothing_and_asks_nothing(repo):
    git(repo, "rm", "-q", ".github/workflows/mutants.yml")
    git(repo, "commit", "-q", "-m", "Drop it")

    done = pending(repo, answers())

    assert done.returncode == 0 and done.stdout == ""
    assert not (repo.parent / "gh.log").exists()


def test_several_repos_are_each_reported(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")

    done = pending(repo, answers(runs=[a_run(30)]), str(repo), str(other))

    assert done.returncode == 0, done.stderr
    assert run_url(30) in done.stdout


def test_a_gh_failure_says_so_in_a_line_and_fails(repo):
    done = pending(repo, answers(), FAKE_GH_FAIL="1")

    assert done.returncode == 1
    assert done.stderr.count("\n") == 1 and "403" in done.stderr


def test_a_gh_call_that_hangs_fails_in_time(repo):
    done = pending(repo, answers(), FAKE_GH_SLEEP="30", MUTANTS_PENDING_GH_SECONDS="1")

    assert done.returncode == 1
    assert "took past 1 seconds" in done.stderr


def test_a_run_by_push_or_without_the_mutants_artifact_is_not_pending(repo):
    found = answers(
        runs=[a_run(30, event="push"), a_run(20)],
        artifacts={20: ["mutants-covered"]},
    )

    assert "nothing pending" in pending(repo, found).stdout


def test_a_repo_whose_origin_isnt_on_github_prints_nothing(repo):
    git(repo, "remote", "set-url", "origin", "/srv/git/r.git")

    done = pending(repo, answers(runs=[a_run(30)]))

    assert done.returncode == 0 and done.stdout == ""


def test_help_prints_the_docstring(repo):
    done = pending(repo, answers(), "-h")

    assert done.returncode == 0 and "Tracking issue: #N" in done.stdout
