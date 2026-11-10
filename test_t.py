"""./t, the repo's test runner, with a fake uv first on PATH that prints its argv."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
PIN = ["-q", "--no-project", "--with-requirements", "requirements-dev.txt"]
PYTEST = ["pytest", "-q", "-p", "no:cacheprovider"]


@pytest.fixture
def fake_uv(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text('#!/bin/sh\necho "cwd=$PWD"\nprintf "%s\\n" "$@"\n')
    (bin_dir / "uv").chmod(0o755)
    return bin_dir


def run_t(fake_uv, *args, cwd=ROOT):
    done = subprocess.run(
        [str(ROOT / "t"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"PATH": f"{fake_uv}{os.pathsep}{os.environ['PATH']}"},
    )
    assert done.returncode == 0, done.stderr
    cwd_line, *argv = done.stdout.splitlines()
    return cwd_line.removeprefix("cwd="), argv


def test_files_run_in_one_process_from_the_repo_root(fake_uv):
    cwd, argv = run_t(fake_uv, "test_t.py", "-k", "root", cwd=ROOT / "lib")
    assert Path(cwd) == ROOT
    assert argv == ["run", *PIN, *PYTEST, "-n0", "test_t.py", "-k", "root"]


def test_with_no_arguments_the_whole_suite_runs_in_parallel(fake_uv):
    _, argv = run_t(fake_uv)
    assert argv == ["run", *PIN, *PYTEST]
