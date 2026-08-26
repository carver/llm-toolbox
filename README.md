# Stuff I use with AI

Currently just skills. Maybe that's all it will ever be?

## Install

```bash
python3 install.py
```

Symlinks every directory under `skills/` into `~/.claude/skills/`, where Claude
Code picks them up as global skills. Links, not copies, so an edit here is live
straight away and `git pull` is the whole update story.

Re-run it after adding or renaming a skill. It is idempotent, it only touches
links pointing into this repo, and it reports (rather than clobbers) a name
that already belongs to some other skill.

The sbx sandbox runs this automatically: `sandbox-setup/setup.py` lists it in
`PROJECT_INSTALLERS`, so a sandbox recreate restores the links.

## Skills

- **unslop** - cut AI tells from writing.
