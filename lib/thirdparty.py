"""Other authors' skills: their upstream sources, the copies installed here, and
the patches in skill-patches/ that this repo keeps on top of them.

A patch is a unified diff from a skill's upstream folder to the installed copy,
with paths relative to the skill folder (a/SKILL.md, b/SKILL.md). It starts
with one `# Why: ...` line per layer of edits, oldest first, which `patch`
skips as leading garbage.
"""

import difflib
import importlib.util
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "skill-patches"
INSTALLED = Path.home() / ".agents" / "skills"
LOCK = Path.home() / ".agents" / ".skill-lock.json"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "skills-news"


@dataclass
class FileStat:
    added: int
    removed: int
    path: str


def third_party_skills() -> dict[str, list[str]]:
    """install.py's THIRD_PARTY_SKILLS: source -> skill names."""
    spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.THIRD_PARTY_SKILLS


def fetch(source: str, cache: Path) -> Path:
    """A shallow clone of the source's default branch, refreshed in place."""
    clone = cache / source.replace("/", "--")
    git = ["git", "-c", "advice.detachedHead=false"]
    if (clone / ".git").is_dir():
        subprocess.run(
            [*git, "-C", str(clone), "fetch", "-q", "--depth", "1", "origin", "HEAD"],
            check=True,
        )
        subprocess.run(
            [*git, "-C", str(clone), "reset", "-q", "--hard", "FETCH_HEAD"], check=True
        )
    else:
        clone.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{source}.git"
        subprocess.run(
            [*git, "clone", "-q", "--depth", "1", url, str(clone)], check=True
        )
    return clone


def upstream_skills(clone: Path) -> dict[str, Path]:
    """Every folder with a SKILL.md, by folder name, as `npx skills` names them."""
    return {
        skill_md.parent.name: skill_md.parent
        for skill_md in sorted(clone.rglob("SKILL.md"))
        if ".git" not in skill_md.relative_to(clone).parts
    }


def tree_hash(clone: Path, folder: Path) -> str:
    """The git tree hash of `folder` at the clone's HEAD, as the skills lock records it."""
    rel = folder.relative_to(clone)
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"HEAD:{rel}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _files(root: Path) -> dict[str, Path]:
    return {
        str(f.relative_to(root)): f
        for f in sorted(root.rglob("*"))
        if f.is_file() and not f.name.endswith((".rej", ".orig"))
    }


def _lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def diff(old_dir: Path, new_dir: Path) -> tuple[list[FileStat], str]:
    """Per-file stats and a unified patch from `old_dir` to `new_dir`."""
    old, new = _files(old_dir), _files(new_dir)
    stats, patch = [], []
    for rel in sorted(old.keys() | new.keys()):
        before, after = _lines(old.get(rel)), _lines(new.get(rel))
        hunk = list(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
        if not hunk:
            continue
        body = hunk[2:]
        added = sum(line.startswith("+") for line in body)
        removed = sum(line.startswith("-") for line in body)
        stats.append(FileStat(added, removed, rel))
        patch += hunk
    return stats, "".join(patch)


def patch_file(name: str) -> Path | None:
    path = PATCHES / f"{name}.patch"
    return path if path.is_file() else None


def whys(patch: Path) -> list[str]:
    """The reasons heading a patch, one per layer of edits, oldest first."""
    reasons = []
    for line in patch.read_text().splitlines():
        if not line.startswith("# Why:"):
            break
        reasons.append(line.removeprefix("# Why:").strip())
    return reasons


def write_patch(name: str, reasons: list[str], patch: str) -> Path:
    if isinstance(reasons, str):
        raise TypeError("write_patch takes a list of reasons, not one string")
    PATCHES.mkdir(exist_ok=True)
    path = PATCHES / f"{name}.patch"
    path.write_text("".join(f"# Why: {reason}\n" for reason in reasons) + patch)
    return path


def run_patch(skill_dir: Path, patch: Path, *flags: str) -> bool:
    """GNU patch on `skill_dir`; True when every hunk applied."""
    with patch.open() as diff_input:
        result = subprocess.run(
            [
                "patch",
                "-p1",
                "-E",
                "-s",
                "-f",
                "--no-backup-if-mismatch",
                "-r",
                "-",
                *flags,
            ],
            cwd=skill_dir,
            stdin=diff_input,
            capture_output=True,
            text=True,
            check=False,
        )
    return result.returncode == 0


def is_applied(skill_dir: Path, patch: Path) -> bool:
    return run_patch(skill_dir, patch, "-R", "--dry-run")


def apply(skill_dir: Path, patch: Path) -> str:
    """'applied', 'already' or 'conflict'. A conflict leaves the skill untouched."""
    if is_applied(skill_dir, patch):
        return "already"
    if not run_patch(skill_dir, patch, "-N", "--dry-run"):
        return "conflict"
    run_patch(skill_dir, patch, "-N")
    return "applied"


@contextmanager
def copy_with(
    skill_dir: Path, patch: Path, reverse: bool = False
) -> Iterator[Path | None]:
    """A temp copy of `skill_dir` with `patch` applied (or reversed); None when it won't apply."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / skill_dir.name
        shutil.copytree(skill_dir, copy)
        ok = run_patch(copy, patch, "-R" if reverse else "-N")
        yield copy if ok else None
