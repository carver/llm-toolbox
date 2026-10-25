"""The pre-commit hook lints what's staged with uv's tools, formats and restages what it can, and
needs only uv."""

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


def stage(repo, source, path="mod.py"):
    (repo / path).parent.mkdir(exist_ok=True)
    (repo / path).write_text(source)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def index_text(repo, path):
    """The file's content in the index."""
    return subprocess.run(
        ["git", "-C", str(repo), "show", f":{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


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


@pytest.mark.parametrize("path", ["mod.py", "bin/tool"])
def test_an_unformatted_staged_file_is_formatted_staged_and_named(repo, path):
    stage(repo, "x  =  1\n", path)
    result = hook(repo)
    assert result.returncode == 0, result.stderr
    assert index_text(repo, path) == (repo / path).read_text() == "x = 1\n"
    assert f"{path}; the formatted file is staged" in result.stderr


def test_a_partly_staged_unformatted_file_fails_and_is_left_alone(repo):
    stage(repo, "x  =  1\n")
    (repo / "mod.py").write_text("x  =  1\ny = 2\n")
    result = hook(repo)
    assert result.returncode != 0
    assert index_text(repo, "mod.py") == "x  =  1\n"
    assert (repo / "mod.py").read_text() == "x  =  1\ny = 2\n"
    assert "mod.py has unstaged changes" in result.stderr


def test_a_syntax_error_fails_and_names_the_file(repo):
    stage(repo, "x = (\n")
    result = hook(repo)
    assert result.returncode != 0
    assert "mod.py" in result.stdout + result.stderr


def test_a_lint_error_elsewhere_still_fails_after_restaging(repo):
    stage(repo, "x  =  1\n")
    stage(repo, "import os\n", "other.py")
    result = hook(repo)
    assert result.returncode != 0
    assert "other.py" in result.stdout + result.stderr
    assert index_text(repo, "mod.py") == "x = 1\n"


def test_a_symlink_is_never_written_through(repo, tmp_path_factory):
    target = tmp_path_factory.mktemp("outside") / "target.py"
    target.write_text("x  =  1\n")
    (repo / "link.py").symlink_to(target)
    stage(repo, "x = 1\n")
    hook(repo)
    assert target.read_text() == "x  =  1\n"


def test_a_file_name_is_not_read_as_a_pattern(repo):
    stage(repo, "x  =  1\n", "a[1].py")
    (repo / "a1.py").write_text("y = 2\n")
    result = hook(repo)
    assert result.returncode == 0, result.stderr
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert "a1.py" not in tracked


def test_a_path_commit_is_refused_rather_than_restaged(repo, monkeypatch):
    """`git commit PATH` commits from a temporary index, so a restage there would leave the real
    index holding the unformatted file."""
    monkeypatch.delenv("AI_AGENT", raising=False)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    stage(repo, "x = 1\n")
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "one"], check=True)
    subprocess.run([*git, "config", "core.hooksPath", str(HOOK.parent)], check=True)
    (repo / "mod.py").write_text("x  =  2\n")
    result = subprocess.run(
        [*git, "commit", "-q", "-m", "two", "mod.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "git add" in result.stderr
    assert index_text(repo, "mod.py") == "x = 1\n"


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
