"""build-paper vendor: copies paperkit into a project, so collaborators without llm-toolbox can build its
paper, and says where the project's copy has drifted from the toolbox's."""

import argparse
import filecmp
import shutil
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


def drift(root: Path) -> list[str]:
    """Where the project's paperkit/ and bin/build-paper differ from what `vendor` writes, one line each."""
    copy = root / "paperkit"
    if not copy.is_dir():
        return ["paperkit/: not in the project"]
    ours, theirs = files(HERE), files(copy)
    lines = [f"paperkit/{name}: only in the project" for name in sorted(theirs - ours)]
    lines += [
        f"paperkit/{name}: missing from the project" for name in sorted(ours - theirs)
    ]
    lines += [
        f"paperkit/{name}: differs"
        for name in sorted(ours & theirs)
        if not filecmp.cmp(HERE / name, copy / name, shallow=False)
    ]
    launcher = root / "bin" / "build-paper"
    if not launcher.exists() or launcher.read_text() != LAUNCHER:
        lines.append(
            "bin/build-paper: differs"
            if launcher.exists()
            else "bin/build-paper: missing"
        )
    return lines


def vendor(root: Path) -> None:
    """Copies paperkit to <root>/paperkit/, replacing an older copy, and writes <root>/bin/build-paper."""
    copy, launcher = root / "paperkit", root / "bin" / "build-paper"
    if launcher.exists() and MARK not in launcher.read_text():
        raise SystemExit(
            "build-paper vendor: bin/build-paper is the project's own, not one vendor wrote; move it "
            "aside first"
        )
    if copy.exists():
        shutil.rmtree(copy)
    shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)


def main(argv: list[str], root: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper vendor",
        description="Copy paperkit into the project, as paperkit/ at its root, and write bin/build-paper "
        "there, which builds the paper with that copy, so collaborators without llm-toolbox can build it. "
        "Run again to update the copy. While a project has its own paperkit/, llm-toolbox's build-paper "
        "builds with it too, never with the toolbox's.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="list the files where the project's copy differs from the toolbox's, and exit 1 if any do",
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
    vendor(root)
    print(
        f"Copied paperkit to {root / 'paperkit'} and wrote {root / 'bin' / 'build-paper'}; commit both."
    )
    return 0
