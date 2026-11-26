"""bin/mutants-base against a throwaway repo, with gh answered by a fake on PATH.

filedrawer keeps a copy of bin/mutants-base; these tests cover both repos' rules, and
the last ones run this repo's workflow step as GitHub would."""

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "bin" / "mutants-base"
WORKFLOW = ".github/workflows/mutants.yml"

# A fake gh: notes each call's arguments in $FAKE_GH_LOG, then answers by the API path
# from $FAKE_GH_ANSWERS, whatever the query: the runs' list, one object a line as
# `--jq .workflow_runs[]` prints them, or each run's artifacts. $FAKE_GH_FAIL fails it
# as gh does on an API error, and $FAKE_GH_SLEEP makes it hang first.
FAKE_GH = """#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
time.sleep(float(os.environ.get("FAKE_GH_SLEEP") or 0))
if os.environ.get("FAKE_GH_FAIL"):
    print("HTTP 403: Resource not accessible by integration", file=sys.stderr)
    sys.exit(1)
answers = json.load(open(os.environ["FAKE_GH_ANSWERS"]))
path = next(arg for arg in args if arg.startswith("repos/")).split("?")[0]
if found := re.fullmatch(r"repos/o/r/actions/workflows/([^/]+)/runs", path):
    listed = answers["runs"].get(found[1], [])
elif found := re.fullmatch(r"repos/o/r/actions/runs/(\\d+)/artifacts", path):
    listed = answers["artifacts"].get(found[1], [])
else:
    print(f"HTTP 404: {path}", file=sys.stderr)
    sys.exit(1)
for item in listed:
    print(json.dumps(item))
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


class Fake:
    """The scratch repo, and the runs and artifacts its fake gh answers with."""

    def __init__(self, root: Path, repo: Path):
        self.root = root
        self.repo = repo
        self.runs: list[dict] = []
        self.artifacts: dict[str, list[dict]] = {}
        fake = root / "bin"
        fake.mkdir()
        (fake / "gh").write_text(FAKE_GH)
        (fake / "gh").chmod(0o755)
        self.env = os.environ | {
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(root / "answers.json"),
            "FAKE_GH_LOG": str(root / "gh.log"),
        }
        self.env.pop("GITHUB_REPOSITORY", None)

    def sha(self, rev: str) -> str:
        return git(self.repo, "rev-parse", rev)

    def add_run(
        self,
        rev: str,
        *artifacts: str,
        event: str = "schedule",
        branch: str = "main",
        conclusion: str = "success",
    ) -> None:
        """A run at `rev`, newer than those added before it, holding `artifacts`,
        `name!` for an expired one."""
        run_id = 100 + len(self.runs)
        head = rev if re.fullmatch(r"[0-9a-f]{40}", rev) else self.sha(rev)
        self.runs.insert(
            0,
            {
                "id": run_id,
                "event": event,
                "head_sha": head,
                "head_branch": branch,
                "conclusion": conclusion,
                "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
            },
        )
        self.artifacts[str(run_id)] = [
            {"name": name.rstrip("!"), "expired": name.endswith("!")}
            for name in artifacts
        ]

    def write_answers(self, workflow: str = "mutants.yml") -> None:
        (self.root / "answers.json").write_text(
            json.dumps({"runs": {workflow: self.runs}, "artifacts": self.artifacts})
        )

    def base(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.write_answers()
        return subprocess.run(
            [str(SCRIPT), *(args or ("--repo", "o/r", "--retention-days", "90"))],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
            env=self.env | env,
        )

    def calls(self) -> list[list[str]]:
        log = self.root / "gh.log"
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def fake(template, tmp_path):
    shutil.copytree(template, tmp_path / "repo", symlinks=True)
    return Fake(tmp_path, tmp_path / "repo")


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


def output(done):
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_the_base_is_the_newest_covered_runs_head(fake):
    fake.add_run("HEAD~2", "mutants", "mutants-covered")
    fake.add_run("HEAD~1", "mutants", "mutants-covered")

    done = fake.base()

    assert done.returncode == 0, done.stderr
    assert output(done) == {"base": fake.sha("HEAD~1"), "skip": "false"}
    assert "actions/runs/101" in done.stderr


@pytest.mark.parametrize(
    "newer",
    [
        {"artifacts": ("mutants",)},
        {"artifacts": ("mutants-covered!",)},
        {"event": "push"},
        {"branch": "topic"},
        {"conclusion": "failure"},
    ],
    ids=["not-covered", "expired", "push", "other-branch", "failed"],
)
def test_a_run_that_cant_show_it_covered_the_branch_is_passed_over(fake, newer):
    fake.add_run("HEAD~2", "mutants-covered", event="workflow_dispatch")
    artifacts = newer.pop("artifacts", ("mutants-covered",))
    fake.add_run("HEAD", *artifacts, **newer)

    assert output(fake.base())["base"] == fake.sha("HEAD~2")


def test_a_run_that_tested_no_mutants_but_covered_its_range_counts(fake):
    # filedrawer's daily run with no mutants in its lines uploads only the marker.
    fake.add_run("HEAD~1", "mutants-covered")

    assert output(fake.base())["base"] == fake.sha("HEAD~1")


def test_a_run_whose_head_left_the_history_is_passed_over(fake):
    gone = git(fake.repo, "commit-tree", "-m", "rewritten away", "HEAD^{tree}")
    fake.add_run(gone, "mutants-covered")
    fake.add_run("f" * 40, "mutants-covered")

    done = fake.base()

    assert done.returncode == 0, done.stderr
    assert output(done)["base"] == fake.sha("HEAD~2")


def test_with_no_covered_run_it_is_the_commit_that_added_the_workflow(fake):
    done = fake.base()

    assert done.returncode == 0, done.stderr
    assert output(done)["base"] == fake.sha("HEAD~2")
    assert "added .github/workflows/mutants.yml" in done.stderr
    assert "last 91 days" in done.stderr


def test_a_covered_run_at_head_means_skip(fake):
    fake.add_run("HEAD", "mutants-covered")

    assert output(fake.base()) == {"base": fake.sha("HEAD"), "skip": "true"}


@pytest.mark.parametrize("days", [30, 90])
def test_it_asks_only_for_runs_the_artifact_can_outlast(fake, days):
    fake.base("--repo", "o/r", "--retention-days", str(days))

    [runs_call] = fake.calls()
    assert "repos/o/r/actions/workflows/mutants.yml/runs" in runs_call
    assert {"branch=main", "status=success"} <= set(runs_call)
    [created] = [arg for arg in runs_call if arg.startswith("created=>=")]
    since = datetime.strptime(created, "created=>=%Y-%m-%d").replace(tzinfo=UTC)
    assert timedelta(days=days) < datetime.now(UTC) - since < timedelta(days=days + 2)


def test_runs_outside_history_cost_no_artifact_call(fake):
    fake.add_run("f" * 40, "mutants-covered")

    fake.base()

    assert not any("/artifacts" in arg for call in fake.calls() for arg in call)


def test_an_override_is_taken_as_given(fake):
    done = fake.base(
        "--repo", "o/r", "--retention-days", "90", "--override", "HEAD~3",
        FAKE_GH_FAIL="1",
    )  # fmt: skip

    assert output(done) == {"base": fake.sha("HEAD~3"), "skip": "false"}


def test_a_gh_error_fails_rather_than_guessing(fake):
    done = fake.base(FAKE_GH_FAIL="1")

    assert done.returncode == 1
    assert done.stdout == ""
    assert "403" in done.stderr and "Traceback" not in done.stderr


def test_a_gh_call_that_hangs_fails_in_time(fake):
    done = fake.base(FAKE_GH_SLEEP="30", MUTANTS_BASE_GH_SECONDS="1")

    assert done.returncode == 1
    assert "took past 1 seconds" in done.stderr


def test_a_workflow_no_commit_added_fails(fake):
    fake.write_answers("other.yml")

    done = subprocess.run(
        [str(SCRIPT), "--repo", "o/r", "--retention-days", "90"]
        + ["--workflow", "other.yml"],
        cwd=fake.repo,
        capture_output=True,
        text=True,
        check=False,
        env=fake.env,
    )

    assert done.returncode == 1
    assert "no commit in HEAD's history adds .github/workflows/other.yml" in done.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--retention-days", "90"],
        ["--repo", "o/r"],
        ["--repo", "o/r", "--retention-days", "0"],
    ],
    ids=["no-repo", "no-retention", "zero-days"],
)
def test_a_missing_option_is_a_usage_error(fake, args):
    done = fake.base(*args)

    assert done.returncode == 2
    assert "usage" in done.stderr
    assert not (fake.root / "gh.log").exists()


# The workflow's own step, run as GitHub runs a `run:` with no shell named (bash -e),
# so a call that no longer fits the interface fails here rather than in the next
# scheduled run.


def workflow_step(name: str) -> str:
    """The one-line `run:` of the workflow step called `name`."""
    lines = (ROOT / WORKFLOW).read_text().splitlines()
    [start] = [i for i, line in enumerate(lines) if line.strip() == f"- name: {name}"]
    indent = lines[start].index("-")
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        if found := re.fullmatch(r"\s+run: ([^|>].*)", line):
            return found[1]
    raise AssertionError(f"{WORKFLOW}'s step {name!r} has no one-line run")


def covered_retention_days() -> int:
    """The retention-days of the workflow's mutants-covered upload."""
    text = (ROOT / WORKFLOW).read_text()
    found = re.search(
        r"name: mutants-covered\n(?:\s+\w.*\n)*?\s+retention-days: (\d+)", text
    )
    assert found, f"{WORKFLOW} uploads no mutants-covered with a retention-days"
    return int(found[1])


def run_step(fake, **env):
    """Run the workflow's Find the base step in the scratch repo, with this
    repo's bin/mutants-base where the step looks for it; return its outputs."""
    (fake.repo / "bin").mkdir()
    shutil.copy2(SCRIPT, fake.repo / "bin" / "mutants-base")
    fake.write_answers()
    outputs = fake.root / "github_output"
    outputs.write_text("")
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", workflow_step("Find the base")],
        cwd=fake.repo,
        capture_output=True,
        text=True,
        check=False,
        env=fake.env
        | {"GITHUB_REPOSITORY": "o/r", "GITHUB_OUTPUT": str(outputs), "GH_TOKEN": "x"}
        | env,
    )
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in outputs.read_text().splitlines())


def test_the_workflows_scheduled_step_finds_the_base(fake):
    fake.add_run("HEAD~1", "mutants", "mutants-covered")

    assert run_step(fake, OVERRIDE="") == {"base": fake.sha("HEAD~1"), "skip": "false"}
    [runs_call] = [
        call for call in fake.calls() if any("/workflows/" in a for a in call)
    ]
    [created] = [arg for arg in runs_call if arg.startswith("created=>=")]
    since = datetime.strptime(created, "created=>=%Y-%m-%d").replace(tzinfo=UTC)
    days = covered_retention_days()
    assert timedelta(days=days) < datetime.now(UTC) - since < timedelta(days=days + 2)


def test_the_workflows_dispatched_step_takes_its_base_input(fake):
    assert run_step(fake, OVERRIDE="HEAD~3", FAKE_GH_FAIL="1") == {
        "base": fake.sha("HEAD~3"),
        "skip": "false",
    }
