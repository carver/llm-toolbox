# Stuff I use with AI

Skills, and the guidelines every agent session starts from.

## Install

```bash
python3 install.py
```

Symlinks every directory under `skills/` into `~/.claude/skills/`, where Claude
Code picks them up as global skills, and `GLOBAL_CLAUDE.md` to `~/.claude/CLAUDE.md`
and `~/.agents/AGENTS.md`, so one file serves whichever convention a tool reads.
Links, not copies, so an edit here is live straight away and `git pull` is the
whole update story.

Re-run it after adding or renaming a skill. It is idempotent and only touches
links that point into this repo. If a name already belongs to some other
skill, it reports the conflict and leaves it alone.

The sbx sandbox runs this automatically: `sandbox-setup/setup.py` lists it in
`PROJECT_INSTALLERS`, so a sandbox recreate restores the links.

## Skills

- **coding**: the coding standards. Loads when code is about to change, so the rest
  of `GLOBAL_CLAUDE.md` stays short.
- **unslop**: cut AI tells from writing.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
