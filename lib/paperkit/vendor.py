"""build-paper vendor: copies paperkit into a project, so collaborators without llm-toolbox can build its
paper, and says where the project's copy has drifted from the toolbox's."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # the paperkit this runs from
MARK = "Written by llm-toolbox's `build-paper vendor`"  # in a launcher vendor may overwrite

LAUNCHER = f"""#!/usr/bin/env bash
# build-paper, from this repo's copy of paperkit (paperkit/README.md): builds paper/paper.typ.
# {MARK}; `build-paper vendor --check` there lists
# where this copy differs from the toolbox's, and `build-paper vendor` updates it.
root="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
# No __pycache__ in the project, which --final would find uncommitted
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root${{PYTHONPATH:+:$PYTHONPATH}}" exec uv run -q \\
  --with-requirements "$root/paperkit/requirements.txt" python -P -m paperkit "$@"
"""


def files(folder: Path) -> set[str]:
    """The files under `folder`, relative to it, without Python's caches."""
    return {
        str(path.relative_to(folder))
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


MANIFEST = "VENDORED"  # in the copy: the toolbox commit it came from, and a hash of each file vendor wrote


def digest(data: bytes) -> str:
    """A file's hash, blind to line endings, so a checkout that writes CRLF (git's autocrlf) edits nothing."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def hashes(root: Path) -> dict[str, str]:
    """A hash of each file of <root>'s copy as it stands, by its path in the project: paperkit's files
    (without caches or the manifest) and bin/build-paper."""
    copy, launcher = root / "paperkit", root / "bin" / "build-paper"
    found = {
        f"paperkit/{name}": digest((copy / name).read_bytes())
        for name in files(copy)
        if name != MANIFEST
    }
    if launcher.exists():
        found["bin/build-paper"] = digest(launcher.read_bytes())
    return found


def toolbox_hashes() -> dict[str, str]:
    """What hashes() would give for a copy vendor writes now."""
    found = {
        f"paperkit/{name}": digest((HERE / name).read_bytes()) for name in files(HERE)
    }
    return found | {"bin/build-paper": digest(LAUNCHER.encode())}


def differing(now: dict[str, str], toolbox: dict[str, str]) -> list[str]:
    """The files whose hash in `now` isn't the toolbox's, including those only one of them has."""
    return sorted(
        name
        for name in now.keys() | toolbox.keys()
        if now.get(name) != toolbox.get(name)
    )


def recorded(root: Path) -> dict | None:
    """<root>'s manifest; None for a copy without one, or with one that isn't a manifest (a merge
    conflict in it, say)."""
    try:
        record = json.loads((root / "paperkit" / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    files_ok = isinstance(record, dict) and isinstance(record.get("files"), dict)
    return record if files_ok and isinstance(record.get("from"), str) else None


def edits(root: Path, record: dict) -> list[tuple[str, str]]:
    """Each file of <root>'s copy edited, added or removed since `record` was written, with which."""
    now, then = hashes(root), record["files"]
    found = [(name, "added") for name in now.keys() - then.keys()]
    found += [(name, "removed") for name in then.keys() - now.keys()]
    found += [
        (name, "edited") for name in now.keys() & then.keys() if now[name] != then[name]
    ]
    return sorted(found)


def edit_warnings(root: Path) -> list[str]:
    """A warning for each file of <root>'s copy edited, added or removed since it was vendored, or for a
    manifest that can't be read."""
    if not (root / "paperkit" / MANIFEST).exists():
        return []
    record = recorded(root)
    if record is None:
        return [
            (
                "paperkit/VENDORED: unreadable, so edits to the copy can't be told from what was "
                "vendored; vendor again to rewrite it"
            )
        ]
    return [
        f"{name}: {how} since it was vendored from {record['from']}; upstream the change to "
        "llm-toolbox's lib/paperkit/ and vendor again"
        for name, how in edits(root, record)
    ]


def drift(root: Path) -> list[str]:
    """Where the project's paperkit/ and bin/build-paper differ from what `vendor` writes, one line each:
    edits made in the project since it was vendored, then the files where the copy is behind the toolbox."""
    if not (root / "paperkit").is_dir():
        return ["paperkit/: not in the project"]
    now, toolbox = hashes(root), toolbox_hashes()
    record = recorded(root)
    if record is None:
        return (
            [
                f"{name}: only in the project"
                for name in sorted(now.keys() - toolbox.keys())
            ]
            + [
                f"{name}: missing from the project"
                for name in sorted(toolbox.keys() - now.keys())
            ]
            + [
                f"{name}: differs"
                for name in differing(now, toolbox)
                if name in now.keys() & toolbox.keys()
            ]
        )
    found = edits(root, record)
    edited = {name for name, _ in found}
    return [f"{name}: {how} in the project" for name, how in found] + [
        f"{name}: behind the toolbox's"
        for name in differing(now, toolbox)
        if name not in edited
    ]


def toolbox_commit() -> str:
    """The toolbox's commit, marked when lib/paperkit/ has changes it doesn't hold."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(HERE), *args], capture_output=True, text=True, check=False
        )

    head = git("rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        return "an unknown commit"
    dirty = git("status", "--porcelain", "--", ".").stdout.strip()
    return head.stdout.strip() + (" with uncommitted changes" if dirty else "")


def vendor(root: Path) -> list[str]:
    """Copies paperkit to <root>/paperkit/, replacing an older copy, writes <root>/bin/build-paper, and
    records both in the manifest. Refuses to drop an edit made in the project that the toolbox doesn't have
    yet: it goes upstream (or is undone) first. Returns the files a copy without a readable manifest held
    that differed from the toolbox's, which nothing could tell from local edits."""
    copy, launcher = root / "paperkit", root / "bin" / "build-paper"
    if launcher.exists() and MARK not in launcher.read_text():
        raise SystemExit(
            "build-paper vendor: bin/build-paper is the project's own, not one vendor wrote; move it "
            "aside first"
        )
    replaced: list[str] = []
    if copy.exists():
        now, toolbox = hashes(root), toolbox_hashes()
        record = recorded(root)
        if record is None:
            replaced = differing(now, toolbox)
        else:
            # A removal holds nothing vendoring could lose
            kept = {name for name, how in edits(root, record) if how != "removed"}
            local = [name for name in differing(now, toolbox) if name in kept]
            if local:
                raise SystemExit(
                    f"build-paper vendor: the project's copy has edits llm-toolbox doesn't have yet: "
                    f"{', '.join(local)}. Vendoring would drop them: upstream them to llm-toolbox's "
                    "lib/paperkit/, or undo them in the project, then vendor again."
                )
        shutil.rmtree(copy)
    shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    record = {"from": f"llm-toolbox {toolbox_commit()}", "files": hashes(root)}
    (copy / MANIFEST).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return replaced


def main(argv: list[str], root: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper vendor",
        description="Copy paperkit into the project, as paperkit/ at its root, and write bin/build-paper "
        "there, which builds the paper with that copy, so collaborators without llm-toolbox can build it. "
        "Run again to update the copy. While a project has its own paperkit/, llm-toolbox's build-paper "
        "builds with it too, never with the toolbox's. paperkit/VENDORED records the toolbox commit and a "
        "hash of each file, so a build warns of each edit made in the project since, and vendoring again "
        "refuses to drop an edit the toolbox doesn't have yet: upstream it to lib/paperkit/ first.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="list the files edited in the project since vendoring, then those where the copy is behind "
        "the toolbox's, and exit 1 if there are any",
    )
    args = parser.parse_args(argv)
    if (root / "paperkit").resolve() == HERE:
        print(
            "build-paper vendor: this is the project's own copy of paperkit; run llm-toolbox's build-paper "
            "to compare it with the toolbox's or update it",
            file=sys.stderr,
        )
        return 1
    if args.check:
        lines = drift(root)
        print(
            "\n".join(lines)
            if lines
            else "paperkit/ and bin/build-paper match the toolbox's"
        )
        return 1 if lines else 0
    replaced = vendor(root)
    print(
        f"Copied paperkit to {root / 'paperkit'} and wrote {root / 'bin' / 'build-paper'}; commit both."
    )
    if replaced:
        print(
            "The old copy had no manifest to tell local edits from an older version, and these files "
            f"differed from the toolbox's; check none held an edit to upstream: {', '.join(replaced)}"
        )
    return 0
