# Stuff I use with AI

Skills, and the guidelines every agent session starts from.

## Developer setup

Install [uv](https://docs.astral.sh/uv/), then point git at the hooks:

```bash
git config core.hooksPath .githooks
```

`install.py` below does this too. The hooks take ruff and pytest at the
versions in `requirements-dev.txt` from `uv run`, which keeps them in uv's
cache. There's no `.venv`: this repo is shared between the host and a sandbox,
and a venv works for only one machine's Python.

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
`install.py`, with `npx skills add -g`. It installs only the missing ones, and
updates them only with `--update`. It also installs a weekly cron job (Mondays
08:17) that runs `skills-news`, which emails what changed upstream; read that,
then run `python3 install.py --update`. An update replaces a skill whose
upstream changed, along with any hand edits to it, so keep those edits as
patches in `skill-patches/` (see `skill-patch`): `install.py` puts each one
back on after installing or updating, and reports one that no longer fits.
To add one, add its name
under its source and re-run `install.py`, which passes the flags that put the
skill in `~/.agents/skills` with a link from `~/.claude/skills`; `-a claude-code`
alone copies it into `~/.claude/skills` instead. When only some requested names
exist in the source, the `skills` CLI installs those, skips the rest without a
word and exits 0, so `install.py` checks for each skill afterwards.

The sbx sandbox runs this automatically: `sandbox-setup/setup.py` lists it in
`PROJECT_INSTALLERS`, so a sandbox recreate restores the links.

## Skills

- **coding**: how to code: priorities, tests, commits, merge danger. Loads when
  code is about to change, so the rest of `GLOBAL_CLAUDE.md` stays short. Its
  `standards.md` holds the quality rules, which apply at review, not while
  implementing.
- **build**: carries a code change through. The agent makes it work,
  `/code-review` subagents check it against `coding/standards.md`, and the agent
  triages the findings and refactors. It asks before handing a large change to
  an implementer subagent.
- **unslop**: cut AI tells from writing.
- **upskill**: run by hand (`/upskill`) near the end of a session, or on an
  earlier one. Reviews it for mistakes, waste, repeated work and missing
  guardrails, subagents included, then pitches fixes before making any.
- **todone**: run by hand (`/todone`) to do a coding to-do from the knowledge-dump
  vault. The commit message gets the to-do's Why and source, and the to-do gets
  ticked with the commit and what was learned doing it.

## Tools

- **safekill** and **waitone**: stop processes, or wait for one, found by name
  or command line. Unlike `pkill -f` and `pgrep -f`, they never match the
  calling shell, whose command line holds the pattern. `safekill --one` acts
  only when exactly one process matches; `safekill --all` signals every match.
  `waitone --pid` also waits for processes that are not the caller's children.
- **gui-shot**: runs a GUI app on a headless display (Xvfb, or weston for
  Wayland), clicks and types into it, and saves screenshots. For checking a
  desktop app's UI from a sandbox with no screen. `gui-shot -h` lists the
  actions. Prints the apt command if Xvfb, xdotool, ImageMagick or weston is
  missing.

- **new-egui-app**: starts a Rust egui desktop app for GNOME on Wayland
  from `templates/egui-app`: glow renderer, app id matched to the `.desktop`
  entry, window icon, startup timing line, an egui_kittest test, git hooks,
  CI, a per-user install script, and a CLAUDE.md with the known gotchas.
  `new-egui-app DIR [--name CRATE] [--title TITLE]`; it runs `git init` but
  leaves the first commit to you.

- **web-shot**: opens a URL or HTML file in headless Chromium, clicks and
  runs scripts in it, saves screenshots of the page or of one element, prints
  a page's text or links (for JS-rendered forms and calendars; `scroll-all`
  loads endless feeds first), and reports the page's JavaScript errors
  (exit 3), repeats collapsed. Falls back to the newest
  cached Chromium when Playwright wants a build that isn't downloaded.
- **sofa**: Stack Overflow for Agents from the command line: search, read,
  post, reply, edit, vote, verify, Playbooks, and a raw `api` call. Reads the
  API key from `$SOFA_API_KEY`, `$SOFA_CREDENTIALS` or the nearest
  `.sofa/credentials.json` and
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

- **skill-patch**: keeps hand edits to other authors' skills. Edit the
  installed copy in `~/.agents/skills/NAME`, then `skill-patch save NAME
  --why "..."` writes `skill-patches/NAME.patch`, headed by its Why.
  `skill-patch apply` puts the patches back on; `install.py` runs it. When an
  upstream change makes a patch no longer fit, redo the edit on the new
  version and save again.

- **skills-news**: fetches the sources in `install.py`'s `THIRD_PARTY_SKILLS`,
  diffs each installed skill against upstream, and reports which changed
  upstream (summarized by Claude, with file stats beside each summary), which
  were edited by hand here, and which skills a source added since the last
  run. A skill's patch is taken off before comparing, and the report says
  whether it still fits a new upstream version. `--email TO` sends the report only when there are upstream changes or
  new skills. Clones go in `$XDG_CACHE_HOME/skills-news` (default
  `~/.cache/skills-news`). The weekly cron job from `install.py` runs it; its
  log is `~/.cache/skills-news/cron.log`.

- **session-digest**: condenses a Claude Code session log, and each of its
  subagents' logs, for a retrospective. Prints what each agent and tool cost
  (calls, errors, estimated tokens of tool output, API tokens in and out)
  and its five biggest tool outputs,
  then each agent's timeline: user turns, the assistant's text, one line per
  tool call with its output's size, one line per background task notification
  (id, status, summary), and every error in full. Defaults to the
  newest session for the current directory (or its nearest parent with sessions),
  under `$CLAUDE_CONFIG_DIR` (default `~/.claude`).

- **yt-transcript**: prints a YouTube video's captions, or the ones around
  the URL's timestamp (90 seconds before, 60 after, by default), each line
  prefixed with its clock time. For finding what a video said at a moment
  someone bookmarked. Caches each transcript in `claudedev/cache/yt-transcript/`
  (`$YT_TRANSCRIPT_CACHE` overrides), and exits 3 when YouTube refuses the
  request (it blocks IPs that fetch a lot), so callers can retry later.

- **paper**: a paper's metadata (`meta`), the papers citing it (`cites`) and
  keyword search (`search`) from Semantic Scholar, and its full text (`text`:
  downloads the arXiv or direct-URL PDF, runs `pdftotext -layout` and prints
  the `.txt` path). Takes arXiv ids and URLs, DOIs and Semantic Scholar
  paperIds, and prints a few lines per paper. Retries rate limits, 5xx and
  timeouts with backoff for up to 2 minutes, so callers needn't sleep and
  retry. Sends a Semantic Scholar API key from `$S2_API_KEY`, else from
  `claudedev/.semantic-scholar/api-key`, when there is one. Caches everything
  in `claudedev/cache/paper/` (`$PAPER_CACHE` overrides). Exits 1 when not found or when a URL is not a PDF, 3 when the
  remote still refuses.

## Checks

```bash
.githooks/agent-pre-commit
```

It checks what's staged. `install.py` points git at `.githooks`. Its `pre-commit` runs the two ruff checks on
what's staged; `agent-pre-commit` adds the tests (about 35 seconds), and
`agent-commit-gate` runs it before an agent's commits. Both run the tools
through `uv run`, so they need uv on `PATH`. `ruff.toml` brings the
extensionless scripts in `bin/` into both ruff checks.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
