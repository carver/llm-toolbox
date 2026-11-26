"""bin/gh-ci-wait: wait for a PR's or commit's runs and say what failed, through a fake gh."""

import json
import os
import shutil
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from conftest import wait_until

SCRIPT = Path(__file__).resolve().parent / "bin" / "gh-ci-wait"
REPO = "acme/widgets"
API = f"repos/{REPO}"
URL = f"https://github.com/{REPO}/actions/runs"

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # A gh that logs each call and answers it from the next step of its key's script,
    # <key>.json in its folder, a list of steps, the last one repeating: {"out": TEXT},
    # {"err": TEXT} (exit 1), or {"sleep": SECONDS} before either. No script: a 404.
    import json, os, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    with open(os.path.join(here, "calls.log"), "a") as log:
        log.write(" ".join(args) + "\\n")
    if args[0] == "api":
        key = "runs"
    elif args[:2] == ["pr", "view"]:
        key = "mergeable" if "mergeable" in args else "pr"
    elif args[:2] == ["run", "view"]:
        key = ("log-" if "--log-failed" in args else "jobs-") + args[2]
    else:
        sys.exit(f"fake gh: no such call {args}")
    script = os.path.join(here, key + ".json")
    if not os.path.exists(script):
        sys.exit("gh: Not Found (HTTP 404)")
    steps = json.load(open(script))
    count = os.path.join(here, key + ".n")
    n = int(open(count).read()) if os.path.exists(count) else 0
    open(count, "w").write(str(n + 1))
    step = steps[min(n, len(steps) - 1)]
    time.sleep(step.get("sleep", 0))
    if "err" in step:
        sys.exit(step["err"])
    sys.stdout.write(step["out"])
    """
)

FAST = {
    "GH_CI_WAIT_QUEUE_SECONDS": "1",
    "GH_CI_WAIT_POLL_SECONDS": "0.02",
    "GH_CI_WAIT_SETTLE_SECONDS": "0",
    "GH_CI_WAIT_CALL_SECONDS": "5",
    "GH_CI_WAIT_GIVE_UP_SECONDS": "1",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, *paths: str) -> str:
    for path in paths:
        file = repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"{path} at {len(git(repo, 'log', '--oneline', '--all'))}\n")
        git(repo, "add", path)
    git(repo, "commit", "-qm", f"change {' '.join(paths)}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """A repo whose origin is on GitHub, with origin/main and origin/HEAD at the base,
    and a branch on top that changed tests/test_changed.py."""
    repo = tmp_path_factory.mktemp("built") / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    base = commit(repo, "tests/test_changed.py", "tests/test_other.py")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(repo, "switch", "-qc", "topic")
    commit(repo, "tests/test_changed.py")
    return repo


def run(
    run_id,
    path="ci.yml",
    conclusion="success",
    event="push",
    status="completed",
    created="2026-11-26T10:00:00Z",
):
    return {
        "id": run_id,
        "name": path.split(".")[0],
        "path": f".github/workflows/{path}",
        "event": event,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "run_attempt": 1,
        "created_at": created,
        "html_url": f"{URL}/{run_id}",
    }


def log_line(text: str, job="check", step="Run tests") -> str:
    return f"{job}\t{step}\t2026-11-26T05:34:57.1082195Z {text}"


class GitHub:
    """A copy of the built repo, and the fake gh the tool reaches GitHub through."""

    def __init__(self, tmp_path: Path, built: Path):
        self.repo = tmp_path / "repo"
        shutil.copytree(built, self.repo, symlinks=True)
        self.fakes = tmp_path / "fakes"
        self.fakes.mkdir()
        gh = self.fakes / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.env = {
            "PATH": f"{self.fakes}:{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            **FAST,
        }
        self.head = git(self.repo, "rev-parse", "HEAD")

    def script(self, key: str, *steps: dict) -> None:
        (self.fakes / f"{key}.json").write_text(json.dumps(list(steps)))

    def polls(self, *lists) -> None:
        """The run list each poll gets, the last one repeating."""
        self.script(
            "runs", *({"out": json.dumps({"workflow_runs": runs})} for runs in lists)
        )

    def pr(self, *heads: str, base="main") -> None:
        self.script(
            "pr",
            *(
                {"out": json.dumps({"headRefOid": h, "baseRefName": base})}
                for h in heads
            ),
        )

    def jobs(self, run_id, *jobs) -> None:
        self.script(f"jobs-{run_id}", {"out": json.dumps({"jobs": list(jobs)})})

    def log(self, run_id, *lines: str) -> None:
        self.script(f"log-{run_id}", {"out": "".join(f"{x}\n" for x in lines)})

    def calls(self) -> list[str]:
        log = self.fakes / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def wait(self, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=self.env | env,
            check=False,
            timeout=60,
        )


def job(name, conclusion="failure", **steps):
    return {
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": n, "conclusion": c} for n, c in steps.items()],
    }


@pytest.fixture
def gh(tmp_path, built):
    return GitHub(tmp_path, built)


def test_head_waits_for_its_push_runs_to_appear_and_finish_and_passes(gh):
    gh.polls(
        [],
        [run(7, status="queued")],
        [run(7, status="in_progress"), run(8, "lint.yml", status="queued")],
        [run(7), run(8, "lint.yml", "skipped")],
    )
    done = gh.wait()
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == [
        f"ci.yml: success {URL}/7",
        f"lint.yml: skipped {URL}/8",
    ]
    asked = [c for c in gh.calls() if c.startswith("api")]
    assert len(asked) >= 4
    assert f"head_sha={gh.head}" in asked[0]
    assert "per_page=100" in asked[0]


def test_no_run_in_time_exits_3_naming_the_commit_and_the_ways_out(gh):
    gh.polls([run(7, event="schedule")])
    done = gh.wait()
    assert done.returncode == 3
    assert done.stdout == ""
    assert done.stderr.startswith(f"gh-ci-wait: no CI run for {gh.head[:7]} on GitHub")
    assert "check the push reached it" in done.stderr
    assert "path filters" in done.stderr
    assert "open a PR" in done.stderr


def test_the_queue_wait_is_the_setting_and_its_default_is_three_minutes(gh):
    gh.polls([])
    done = gh.wait(GH_CI_WAIT_QUEUE_SECONDS="0.3")
    assert done.returncode == 3
    assert "after 0.3 s" in done.stderr


def test_a_pr_counts_only_its_pull_request_runs_for_its_head(gh):
    gh.pr("a" * 40)
    gh.polls(
        [
            run(9, "ci.yml", "failure", event="push"),
            run(8, "ci.yml", "success", event="pull_request"),
            run(6, "nightly.yml", "failure", event="schedule"),
        ]
    )
    done = gh.wait("42")
    assert (done.returncode, done.stdout) == (0, f"ci.yml: success {URL}/8\n")
    assert f"pr view 42 -R {REPO} --json headRefOid,baseRefName" in gh.calls()
    assert any(f"head_sha={'a' * 40}" in c for c in gh.calls())


def test_a_pr_whose_head_moves_starts_the_wait_over_on_the_new_head(gh):
    old, new = "a" * 40, "b" * 40
    gh.pr(old, new)
    gh.polls(
        [run(8, event="pull_request", status="in_progress")],
        [run(9, event="pull_request")],
    )
    done = gh.wait("42")
    assert done.returncode == 0
    assert done.stdout.splitlines() == [
        f"PR #42's head moved from {old[:7]} to {new[:7]}: waiting on {new[:7]}",
        f"ci.yml: success {URL}/9",
    ]
    assert any(f"head_sha={new}" in c for c in gh.calls())


@pytest.mark.parametrize(
    "said",
    [
        (
            "GraphQL: Could not resolve to a PullRequest with the number of 42."
            " (repository.pullRequest)"
        ),
        "no pull requests found for branch 42",
    ],
)
def test_a_pr_github_doesnt_have_exits_2_at_once(gh, said):
    gh.script("pr", {"err": said})
    done = gh.wait("42", GH_CI_WAIT_GIVE_UP_SECONDS="30")
    assert done.returncode == 2
    assert done.stderr.startswith(f"gh-ci-wait: no PR #42 in {REPO}")
    assert len(gh.calls()) == 1


def test_a_pr_with_no_run_that_conflicts_with_its_base_says_so(gh):
    gh.pr("a" * 40)
    gh.polls([])
    gh.script("mergeable", {"out": json.dumps({"mergeable": "CONFLICTING"})})
    done = gh.wait("42")
    assert done.returncode == 3
    assert "conflicts with its base" in done.stderr
    assert "open a PR" not in done.stderr


def test_each_workflows_newest_run_counts_and_ignored_ones_are_left_out(gh):
    git(gh.repo, "config", "--add", "ci-status.ignore", "mutants.yml")
    gh.polls(
        [
            run(5, "ci.yml", "failure", created="2026-11-26T09:00:00Z"),
            run(6, "ci.yml", "success", created="2026-11-26T09:30:00Z"),
            run(7, "mutants.yml", "failure"),
        ]
    )
    done = gh.wait()
    assert (done.returncode, done.stdout) == (0, f"ci.yml: success {URL}/6\n")


def test_a_ruleset_workflow_and_the_repos_own_of_one_file_name_count_apart(gh):
    ruleset = run(8, "ci.yml", "failure") | {
        "path": "acme/.github/.github/workflows/ci.yml@refs/heads/main",
        "created_at": "2026-11-26T11:00:00Z",
    }
    gh.polls([run(7, "ci.yml"), ruleset])
    done = gh.wait()
    assert done.returncode == 1
    assert done.stdout.splitlines()[:2] == [
        f"ci.yml: success {URL}/7",
        f"ci.yml: failure {URL}/8",
    ]


def test_a_workflow_that_starts_late_joins_within_the_settle_window(gh):
    gh.polls(
        [run(7)],
        [run(7)],
        [run(7), run(8, "late.yml", status="queued")],
        [run(7), run(8, "late.yml", "failure")],
    )
    done = gh.wait(GH_CI_WAIT_SETTLE_SECONDS="2", GH_CI_WAIT_POLL_SECONDS="0.1")
    assert done.returncode == 1
    assert f"late.yml: failure {URL}/8" in done.stdout.splitlines()


def test_a_failed_run_names_its_failed_jobs_steps_and_tests(gh):
    gh.polls([run(7, "ci.yml", "failure"), run(8, "lint.yml")])
    gh.jobs(
        7,
        job("check", **{"Set up": "success", "Run tests": "failure"}),
        job("docs", "success", Build="success"),
        job("deploy", "skipped"),
    )
    nextest = (
        "^[[31;1m        FAIL^[[0m [   0.493s] (1041/1094) "
        "^[[35;1mserver::it^[[0m ^[[36msmoke^[[0m^[[36m::^[[0m^[[34;1mcap_spent^[[0m"
    )
    gh.log(
        7,
        "check\tRun tests\t\ufeff2026-11-26T05:34:00.0Z Current runner version",
        log_line(nextest),
        log_line("    test smoke::cap_spent ... FAILED"),
        log_line("    FAIL submit /tmp/on.md: 000"),
        log_line(
            "\x1b[31mFAILED tests/test_changed.py::test_a - ValueError: bad\x1b[0m"
        ),
        log_line("ERROR tests/test_changed.py::test_b - OSError"),
        log_line(nextest),
        log_line("##[error]Process completed with exit code 1."),
    )
    done = gh.wait()
    assert done.returncode == 1
    assert done.stdout.splitlines() == [
        f"ci.yml: failure {URL}/7",
        f"lint.yml: success {URL}/8",
        "  check: Run tests",
        "  FAIL [   0.493s] (1041/1094) server::it smoke::cap_spent",
        "  FAILED tests/test_changed.py::test_a - ValueError: bad",
        "  ERROR tests/test_changed.py::test_b - OSError",
    ]
    assert f"run view 7 -R {REPO} --json jobs" in gh.calls()
    assert f"run view 7 -R {REPO} --log-failed" in gh.calls()


def test_every_nextest_failure_kind_is_a_test_line(gh):
    gh.polls([run(7, "ci.yml", "timed_out")])
    kinds = ["TIMEOUT", "SIGSEGV", "SIGABRT", "LEAK-FAIL"]
    gh.log(7, *(log_line(f"   {k} [ 1.0s] crate::t_{k}") for k in kinds))
    done = gh.wait()
    assert done.returncode == 1
    assert done.stdout.splitlines()[1:] == [
        f"  {k} [ 1.0s] crate::t_{k}" for k in kinds
    ]


def test_failed_jobs_come_before_cancelled_ones(gh):
    # A fail-fast matrix cancels the failed job's siblings, listed before it.
    gh.polls([run(7, "ci.yml", "failure")])
    gh.jobs(
        7,
        *(job(f"matrix{i}", "cancelled", Run="cancelled") for i in range(6)),
        job("test", Run="failure"),
    )
    lines = gh.wait().stdout.splitlines()
    assert lines[1:6] == ["  test: Run", *(f"  matrix{i}: Run" for i in range(4))]


def test_a_failing_test_file_the_branch_left_alone_is_called_unchanged(gh):
    gh.polls([run(7, "ci.yml", "failure")])
    gh.log(
        7,
        log_line("FAILED tests/test_other.py::test_a - AssertionError"),
        log_line("FAILED tests/test_other.py::test_b - AssertionError"),
        log_line("FAILED ./tests/test_changed.py::test_c - AssertionError"),
    )
    done = gh.wait()
    assert done.returncode == 1
    assert done.stdout.splitlines()[1:] == [
        "  FAILED tests/test_other.py::test_a - AssertionError",
        "  FAILED tests/test_other.py::test_b - AssertionError",
        "  FAILED ./tests/test_changed.py::test_c - AssertionError",
        "  tests/test_other.py: unchanged by this branch",
    ]


def test_a_changed_file_whose_path_ends_with_the_failing_tests_counts_as_touched(gh):
    # pytest run from a subfolder names its tests from there.
    commit(gh.repo, "pkg/tests/test_deep.py")
    gh.polls([run(7, "ci.yml", "failure")])
    gh.log(
        7,
        log_line("FAILED tests/test_deep.py::t - AssertionError"),
        log_line("FAILED tests/test_other.py::t - AssertionError"),
    )
    assert gh.wait().stdout.splitlines()[1:] == [
        "  FAILED tests/test_deep.py::t - AssertionError",
        "  FAILED tests/test_other.py::t - AssertionError",
        "  tests/test_other.py: unchanged by this branch",
    ]


def test_a_pr_compares_with_its_base_branch(gh):
    # The PR's base is release, which holds the branch's commit: nothing is unchanged
    # against it, though against origin/main test_other.py would be.
    git(gh.repo, "update-ref", "refs/remotes/origin/release", gh.head)
    gh.pr(gh.head, base="release")
    gh.polls([run(7, "ci.yml", "failure", event="pull_request")])
    gh.log(7, log_line("FAILED tests/test_other.py::test_a - AssertionError"))
    done = gh.wait("42")
    assert done.returncode == 1
    assert "unchanged" not in done.stdout
    gh.pr(gh.head, base="main")
    (gh.fakes / "pr.n").unlink()
    assert "  tests/test_other.py: unchanged by this branch" in gh.wait("42").stdout


def test_no_unchanged_line_on_the_base_itself_or_when_git_cant_tell(gh):
    gh.polls([run(7, "ci.yml", "failure")])
    gh.log(7, log_line("FAILED tests/test_other.py::test_a - AssertionError"))
    base = git(gh.repo, "rev-parse", "origin/main")
    git(gh.repo, "update-ref", "refs/remotes/origin/main", gh.head)
    assert "unchanged" not in gh.wait().stdout
    git(gh.repo, "update-ref", "refs/remotes/origin/main", base)
    git(gh.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    done = gh.wait()
    assert done.returncode == 1
    assert "unchanged" not in done.stdout


def test_with_no_test_lines_the_log_before_the_failure_is_shown(gh):
    gh.polls([run(7, "ci.yml", "failure")])
    gh.log(
        7,
        log_line("one"),
        log_line("two"),
        log_line("three"),
        log_line("##[endgroup]"),
        log_line("[command]/usr/bin/git config"),
        log_line("four"),
        log_line(""),
        log_line("five"),
        log_line("six"),
        log_line("##[error]Process completed with exit code 1."),
        log_line("Cleaning up orphan processes", step="Post checkout"),
    )
    done = gh.wait()
    assert done.stdout.splitlines()[1:] == [
        "  two",
        "  three",
        "  four",
        "  five",
        "  six",
    ]


def test_with_two_failed_steps_the_lines_before_the_last_failure_are_shown(gh):
    gh.polls([run(7, "ci.yml", "failure")])
    gh.log(
        7,
        log_line("one", step="Lint"),
        log_line("##[error]Process completed with exit code 1.", step="Lint"),
        log_line("two"),
        log_line("three"),
        log_line("##[error]Process completed with exit code 1."),
        log_line("Cleaning up orphan processes", step="Post checkout"),
    )
    assert gh.wait().stdout.splitlines()[1:] == ["  one", "  two", "  three"]


def test_a_job_with_no_conclusion_yet_is_left_out(gh):
    gh.polls([run(7, "ci.yml", "failure")])
    gh.jobs(7, job("pending", None, Run=None), job("check", Run="failure"))
    assert gh.wait().stdout.splitlines()[1:] == ["  check: Run"]


def test_the_report_keeps_to_its_budget_and_says_how_to_see_the_rest(gh):
    gh.polls(
        [run(7, "ci.yml", "failure"), *(run(i, f"w{i}.yml") for i in range(10, 14))]
    )
    gh.jobs(7, *(job(f"job{i}", Run="failure") for i in range(8)))
    gh.log(7, *(log_line(f"FAILED tests/test_other.py::t{i}") for i in range(14)))
    lines = gh.wait().stdout.splitlines()
    # 5 runs, 5 of the 8 jobs, 10 of the 14 tests and the unchanged line make 21: the
    # cut keeps 19 and its own line, and drops test lines before the note.
    assert len(lines) == 20
    assert lines[5:10] == [f"  job{i}: Run" for i in range(5)]
    assert lines[10:18] == [f"  FAILED tests/test_other.py::t{i}" for i in range(8)]
    assert lines[18:] == [
        "  tests/test_other.py: unchanged by this branch",
        f"(2 more lines: gh run view 7 -R {REPO} --log-failed)",
    ]


def test_two_failed_runs_each_head_their_own_details(gh):
    gh.polls([run(7, "ci.yml", "failure"), run(8, "lint.yml", "cancelled")])
    gh.log(7, log_line("FAILED tests/test_changed.py::t"))
    gh.log(8, log_line("ruff found 2 errors"))
    assert gh.wait().stdout.splitlines() == [
        f"ci.yml: failure {URL}/7",
        f"lint.yml: cancelled {URL}/8",
        "ci.yml:",
        "  FAILED tests/test_changed.py::t",
        "lint.yml:",
        "  ruff found 2 errors",
    ]


def test_log_text_and_names_are_cut_to_plain_characters(gh):
    gh.polls([run(7, "c\u202ei$(x).yml", "failure")])
    gh.jobs(7, job("che`ck\u202e", **{"Run; rm -rf /": "failure"}))
    gh.log(
        7, log_line('FAILED tests/t.py::t - x\u202e\x07y "q" (a, b) 1+1=2 ' + "z" * 300)
    )
    lines = gh.wait().stdout.splitlines()
    assert lines[0] == f"cix.yml: failure {URL}/7"
    assert lines[1] == "  check: Run rm -rf /"
    assert lines[2].startswith('  FAILED tests/t.py::t - xy "q" (a, b) 1+1=2 zzz')
    assert len(lines[2]) == 2 + 200


def test_details_gh_cant_give_leave_their_lines_out_and_the_verdict_stands(gh):
    gh.polls([run(7, "ci.yml", "startup_failure")])
    gh.script("jobs-7", {"out": "not json"})
    done = gh.wait()
    assert (done.returncode, done.stdout) == (1, f"ci.yml: startup_failure {URL}/7\n")


@pytest.mark.parametrize(
    ("runs", "status", "verdict"),
    [
        ([run(7, conclusion="action_required")], 5, "needs approval"),
        ([run(7, status="waiting")], 5, "needs approval"),
        ([run(7, status="waiting"), run(8, "zz.yml", "failure")], 1, "needs approval"),
        ([run(7, conclusion="stale")], 1, "stale"),
        ([run(7, conclusion="neutral")], 0, "neutral"),
    ],
)
def test_each_verdict_sets_the_exit_status(gh, runs, status, verdict):
    gh.polls(runs)
    done = gh.wait()
    assert done.returncode == status
    assert done.stdout.splitlines()[0] == f"ci.yml: {verdict} {URL}/7"


def test_a_poll_that_fails_is_retried(gh):
    gh.script(
        "runs",
        {"err": "gh: HTTP 502"},
        {"out": "{}"},
        {"out": json.dumps({"workflow_runs": [run(7)]})},
    )
    done = gh.wait(GH_CI_WAIT_GIVE_UP_SECONDS="30")
    assert (done.returncode, done.stdout) == (0, f"ci.yml: success {URL}/7\n")


@pytest.mark.parametrize(
    "step",
    [
        {"err": "gh: HTTP 502: Bad Gateway"},
        {"out": "[1, 2]"},
        # fixed sleep: a gh call that hangs past the call's time limit.
        {"sleep": 5, "out": "{}"},
    ],
)
def test_gh_failing_past_the_give_up_time_exits_4_naming_the_error(gh, step):
    gh.script("runs", step)
    # Only the call that hangs gets a short limit: a busy machine can take longer than
    # that to start the fake gh.
    call = "0.3" if "sleep" in step else FAST["GH_CI_WAIT_CALL_SECONDS"]
    done = gh.wait(GH_CI_WAIT_CALL_SECONDS=call, GH_CI_WAIT_QUEUE_SECONDS="30")
    assert done.returncode == 4
    assert done.stderr.startswith("gh-ci-wait: gh kept failing")
    assert done.stderr.endswith("then run gh-ci-wait again\n")
    if "err" in step:
        assert "HTTP 502: Bad Gateway" in done.stderr


def test_a_short_sha_is_resolved_to_the_full_one(gh):
    base = git(gh.repo, "rev-parse", "origin/main")
    gh.polls([run(7)])
    assert gh.wait(base[:7]).returncode == 0
    assert any(f"head_sha={base}" in c for c in gh.calls())


def test_an_all_digit_target_git_has_as_a_commit_is_that_commit(gh):
    # Commit until one's SHA starts with 7 digits, about 1 in 27 commits.
    for attempt in range(2000):
        sha = git(gh.repo, "commit-tree", "HEAD^{tree}", "-m", f"try {attempt}")
        if sha[:7].isdigit():
            break
    gh.polls([run(7)])
    assert gh.wait(sha[:7]).returncode == 0
    assert any(f"head_sha={sha}" in c for c in gh.calls())
    assert not any(c.startswith("pr view") for c in gh.calls())


def test_an_all_digit_target_git_has_no_commit_for_is_a_pr(gh):
    gh.pr("a" * 40)
    gh.polls([run(7, event="pull_request")])
    assert gh.wait("1234567").returncode == 0
    assert f"pr view 1234567 -R {REPO} --json headRefOid,baseRefName" in gh.calls()


def test_a_full_sha_git_lacks_is_asked_about_as_it_is(gh):
    gh.polls([run(7)])
    assert gh.wait("C" * 40).returncode == 0
    assert any(f"head_sha={'c' * 40}" in c for c in gh.calls())


@pytest.mark.parametrize(
    ("args", "says"),
    [
        (["abcdef1"], "fetch it, or pass the full SHA"),
        (["main"], "a PR number or a commit"),
        (["1", "2"], "usage"),
        (["--remote", "nowhere"], "nowhere isn't on GitHub"),
    ],
)
def test_usage_errors_exit_2_before_asking_github(gh, args, says):
    done = gh.wait(*args)
    assert done.returncode == 2
    assert says in done.stderr
    assert gh.calls() == []


def test_a_setting_that_isnt_a_number_is_a_usage_error(gh):
    done = gh.wait(GH_CI_WAIT_POLL_SECONDS="soon")
    assert done.returncode == 2
    assert done.stderr == (
        "gh-ci-wait: GH_CI_WAIT_POLL_SECONDS is 'soon', not a number of seconds\n"
    )


def test_help_prints_the_docstring(gh):
    done = gh.wait("-h")
    assert done.returncode == 0
    assert "gh-ci-wait [-C DIR] [--remote NAME] [PR | SHA]" in done.stdout


def test_dash_c_looks_at_the_repo_in_dir(gh):
    gh.polls([run(7)])
    done = subprocess.run(
        [str(SCRIPT), "-C", str(gh.repo)],
        cwd=gh.fakes,
        capture_output=True,
        text=True,
        env=gh.env,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0
    assert any(f"head_sha={gh.head}" in c for c in gh.calls())


def test_stopping_it_stops_the_gh_call_it_is_waiting_on(gh):
    # fixed sleep: a gh call that hangs until the tool, stopped, stops it.
    gh.script("runs", {"sleep": 30, "out": "{}"})
    child = subprocess.Popen(
        [str(SCRIPT)],
        cwd=gh.repo,
        env=gh.env | {"GH_CI_WAIT_CALL_SECONDS": "60"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: gh.calls(), "the tool calling gh", seconds=30)
        pid = subprocess.run(
            ["pgrep", "-P", str(child.pid)], capture_output=True, text=True, check=False
        ).stdout.split()
        assert pid, "gh's process"
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=10)
        wait_until(
            lambda: not Path(f"/proc/{pid[0]}").exists(), "gh stopping", seconds=10
        )
    finally:
        child.kill()
        child.wait()


def test_a_head_that_moves_gives_the_new_head_the_whole_queue_wait(gh):
    # The move is seen past the queue wait from the start, so only a wait started over
    # on the new head lets its run, listed a poll later, count. The new head's wait
    # spans one gh call before its empty poll, which the 3 s queue wait dwarfs.
    gh.script(
        "pr",
        {"out": json.dumps({"headRefOid": "a" * 40, "baseRefName": "main"})},
        {
            # fixed sleep: the move must show past the queue wait from the start, and
            # a poll's own time is the only clock the fake gh has.
            "sleep": 3.5,
            "out": json.dumps({"headRefOid": "b" * 40, "baseRefName": "main"}),
        },
    )
    gh.polls(
        [run(8, event="pull_request", status="in_progress")],
        [],
        [run(9, event="pull_request")],
    )
    done = gh.wait("42", GH_CI_WAIT_QUEUE_SECONDS="3")
    assert (done.returncode, done.stdout.splitlines()[-1]) == (
        0,
        f"ci.yml: success {URL}/9",
    )


def test_the_settle_window_counts_from_the_first_run_seen_not_the_start(gh):
    # The first run shows past the settle window from the start; the run listed a
    # poll after it still joins. The poll that first lists a run never ends the wait,
    # so a slow machine only lengthens the first poll, which the test needs long.
    gh.script(
        "runs",
        # fixed sleep: the first run must show later than the settle window allows
        # from the start, and a poll's own time is the only clock the fake gh has.
        {"sleep": 1.5, "out": json.dumps({"workflow_runs": []})},
        {"out": json.dumps({"workflow_runs": [run(7)]})},
        {"out": json.dumps({"workflow_runs": [run(7), run(8, "late.yml", "failure")]})},
    )
    done = gh.wait(GH_CI_WAIT_SETTLE_SECONDS="1", GH_CI_WAIT_QUEUE_SECONDS="10")
    assert done.returncode == 1
    assert f"late.yml: failure {URL}/8" in done.stdout.splitlines()


def test_polls_are_spaced_by_the_poll_setting(gh):
    gh.polls(*[[run(7, status="in_progress")]] * 3, [run(7)])
    started = time.monotonic()
    assert gh.wait(GH_CI_WAIT_POLL_SECONDS="0.5").returncode == 0
    assert time.monotonic() - started >= 1.5


def test_failing_polls_back_off(gh):
    gh.script("runs", {"err": "gh: HTTP 502"})
    done = gh.wait(GH_CI_WAIT_POLL_SECONDS="0.1", GH_CI_WAIT_GIVE_UP_SECONDS="1.5")
    assert done.returncode == 4
    # Backing off from 0.2 s, doubling up to 0.8 s, makes about 4 calls in 1.5 s; at a
    # steady 0.1 s or none it would be 10 or more.
    assert len(gh.calls()) <= 6


def test_a_poll_that_works_starts_the_give_up_time_over(gh):
    # fixed sleep: the two failures must lie further apart than the give-up time, and
    # a call's own time is the only clock the fake gh has.
    gh.script(
        "runs",
        {"sleep": 0.7, "err": "gh: HTTP 502"},
        {"out": json.dumps({"workflow_runs": [run(7, status="in_progress")]})},
        {"sleep": 0.7, "err": "gh: HTTP 502"},
        {"out": json.dumps({"workflow_runs": [run(7)]})},
    )
    assert gh.wait(GH_CI_WAIT_GIVE_UP_SECONDS="1").returncode == 0


def test_gh_failing_past_the_queue_wait_is_still_retried(gh):
    gh.script(
        "runs",
        # fixed sleep: a failure that lasts past the queue wait.
        {"sleep": 1, "err": "gh: HTTP 502"},
        {"out": json.dumps({"workflow_runs": [run(7)]})},
    )
    done = gh.wait(GH_CI_WAIT_QUEUE_SECONDS="0.5", GH_CI_WAIT_GIVE_UP_SECONDS="30")
    assert (done.returncode, done.stdout) == (0, f"ci.yml: success {URL}/7\n")


def test_a_pr_whose_mergeable_state_gh_cant_give_gets_the_usual_message(gh):
    gh.pr("a" * 40)
    gh.polls([])
    done = gh.wait("42")
    assert done.returncode == 3
    assert "check the push reached it" in done.stderr


@pytest.mark.parametrize(
    ("key", "answer"),
    [
        ("runs", "{}"),
        ("runs", '{"workflow_runs": [1]}'),
        ("pr", "[1, 2]"),
        ("pr", '{"baseRefName": "main"}'),
    ],
)
def test_an_answer_of_another_shape_is_gh_failing(gh, key, answer):
    gh.pr("a" * 40)
    gh.polls([run(7, event="pull_request")])
    gh.script(key, {"out": answer})
    done = gh.wait("42")
    assert done.returncode == 4
    assert done.stderr.startswith("gh-ci-wait: gh kept failing")


def test_a_gh_call_past_its_time_is_stopped_not_waited_for(gh):
    # fixed sleep: a gh call that hangs far past the call's time limit.
    gh.script("runs", {"sleep": 30, "out": "{}"})
    started = time.monotonic()
    done = gh.wait(GH_CI_WAIT_CALL_SECONDS="0.3")
    assert done.returncode == 4
    assert time.monotonic() - started < 15


def test_no_gh_on_path_is_gh_failing(gh):
    bare = gh.fakes.parent / "bare"
    bare.mkdir()
    for tool in ("git", "python3"):
        (bare / tool).symlink_to(shutil.which(tool))
    done = gh.wait(PATH=str(bare))
    assert done.returncode == 4
    assert "no gh" in done.stderr


def test_a_repo_with_no_commit_is_a_usage_error(gh, tmp_path):
    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    git(empty, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    done = gh.wait("-C", str(empty))
    assert done.returncode == 2
    assert "no commit at HEAD" in done.stderr
    assert "commit first, or name a PR or a commit" in done.stderr
    assert gh.calls() == []
