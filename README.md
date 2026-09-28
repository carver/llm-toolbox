# Stuff I use with AI

Skills, and the guidelines every agent session starts from.

## Install

```bash
python3 install.py
```

Symlinks every executable in `bin/` into `~/.local/bin/`, and every directory
under `skills/` into `~/.claude/skills/`, where Claude
Code picks them up as global skills, and into `~/.agents/skills/`, the AGENTS.md
convention's home for them, and `GLOBAL_CLAUDE.md` to `~/.claude/CLAUDE.md`
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
- **retro**: run by hand (`/retro`) near the end of a session. Reviews the session for
  mistakes, waste and repeated work, then pitches fixes before making any.

## Tools

- **killone** and **waitone**: stop, or wait for, exactly one process found
  by name or command line. Unlike `pkill -f` and `pgrep -f`, they never match
  the calling shell, whose command line holds the pattern. `waitone --pid`
  also waits for processes that are not the caller's children.
- **gui-shot**: runs a GUI app on a headless display (Xvfb, or weston for
  Wayland), clicks and types into it, and saves screenshots. For checking a
  desktop app's UI from a sandbox with no screen. `gui-shot -h` lists the
  actions. Prints the apt command if Xvfb, xdotool, ImageMagick or weston is
  missing.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
