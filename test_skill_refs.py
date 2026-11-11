"""Every skill the guidelines, skills and patches name is one install.py installs."""

import re
from pathlib import Path

import install

ROOT = Path(__file__).resolve().parent
OWN = {p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()}
INSTALLED = OWN | {n for names in install.THIRD_PARTY_SKILLS.values() for n in names}
# Claude Code's own slash commands, which docs may name beside skills.
BUILTIN = {"clear", "compact", "config", "help", "init", "memory", "resume"}

SLASH = re.compile(r"`/([a-z][a-z0-9-]*)`")
NAMED = re.compile(r"the `([a-z][a-z0-9-]*)` skill")
PATH = re.compile(r"~/\.agents/skills/([a-z][a-z0-9-]*)(/[\w./-]*\w)?")


def broken_refs(text: str) -> list[str]:
    broken = [
        f"/{name}"
        for name in SLASH.findall(text) + NAMED.findall(text)
        if name not in INSTALLED | BUILTIN
    ]
    for name, rest in PATH.findall(text):
        if name not in INSTALLED:
            broken.append(f"~/.agents/skills/{name}")
        elif rest and name in OWN and not (ROOT / "skills" / name / rest[1:]).exists():
            broken.append(f"~/.agents/skills/{name}{rest}")
    return broken


def without_removed_lines(patch: str) -> str:
    """A patch's text minus the upstream lines it removes, which this repo doesn't name."""
    return "".join(
        line
        for line in patch.splitlines(keepends=True)
        if not line.startswith("-") or line.startswith("---")
    )


def refs_text(doc: Path) -> str:
    text = doc.read_text()
    return without_removed_lines(text) if doc.suffix == ".patch" else text


def docs() -> list[Path]:
    return [
        ROOT / "GLOBAL_CLAUDE.md",
        ROOT / "README.md",
        *sorted((ROOT / "skills").rglob("*.md")),
        *sorted((ROOT / "skill-patches").glob("*.patch")),
    ]


def test_finds_each_kind_of_broken_ref() -> None:
    text = (
        "Run `/implement`, then the `nope` skill. Read "
        "~/.agents/skills/coding/missing.md and ~/.agents/skills/gone/SKILL.md, "
        "but `/build`, the `tdd` skill and ~/.agents/skills/coding/standards.md are fine."
    )
    assert broken_refs(text) == [
        "/implement",
        "/nope",
        "~/.agents/skills/coding/missing.md",
        "~/.agents/skills/gone",
    ]


def test_a_patch_s_removed_lines_are_not_refs() -> None:
    patch = (
        "--- a/SKILL.md\n"
        "+++ b/SKILL.md\n"
        "@@ -1,3 +1,3 @@\n"
        " Then run `/build`.\n"
        "-Write it to `/tmp`, then run `/retro`.\n"
        "+Publish it, then run `/upskill`.\n"
    )
    assert without_removed_lines(patch) == (
        "--- a/SKILL.md\n"
        "+++ b/SKILL.md\n"
        "@@ -1,3 +1,3 @@\n"
        " Then run `/build`.\n"
        "+Publish it, then run `/upskill`.\n"
    )


def test_every_named_skill_is_installed() -> None:
    broken = {
        str(doc.relative_to(ROOT)): refs
        for doc in docs()
        if (refs := broken_refs(refs_text(doc)))
    }
    assert broken == {}
