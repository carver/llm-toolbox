# Stuff I use with AI

Currently just skills. Maybe that's all it will ever be?

## Install

```bash
python3 install.py
```

Symlinks every directory under `skills/` into `~/.claude/skills/`, where Claude
Code picks them up as global skills. Links, not copies, so an edit here is live
straight away and `git pull` is the whole update story.

Re-run it after adding or renaming a skill. It is idempotent and only touches
links that point into this repo. If a name already belongs to some other
skill, it reports the conflict and leaves it alone.

The sbx sandbox runs this automatically: `sandbox-setup/setup.py` lists it in
`PROJECT_INSTALLERS`, so a sandbox recreate restores the links.

## Skills

- **unslop**: cut AI tells from writing.
