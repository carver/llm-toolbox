#!/usr/bin/env python3
"""Link this repo into the home directory: every skill into ~/.claude/skills and
~/.agents/skills as a global skill, and GLOBAL_CLAUDE.md to ~/.claude/CLAUDE.md and
~/.agents/AGENTS.md, one set of guidelines whichever convention a tool reads.

Symlinks rather than copies, so editing here takes effect immediately and
`git pull` is the whole update story.

Idempotent: safe to re-run. Skills: it only ever touches links that point into
this repo. A real directory, or a link to someone else's skill of the same
name, gets reported and left alone. It removes links into this repo whose
source is no longer a skill (renamed, deleted, or missing its SKILL.md).
Guidelines: a real file moves aside to .bak, a link is replaced wherever it
points, since a link is cheap to put back.

    python3 install.py
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILLS_SRC = REPO / "skills"
SKILLS_DST = Path.home() / ".claude" / "skills"
# The AGENTS.md convention's home for skills. Tools that read ~/.agents/AGENTS.md look here.
AGENTS_SKILLS_DST = Path.home() / ".agents" / "skills"
GUIDELINES_SRC = REPO / "GLOBAL_CLAUDE.md"
GUIDELINES_DSTS = [Path.home() / ".claude" / "CLAUDE.md", Path.home() / ".agents" / "AGENTS.md"]


def say(msg):
    print(f"  {msg}")


def is_skill(path: Path) -> bool:
    """The one rule for what gets linked and what gets pruned."""
    return (path / "SKILL.md").is_file()


def link_skills() -> int:
    """Returns the number of skills that could not be linked."""
    if not SKILLS_SRC.is_dir():
        say(f"no skills directory at {SKILLS_SRC}")
        return 0

    conflicts = 0
    for dst_dir in (SKILLS_DST, AGENTS_SKILLS_DST):
        dst_dir.mkdir(parents=True, exist_ok=True)
        for skill in sorted(p for p in SKILLS_SRC.iterdir() if p.is_dir()):
            if not is_skill(skill):
                if dst_dir == SKILLS_DST:
                    say(f"skipped {skill.name}: no SKILL.md")
                continue
            conflicts += link_skill(skill, dst_dir / skill.name)
    return conflicts


def link_skill(skill: Path, dst: Path) -> int:
    """Returns 1 when something else already holds the name."""
    if dst.is_symlink():
        # resolve() on a dangling link still gives the intended target.
        if dst.resolve() == skill:
            return 0
        say(f"CONFLICT {dst} already links to {dst.resolve()} -- left alone")
        return 1
    if dst.exists():
        say(f"CONFLICT {dst} is a real directory -- left alone")
        return 1
    dst.symlink_to(skill)
    say(f"linked {dst} -> {skill}")
    return 0


def link_guidelines() -> int:
    """Returns the number of destinations that could not be linked."""
    conflicts = 0
    for dst in GUIDELINES_DSTS:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            if dst.resolve() == GUIDELINES_SRC:
                continue
            dst.unlink()
        elif dst.exists():
            bak = dst.with_name(dst.name + ".bak")
            if bak.exists():
                say(f"CONFLICT {dst} is a real file and {bak} already exists -- left alone")
                conflicts += 1
                continue
            dst.rename(bak)
            say(f"moved {dst} to {bak}")
        dst.symlink_to(GUIDELINES_SRC)
        say(f"linked {dst} -> {GUIDELINES_SRC}")
    return conflicts


def prune_stale_links():
    """Remove links into this repo whose target is no longer a skill, by
    the same rule link_skills uses to create them."""
    for dst_dir in (SKILLS_DST, AGENTS_SKILLS_DST):
        if not dst_dir.is_dir():
            continue
        for dst in sorted(dst_dir.iterdir()):
            if not dst.is_symlink():
                continue
            target = dst.resolve()
            if target.parent == SKILLS_SRC and not is_skill(target):
                dst.unlink()
                say(f"removed stale link {dst}")


def main():
    print("[llm-toolbox]")
    prune_stale_links()
    conflicts = link_skills() + link_guidelines()
    if conflicts:
        say(f"{conflicts} link(s) not made -- resolve the conflicts above by hand")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
