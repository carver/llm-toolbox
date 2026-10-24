#!/usr/bin/env python3
"""Link this repo into the home directory: every skill into ~/.claude/skills and
~/.agents/skills as a global skill, every tool in bin/ into ~/.local/bin, and
GLOBAL_CLAUDE.md to ~/.claude/CLAUDE.md and ~/.agents/AGENTS.md, one set of
guidelines whichever convention a tool reads. Registers the Claude Code hooks in
HOOKS and the values in SETTINGS_VALUES in ~/.claude/settings.json. Installs the other authors' skills in
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
Settings values: set when missing; one already set to something else is
reported and left alone.
Third-party skills: only missing ones are installed; it never removes one,
and updates them only with --update, which then prints what the update
changed in each skill (bin/skills-news --since). Installs a weekly cron job that runs
bin/skills-news, which emails what changed upstream, so you know when an
--update is worth running.

    python3 install.py [--update]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "lib"))
import thirdparty

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
        {"timeout": 10},
    ),
    (
        "PreToolUse",
        "Bash",
        "edit-script-gate",
        {"timeout": 10},
    ),
    (
        "PostToolUse",
        "Bash",
        "issue-view-hint",
        {
            "timeout": 10,
            "if": "Bash(gh issue *)",
        },  # starts the script only for gh issue
    ),
]
# (key path, value, why). Claude Code reads these only at user scope.
SETTINGS_VALUES = [
    (
        ["pluginConfigs", "cc-plugin-agents-md@builtin", "options", "instructionFiles"],
        "claude-md-and-agents-md",
        "read a repo's AGENTS.md even though the workspace has a CLAUDE.md",
    ),
]
# Other authors' skills, by GitHub source. Installed into ~/.agents/skills with a
# link from ~/.claude/skills, the layout `npx skills add -g` gives Claude Code and Codex.
THIRD_PARTY_SKILLS = {
    "humanlayer/skills": ["show-me"],
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
        "improve-codebase-architecture",
        "pr",
        "prototype",
        "research",
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
CRON_MARKER = "# llm-toolbox"
CRON_LINES = [
    (
        "17 8 * * 1 bash -lc 'mkdir -p ~/.cache/skills-news && skills-news"
        " --email ADDRESS-REMOVED >> ~/.cache/skills-news/cron.log 2>&1'"
        f" {CRON_MARKER} weekly skills-news"
    ),
]


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
        # resolve() on a dangling link still gives the intended target. Both sides are
        # resolved because a tool may itself be a link to another (claude-json).
        if dst.resolve() == src.resolve():
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


def load_settings(what: str) -> dict | None:
    """None, after reporting it, when the settings file is not valid JSON."""
    try:
        return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    except json.JSONDecodeError:
        say(f"CONFLICT {SETTINGS} is not valid JSON -- {what}")
        return None


def save_settings(settings: dict):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")


def register_hooks() -> int:
    """Returns 1 when the settings file cannot be read as JSON."""
    settings = load_settings("hooks not registered")
    if settings is None:
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
        save_settings(settings)
    return 0


def set_settings() -> int:
    """Returns the number of values not set: a broken settings file, or a value set by hand."""
    settings = load_settings("settings values not set")
    if settings is None:
        return 1
    conflicts = 0
    changed = False
    for keys, value, why in SETTINGS_VALUES:
        parent = settings
        for key in keys[:-1]:
            parent = parent.setdefault(key, {})
        name = ".".join(keys)
        if keys[-1] not in parent:
            parent[keys[-1]] = value
            say(f"set {name} = {value!r}, to {why}")
            changed = True
        elif parent[keys[-1]] != value:
            say(f"CONFLICT {name} is {parent[keys[-1]]!r}, not {value!r} -- left alone")
            conflicts += 1
    if changed:
        save_settings(settings)
    return conflicts


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


def update_third_party_skills() -> int:
    """Returns 1 when the update fails, plus the number of skill patches that no
    longer fit. `npx skills` replaces a skill only when its upstream folder
    changed, overwriting any hand edits to it; the patches then go back on.
    Prints what the update changed in each skill (bin/skills-news --since)."""
    names = [n for names in THIRD_PARTY_SKILLS.values() for n in names]
    cmd = ["npx", "-y", "skills", "update", "-g", "-y", *names]
    with tempfile.TemporaryDirectory(prefix="skills-before-update-") as before:
        for name in names:
            if (thirdparty.INSTALLED / name).is_dir():
                shutil.copytree(
                    thirdparty.INSTALLED / name, Path(before) / name, symlinks=True
                )
        try:
            exited = subprocess.run(cmd, cwd=Path.home(), check=False).returncode
            problem = f"`{' '.join(cmd)}` exited {exited}" if exited else ""
        except FileNotFoundError:
            problem = "npx not found -- install Node, then re-run"
        conflicts = apply_skill_patches()
        if problem:
            say(f"FAILED update: {problem}")
            return 1 + conflicts
        # Both sides carry the patches, so the report shows the upstream change.
        report = [str(BIN_SRC / "skills-news"), "--since", before]
        try:
            reported = subprocess.run(report, check=False).returncode == 0
        except OSError:
            reported = False
        if not reported:
            say(
                f"the update worked, but `{' '.join(report)}` had a problem (see above)"
            )
        return conflicts


def merged_crontab(current: str) -> str:
    """`current` with this repo's lines replaced by CRON_LINES, others kept."""
    kept = [line for line in current.splitlines() if CRON_MARKER not in line]
    return "\n".join([*kept, *CRON_LINES]) + "\n"


def install_cron() -> int:
    """Returns 1 when crontab refuses the new table."""
    try:
        listed = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        say("FAILED cron: no crontab command")
        return 1
    current = listed.stdout if listed.returncode == 0 else ""
    wanted = merged_crontab(current)
    if wanted == current:
        return 0
    written = subprocess.run(
        ["crontab", "-"], input=wanted, text=True, capture_output=True, check=False
    )
    if written.returncode:
        say(f"FAILED cron: {written.stderr.strip()}")
        return 1
    say("installed the weekly skills-news cron job")
    return 0


def apply_skill_patches() -> int:
    """Put skill-patches/ back on the skills they patch, after an install or
    update replaced them. Returns the number that no longer fit."""
    conflicts = 0
    for patch in sorted(thirdparty.PATCHES.glob("*.patch")):
        skill = thirdparty.INSTALLED / patch.stem
        if not skill.is_dir():
            continue
        status = thirdparty.apply(skill, patch)
        if status == "applied":
            say(f"patched {patch.stem}")
        elif status == "conflict":
            say(
                f"CONFLICT {patch.stem}: {patch.name} no longer fits the new version; "
                f"redo the edit on {skill}, then `skill-patch save {patch.stem}`"
            )
            conflicts += 1
    return conflicts


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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="also update the third-party skills whose upstream changed",
    )
    args = parser.parse_args()
    print("[llm-toolbox]")
    prune_stale_links()
    conflicts = (
        link_skills()
        + link_tools()
        + link_guidelines()
        + register_hooks()
        + set_settings()
    )
    failures = install_third_party_skills() + enable_git_hooks() + install_cron()
    failures += update_third_party_skills() if args.update else apply_skill_patches()
    if conflicts:
        say(f"{conflicts} link(s) not made -- resolve the conflicts above by hand")
    return 1 if conflicts or failures else 0


if __name__ == "__main__":
    sys.exit(main())
