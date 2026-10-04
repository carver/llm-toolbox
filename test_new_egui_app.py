"""new-egui-app writes a ready-to-build Rust egui project from templates/egui-app."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "new-egui-app"


def run(*args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *map(str, args)],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=False
    )


def text_files(root: Path) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        if path.suffix == ".png":
            continue
        found.append(path)
    return found


@pytest.fixture
def made(tmp_path: Path) -> Path:
    target = tmp_path / "sky-lantern"
    result = run(target, "--title", "Sky Lantern")
    assert result.returncode == 0, result.stderr
    return target


def test_writes_a_complete_project(made: Path) -> None:
    for name in [
        "Cargo.toml",
        "src/main.rs",
        "src/lib.rs",
        "src/app.rs",
        "tests/window.rs",
        "assets/icon.svg",
        "assets/icon.png",
        "assets/sky-lantern.desktop",
        "scripts/install-desktop.sh",
        ".githooks/pre-commit",
        ".githooks/agent-pre-commit",
        ".github/workflows/ci.yml",
        "rustfmt.toml",
        ".gitignore",
        "CLAUDE.md",
        "README.md",
    ]:
        assert (made / name).is_file(), name
    assert (made / "assets/icon.png").read_bytes().startswith(b"\x89PNG")


def test_leaves_no_placeholder(made: Path) -> None:
    for path in text_files(made):
        assert not re.search(r"\{\{\s*\w+\s*\}\}", path.read_text()), path
    for path in made.rglob("*"):
        assert "{{" not in path.name, path


def test_names_line_up(made: Path) -> None:
    cargo = (made / "Cargo.toml").read_text()
    assert re.search(r'^name = "sky-lantern"$', cargo, re.MULTILINE)
    main = (made / "src/main.rs").read_text()
    assert 'with_app_id("sky-lantern")' in main
    assert 'with_title("Sky Lantern")' in main
    assert "use sky_lantern::" in main
    assert "use sky_lantern::" in (made / "tests/window.rs").read_text()
    desktop = (made / "assets/sky-lantern.desktop").read_text()
    assert "StartupWMClass=sky-lantern\n" in desktop
    assert "Icon=sky-lantern\n" in desktop
    assert "Name=Sky Lantern\n" in desktop
    install = (made / "scripts/install-desktop.sh").read_text()
    assert "sky-lantern.desktop" in install


def test_title_defaults_from_the_name(tmp_path: Path) -> None:
    target = tmp_path / "tide_clock"
    assert run(target).returncode == 0
    assert 'with_title("Tide Clock")' in (target / "src/main.rs").read_text()
    assert (target / "assets/tide_clock.desktop").is_file()


def test_name_overrides_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "work"
    assert run(target, "--name", "fern").returncode == 0
    assert re.search(
        r'^name = "fern"$', (target / "Cargo.toml").read_text(), re.MULTILINE
    )
    assert (target / "assets/fern.desktop").is_file()


def test_rejects_a_name_cargo_would(tmp_path: Path) -> None:
    result = run(tmp_path / "x", "--name", "Bad Name")
    assert result.returncode != 0
    assert "name" in result.stderr
    assert not (tmp_path / "x").exists()


def test_scripts_are_executable(made: Path) -> None:
    for name in [
        ".githooks/pre-commit",
        ".githooks/agent-pre-commit",
        "scripts/install-desktop.sh",
    ]:
        assert os.access(made / name, os.X_OK), name
    assert not os.access(made / "Cargo.toml", os.X_OK)


def test_makes_a_repo_with_hooks_and_no_commit(made: Path) -> None:
    assert git(made, "config", "core.hooksPath").stdout.strip() == ".githooks"
    assert git(made, "rev-parse", "--verify", "HEAD").returncode != 0


def test_refuses_a_non_empty_dir(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "notes.txt").write_text("mine\n")
    result = run(target)
    assert result.returncode != 0
    assert "not empty" in result.stderr
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]


def test_uses_an_existing_empty_repo(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    assert git(target, "init", "-q").returncode == 0
    result = run(target)
    assert result.returncode == 0, result.stderr
    assert (target / "Cargo.toml").is_file()
    assert git(target, "config", "core.hooksPath").stdout.strip() == ".githooks"


def test_help_says_it_does_not_commit() -> None:
    result = run("-h")
    assert result.returncode == 0
    assert "--title" in result.stdout
    assert "commit" in result.stdout


@pytest.mark.skipif(shutil.which("rustfmt") is None, reason="rustfmt not on PATH")
@pytest.mark.parametrize("name", ["aaa", "zzz"])
def test_rust_is_formatted_whatever_the_name(tmp_path: Path, name: str) -> None:
    # rustfmt sorts imports, so the crate's own `use` must not move with its name.
    target = tmp_path / name
    assert run(target).returncode == 0
    sources = sorted(str(p.relative_to(target)) for p in target.rglob("*.rs"))
    result = subprocess.run(
        ["rustfmt", "--check", "--edition", "2024", *sources],
        cwd=target,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
