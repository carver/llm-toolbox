"""The pre-commit hook lints what's staged with uv's tools, and needs only uv."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / ".githooks" / "pre-commit"


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("ruff.toml", "requirements-dev.txt"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def stage(repo, source):
    (repo / "mod.py").write_text(source)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def hook(repo, env=None):
    return subprocess.run(
        [str(HOOK)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )


def test_clean_staged_code_passes(repo):
    stage(repo, "x = 1\n")
    result = hook(repo)
    assert result.returncode == 0, result.stderr


def test_a_lint_error_fails(repo):
    stage(repo, "import os\n")
    assert hook(repo).returncode != 0


def test_the_hook_ignores_a_venv(repo):
    venv_python = repo / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\nexit 1\n")
    venv_python.chmod(0o755)
    stage(repo, "x = 1\n")
    assert hook(repo).returncode == 0


def test_without_uv_it_says_to_install_uv(repo, tmp_path_factory):
    bare = tmp_path_factory.mktemp("bin")
    for tool in ("sh", "git", "mktemp", "rm"):
        os.symlink(shutil.which(tool), bare / tool)
    stage(repo, "x = 1\n")
    result = hook(repo, env={"PATH": str(bare)})
    assert result.returncode != 0
    assert "install uv" in result.stderr
