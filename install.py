#!/usr/bin/env python3
"""Link this repo into the home directory: every skill into ~/.claude/skills and
~/.agents/skills as a global skill, every tool in bin/ into ~/.local/bin, and
GLOBAL_CLAUDE.md to ~/.claude/CLAUDE.md and ~/.agents/AGENTS.md, one set of
guidelines whichever convention a tool reads. Registers the Claude Code hooks in
HOOKS in ~/.claude/settings.json. Installs the other authors' skills in
THIRD_PARTY_SKILLS with the `skills` CLI (npx), so a recreated sandbox gets them back.
Points this repo's git at the hooks in .githooks.

Symlinks rather than copies, so editing here takes effect immediately and
`git pull` is the whole update story.

Idempotent: safe to re-run. Skills: it only ever touches links that point into
this repo. A real directory, or a link to someone else's skill of the same
name, gets reported and left alone. It removes links into this repo whose
source is no longer a skill (renamed, deleted, or missing its SKILL.md).
Guidelines: a real file moves aside to .bak, a link is replaced wherever it
points, since a link is cheap to put back. Hooks: added once, beside whatever
else the settings hold; a settings file that is not valid JSON is left alone.
Third-party skills: only missing ones are installed; it never updates or
removes one (`npx skills update -g` does that).

    python3 install.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILLS_SRC = REPO / "skills"
SKILLS_DST = Path.home() / ".claude" / "skills"
# The AGENTS.md convention's home for skills. Tools that read ~/.agents/AGENTS.md look here.
AGENTS_SKILLS_DST = Path.home() / ".agents" / "skills"
BIN_SRC = REPO / "bin"
BIN_DST = Path.home() / ".local" / "bin"
GUIDELINES_SRC = REPO / "GLOBAL_CLAUDE.md"
GUIDELINES_DSTS = [
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".agents" / "AGENTS.md",
]
SETTINGS = Path.home() / ".claude" / "settings.json"
# (event, matcher, tool in bin/, hook fields beyond the command)
HOOKS = [
    (
        "PreToolUse",
        "Bash",
        "agent-commit-gate",
        {"timeout": 900, "statusMessage": "Running the repo's agent pre-commit check"},
    ),
]
# Other authors' skills, by GitHub source. Installed into ~/.agents/skills with a
# link from ~/.claude/skills, the layout `npx skills add -g` gives Claude Code and Codex.
THIRD_PARTY_SKILLS = {
    "mattpocock/skills": [
        "ask-matt",
        "claude-handoff",
        "code-review",
        "codebase-design",
        "diagnosing-bugs",
        "domain-modeling",
        "grill-me",
        "grill-with-docs",
        "grilling",
        "handoff",
        "implement",
        "improve-codebase-architecture",
        "prototype",
        "research",
        "retro",
        "setup-matt-pocock-skills",
        "tdd",
        "teach",
        "to-questionnaire",
        "to-spec",
        "to-tickets",
        "triage",
        "wait-what",
        "wayfinder",
        "wizard",
        "writing-beats",
        "writing-for-agents",
        "writing-fragments",
        "writing-shape",
    ],
    "vercel-labs/skills": ["find-skills"],
}


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
            conflicts += link_one(skill, dst_dir / skill.name)
    return conflicts


def is_tool(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def link_tools() -> int:
    """Returns the number of tools that could not be linked."""
    if not BIN_SRC.is_dir():
        return 0
    BIN_DST.mkdir(parents=True, exist_ok=True)
    return sum(
        link_one(tool, BIN_DST / tool.name)
        for tool in sorted(BIN_SRC.iterdir())
        if is_tool(tool)
    )


def link_one(src: Path, dst: Path) -> int:
    """Returns 1 when something else already holds the name."""
    if dst.is_symlink():
        # resolve() on a dangling link still gives the intended target.
        if dst.resolve() == src:
            return 0
        say(f"CONFLICT {dst} already links to {dst.resolve()} -- left alone")
        return 1
    if dst.exists():
        say(f"CONFLICT {dst} already exists and is not a link -- left alone")
        return 1
    dst.symlink_to(src)
    say(f"linked {dst} -> {src}")
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
                say(
                    f"CONFLICT {dst} is a real file and {bak} already exists -- left alone"
                )
                conflicts += 1
                continue
            dst.rename(bak)
            say(f"moved {dst} to {bak}")
        dst.symlink_to(GUIDELINES_SRC)
        say(f"linked {dst} -> {GUIDELINES_SRC}")
    return conflicts


def register_hooks() -> int:
    """Returns 1 when the settings file cannot be read as JSON."""
    try:
        settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    except json.JSONDecodeError:
        say(f"CONFLICT {SETTINGS} is not valid JSON -- hooks not registered")
        return 1
    changed = False
    for event, matcher, tool, fields in HOOKS:
        command = str(BIN_DST / tool)
        groups = settings.setdefault("hooks", {}).setdefault(event, [])
        if any(
            h.get("command") == command
            for g in groups
            if g.get("matcher") == matcher
            for h in g.get("hooks", [])
        ):
            continue
        groups.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command, **fields}],
            }
        )
        say(f"registered {tool} as a {event} hook on {matcher}")
        changed = True
    if changed:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return 0


def install_third_party_skills() -> int:
    """Returns the number of sources whose missing skills could not be installed."""
    failures = 0
    for source, names in THIRD_PARTY_SKILLS.items():
        missing = [n for n in names if not (AGENTS_SKILLS_DST / n).exists()]
        if not missing:
            continue
        cmd = [
            "npx",
            "-y",
            "skills",
            "add",
            source,
            "-g",
            "-y",
            "-s",
            *missing,
            "-a",
            "claude-code",
            "codex",
        ]
        try:
            exited = subprocess.run(
                cmd, cwd=Path.home(), stdout=subprocess.DEVNULL, check=False
            ).returncode
        except FileNotFoundError:
            say(f"FAILED {source}: npx not found -- install Node, then re-run")
            failures += 1
            continue
        # The CLI exits 0 even when the source no longer has a skill, so look for each one.
        still_missing = [n for n in missing if not (AGENTS_SKILLS_DST / n).exists()]
        if exited or still_missing:
            say(
                f"FAILED {source}: `{' '.join(cmd)}` exited {exited}, still missing: {', '.join(still_missing)}"
            )
            failures += 1
        else:
            say(f"installed {', '.join(missing)} from {source}")
    return failures


def enable_git_hooks() -> int:
    """Returns 1 when git refuses the setting."""
    cmd = ["git", "-C", str(REPO), "config", "core.hooksPath", ".githooks"]
    if subprocess.run(cmd, check=False).returncode != 0:
        say("FAILED to point git at .githooks")
        return 1
    return 0


def prune_stale_links():
    """Remove links into this repo whose target is no longer a skill or a
    tool, by the same rules link_skills and link_tools use to create them."""
    rules = [
        (SKILLS_DST, SKILLS_SRC, is_skill),
        (AGENTS_SKILLS_DST, SKILLS_SRC, is_skill),
        (BIN_DST, BIN_SRC, is_tool),
    ]
    for dst_dir, src_dir, still_valid in rules:
        if not dst_dir.is_dir():
            continue
        for dst in sorted(dst_dir.iterdir()):
            if not dst.is_symlink():
                continue
            target = dst.resolve()
            if target.parent == src_dir and not still_valid(target):
                dst.unlink()
                say(f"removed stale link {dst}")


def main():
    print("[llm-toolbox]")
    prune_stale_links()
    conflicts = link_skills() + link_tools() + link_guidelines() + register_hooks()
    failures = install_third_party_skills() + enable_git_hooks()
    if conflicts:
        say(f"{conflicts} link(s) not made -- resolve the conflicts above by hand")
    return 1 if conflicts or failures else 0


if __name__ == "__main__":
    sys.exit(main())
