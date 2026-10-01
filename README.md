# Stuff I use with AI

Skills, and the guidelines every agent session starts from.

## Developer setup

Add linting tools:
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

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

It also installs other authors' skills, listed in `THIRD_PARTY_SKILLS` in
`install.py`, with `npx skills add -g`. It installs only the missing ones and
never updates them; `npx skills update -g` does that. To add one, add its name
under its source and re-run `install.py`, which passes the flags that put the
skill in `~/.agents/skills` with a link from `~/.claude/skills`; `-a claude-code`
alone copies it into `~/.claude/skills` instead. When only some requested names
exist in the source, the `skills` CLI installs those, skips the rest without a
word and exits 0, so `install.py` checks for each skill afterwards.

The sbx sandbox runs this automatically: `sandbox-setup/setup.py` lists it in
`PROJECT_INSTALLERS`, so a sandbox recreate restores the links.

## Skills

- **coding**: the coding standards. Loads when code is about to change, so the rest
  of `GLOBAL_CLAUDE.md` stays short.
- **unslop**: cut AI tells from writing.
- **upskill**: run by hand (`/upskill`) near the end of a session. Reviews the session for
  mistakes, waste and repeated work, then pitches fixes before making any. It ends
  by suggesting Matt Pocock's `/retro`, which looks at the same session for guardrails.
- **todone**: run by hand (`/todone`) to do a coding to-do from the knowledge-dump
  vault. The commit message gets the to-do's Why and source, and the to-do gets
  ticked with the commit and what was learned doing it.

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

- **web-shot**: opens a URL or HTML file in headless Chromium, clicks and
  runs scripts in it, saves screenshots of the page or of one element, prints
  a page's text or links (for JS-rendered forms and calendars; `scroll-all`
  loads endless feeds first), and reports the page's JavaScript errors
  (exit 3), repeats collapsed. Falls back to the newest
  cached Chromium when Playwright wants a build that isn't downloaded.
- **sofa**: Stack Overflow for Agents from the command line: search, read,
  post, reply, edit, vote, verify, Playbooks, and a raw `api` call. Reads the
  API key from `$SOFA_API_KEY` or the nearest `.sofa/credentials.json` and
  never prints it; caches the session in `~/.cache/sofa/`.
- **claude-json**: one headless Claude call for scripts and cron jobs. Prompt
  on stdin, JSON Schema as a flag, the answer as JSON on stdout. It always
  bills the Claude subscription (it drops `ANTHROPIC_API_KEY`), and by
  default gives the model no tools, settings, hooks, MCP servers or
  CLAUDE.md, since prompts often carry untrusted text. `--tools` opts named
  tools back in.
- **agent-commit-gate**: a Claude Code hook, registered by `install.py` in
  `~/.claude/settings.json`. Before the agent runs `git commit`, it runs the
  repo's `agent-pre-commit`, an executable kept beside the ordinary
  pre-commit hook, and blocks the commit if it fails. People keep the quick
  hook; the agent also gets the slow checks, such as the whole test suite.
  `agent-commit-gate --run` runs a repo's check by hand.

- **yt-transcript**: prints a YouTube video's captions, or the ones around
  the URL's timestamp (90 seconds before, 60 after, by default), each line
  prefixed with its clock time. For finding what a video said at a moment
  someone bookmarked. Caches each transcript, and exits 3 when YouTube
  refuses the request (it blocks IPs that fetch a lot), so callers can retry later.

## Checks

```bash
python3 -m ruff check . && python3 -m ruff format --check . && python3 -m pytest
```

`install.py` points git at `.githooks`. Its `pre-commit` runs the two ruff checks on
what's staged; `agent-pre-commit` adds the tests (about 35 seconds), and
`agent-commit-gate` runs it before an agent's commits. Both need ruff for
whichever `python3` is on `PATH` (`pip install ruff`). `ruff.toml` brings the
extensionless scripts in `bin/` into both ruff checks.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
