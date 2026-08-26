#!/usr/bin/env python3
"""Link every skill in this repo into ~/.claude/skills as a global skill.

Symlinks rather than copies, so editing a skill here takes effect immediately
and `git pull` is the whole update story.

Idempotent: safe to re-run. It only ever touches links that point into this
repo -- a real directory or a link to someone else's skill of the same name is
reported and left alone. Links into this repo whose source has gone away
(skill renamed or deleted) are removed.

    python3 install.py
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILLS_SRC = REPO / "skills"
SKILLS_DST = Path.home() / ".claude" / "skills"


def say(msg):
    print(f"  {msg}")


def link_skills() -> int:
    """Returns the number of skills that could not be linked."""
    if not SKILLS_SRC.is_dir():
        say(f"no skills directory at {SKILLS_SRC}")
        return 0

    SKILLS_DST.mkdir(parents=True, exist_ok=True)
    conflicts = 0

    for skill in sorted(p for p in SKILLS_SRC.iterdir() if p.is_dir()):
        dst = SKILLS_DST / skill.name
        if not (skill / "SKILL.md").is_file():
            say(f"skipped {skill.name}: no SKILL.md")
            continue
        if dst.is_symlink():
            # resolve() on a dangling link still gives the intended target.
            if dst.resolve() == skill:
                continue
            say(f"CONFLICT {dst} already links to {dst.resolve()} -- left alone")
            conflicts += 1
            continue
        if dst.exists():
            say(f"CONFLICT {dst} is a real directory -- left alone")
            conflicts += 1
            continue
        dst.symlink_to(skill)
        say(f"linked {dst} -> {skill}")

    return conflicts


def prune_stale_links():
    """Remove links into this repo whose skill no longer exists."""
    if not SKILLS_DST.is_dir():
        return
    for dst in sorted(SKILLS_DST.iterdir()):
        if not dst.is_symlink():
            continue
        target = dst.resolve()
        if target.parent == SKILLS_SRC and not target.exists():
            dst.unlink()
            say(f"removed stale link {dst}")


def main():
    print("[llm-toolbox skills]")
    prune_stale_links()
    conflicts = link_skills()
    if conflicts:
        say(f"{conflicts} skill(s) not linked -- resolve the conflicts above by hand")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
