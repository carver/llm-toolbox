"""gh-src prints the path of a shared, cached checkout of a repo at a ref. The tests
serve a git repo made in tmp_path over file://, through GH_SRC_URL_BASE."""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "gh-src"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """owner/tool with a tag v1 and, after it, a commit on main and on feature/x."""
    repo = tmp_path / "remote" / "owner" / "tool"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "lib.rs").write_text("fn v1() {}\n")
    git(repo, "add", "lib.rs")
    git(repo, "commit", "-q", "-m", "one")
    git(repo, "tag", "v1")
    (repo / "lib.rs").write_text("fn v2() {}\n")
    git(repo, "commit", "-q", "-am", "two")
    git(repo, "branch", "feature/x")
    return repo


def env_for(tmp_path: Path, fake_bin: Path | None = None) -> dict[str, str]:
    path = "/usr/bin:/bin" if fake_bin is None else f"{fake_bin}:/usr/bin:/bin"
    return {
        "PATH": path,
        "HOME": str(tmp_path / "home"),
        "GH_SRC_CACHE": str(tmp_path / "cache"),
        "GH_SRC_URL_BASE": f"file://{tmp_path / 'remote'}/",
    }


def gh_src(
    tmp_path: Path, *args: str, fake_bin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env_for(tmp_path, fake_bin),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def fake_git(tmp_path: Path, on_fetch: str) -> Path:
    """A git first on PATH that runs ON_FETCH (shell) before each fetch."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    script = fake_bin / "git"
    script.write_text(
        f'#!/bin/sh\ncase " $* " in *" fetch "*) {on_fetch} ;; esac\n'
        'exec /usr/bin/git "$@"\n'
    )
    script.chmod(0o755)
    return fake_bin


def test_a_tag_is_checked_out_and_its_path_printed(tmp_path, upstream):
    result = gh_src(tmp_path, "owner/tool", "v1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path / 'cache/owner/tool/v1'}\n"
    assert (tmp_path / "cache/owner/tool/v1/lib.rs").read_text() == "fn v1() {}\n"


def test_a_commit_sha_is_checked_out(tmp_path, upstream):
    sha = git(upstream, "rev-parse", "v1")
    result = gh_src(tmp_path, "owner/tool", sha)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"cache/owner/tool/{sha}/lib.rs").read_text() == "fn v1() {}\n"


def test_a_branch_with_a_slash_gets_one_folder(tmp_path, upstream):
    result = gh_src(tmp_path, "owner/tool", "feature/x")
    assert result.returncode == 0, result.stderr
    checkout = tmp_path / "cache/owner/tool/feature%2Fx"
    assert result.stdout == f"{checkout}\n"
    assert (checkout / "lib.rs").read_text() == "fn v2() {}\n"


def test_a_tag_and_its_sha_share_one_checkout(tmp_path, upstream):
    sha = git(upstream, "rev-parse", "v1")
    assert gh_src(tmp_path, "owner/tool", "v1").returncode == 0
    assert gh_src(tmp_path, "owner/tool", sha).returncode == 0
    cache = tmp_path / "cache/owner/tool"
    assert (cache / "v1").resolve() == (cache / sha).resolve()


def test_a_cached_checkout_needs_no_network(tmp_path, upstream):
    assert gh_src(tmp_path, "owner/tool", "v1").returncode == 0
    shutil.rmtree(tmp_path / "remote")
    result = gh_src(tmp_path, "owner/tool", "v1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path / 'cache/owner/tool/v1'}\n"


def test_refresh_fetches_a_moved_branch_and_keeps_the_old_copy_whole(
    tmp_path, upstream
):
    assert gh_src(tmp_path, "owner/tool", "main").returncode == 0
    checkout = tmp_path / "cache/owner/tool/main"
    # What a reader that cd'd into the checkout before the refresh holds.
    held = checkout.resolve()
    (upstream / "lib.rs").write_text("fn v3() {}\n")
    git(upstream, "commit", "-q", "-am", "three")
    assert gh_src(tmp_path, "owner/tool", "main").returncode == 0
    assert (checkout / "lib.rs").read_text() == "fn v2() {}\n"
    result = gh_src(tmp_path, "--refresh", "owner/tool", "main")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{checkout}\n"
    assert (checkout / "lib.rs").read_text() == "fn v3() {}\n"
    assert (held / "lib.rs").read_text() == "fn v2() {}\n"


def test_a_missing_ref_fails_with_gits_fatal_line_and_leaves_nothing(
    tmp_path, upstream
):
    result = gh_src(tmp_path, "owner/tool", "v9")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("gh-src: can't fetch v9 of file://")
    assert "fatal: " in result.stderr
    assert result.stderr.count("\n") == 1
    assert list((tmp_path / "cache/owner/tool").iterdir()) == []


def test_a_missing_repo_fails_with_gits_fatal_line(tmp_path, upstream):
    result = gh_src(tmp_path, "owner/nothing", "v1")
    assert result.returncode == 1
    assert "fatal: " in result.stderr
    assert result.stderr.count("\n") == 1


@pytest.mark.parametrize(
    "repo", ["tool", "owner/", "/tool", "a/b/c", "../tool", "owner/..", "./tool"]
)
def test_a_repo_not_owner_slash_name_is_a_usage_error(tmp_path, repo):
    result = gh_src(tmp_path, repo, "v1")
    assert result.returncode == 2
    assert "expected OWNER/REPO" in result.stderr
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("ref", ["..", ".", "/etc", "x/", "a..b", "-x", "a b"])
def test_a_ref_git_would_refuse_is_a_usage_error(tmp_path, upstream, ref):
    # After --, so argparse passes -x on as the ref.
    result = gh_src(tmp_path, "--", "owner/tool", ref)
    assert result.returncode == 2
    assert "not a ref" in result.stderr
    assert not (tmp_path / "cache").exists()


def test_git_never_stops_to_ask_for_a_password(tmp_path, upstream):
    log = tmp_path / "prompt.log"
    fake_bin = fake_git(tmp_path, f'echo "$GIT_TERMINAL_PROMPT" >> {log}')
    assert gh_src(tmp_path, "owner/tool", "v1", fake_bin=fake_bin).returncode == 0
    assert log.read_text() == "0\n"


def test_a_stopped_call_stops_git_and_leaves_nothing(tmp_path, upstream):
    started = tmp_path / "git.pid"
    fake_bin = fake_git(tmp_path, f"echo $$ > {started}; sleep 30")
    call = subprocess.Popen(
        [str(SCRIPT), "owner/tool", "v1"],
        env=env_for(tmp_path, fake_bin),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not started.exists() or not started.read_text().strip():
            assert time.monotonic() < deadline, "the fetch never started"
            time.sleep(0.05)
        git_pid = int(started.read_text())
        call.send_signal(signal.SIGTERM)
        assert call.wait(timeout=30) == 128 + signal.SIGTERM
    finally:
        call.kill()
        call.communicate()
    deadline = time.monotonic() + 10
    while Path(f"/proc/{git_pid}").exists():
        assert time.monotonic() < deadline, "git outlived gh-src"
        time.sleep(0.05)
    assert list((tmp_path / "cache/owner/tool").iterdir()) == []


def test_two_calls_at_once_share_one_checkout(tmp_path, upstream):
    # Each fetch waits until both calls are fetching, so they always race.
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    fake_bin = fake_git(
        tmp_path,
        f"touch {barrier}/$$; n=0; "
        f'while [ "$(ls {barrier} | wc -l)" -lt 2 ] && [ $n -lt 200 ]; '
        "do sleep 0.05; n=$((n+1)); done",
    )
    calls = [
        subprocess.Popen(
            [str(SCRIPT), "owner/tool", "v1"],
            env=env_for(tmp_path, fake_bin),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        results = [(call.communicate(timeout=60), call.returncode) for call in calls]
    finally:
        for call in calls:
            call.kill()
            call.communicate()
    assert len(os.listdir(barrier)) == 2
    checkout = tmp_path / "cache/owner/tool/v1"
    for (out, err), status in results:
        assert status == 0, err
        assert out == f"{checkout}\n"
    assert sorted(p.name for p in checkout.parent.iterdir()) == [".commits", "v1"]
    assert len(list((checkout.parent / ".commits").iterdir())) == 1
    assert (checkout / "lib.rs").read_text() == "fn v1() {}\n"
