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

It also sets the user-scope Claude Code settings in `install.py`'s
`SETTINGS_VALUES`, such as loading a repo's `AGENTS.md` alongside the
workspace `CLAUDE.md` (by default Claude Code skips `AGENTS.md` when any
`CLAUDE.md` is found). A value already set to something else is reported and
left alone.

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
- **build**: carries a code change through. The agent makes it work, a review
  subagent checks it on `/code-review`'s two axes, against
  `coding/standards.md`, and the agent
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

- **safekill**, **waitone** and **safeps**: stop processes, wait for one, or
  list them, found by name or command line. Unlike `pkill -f` and `pgrep -f`,
  they never match the calling shell, whose command line holds the pattern.
  `safekill --one` acts only when exactly one process matches; `safekill --all`
  signals every match. `waitone --pid` also waits for processes that are not
  the caller's children. `safeps` prints each match's PID, the time since it
  started and its command line, shortened around the match. Without `-f` the
  pattern is the exact process name; when that matches nothing but `-f` would,
  they list what `-f` would match.
- **bgrun**: `bgrun -o LOG -- CMD` starts CMD in the background, in its own
  session with stdin from /dev/null and its output in LOG, and prints CMD's
  own PID. `$!` after `cd DIR && nohup CMD &` is a subshell's PID, and killing
  it leaves CMD running. `-C DIR` sets CMD's directory, `--append` keeps LOG.
- **apply-edits**: replaces old text with new across files, all or nothing,
  from `<<<<<<<` / `=======` / `>>>>>>>` blocks under `--- PATH` lines on
  stdin. Each old text must occur exactly once; on a miss nothing is written
  and the closest text in the file is shown as a diff. For edits that would
  otherwise be a heredoc Python script. `apply-edits -h` shows the format.
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

- **web-shot**: opens a URL or HTML file in headless Chromium (with
  `--cookie NAME=VALUE` for an app that keeps your login in a cookie), clicks and
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
- **claude-fresh**: one headless call to a fresh Claude, for scripts and cron
  jobs. Prompt on stdin; `--json SCHEMA` prints the schema-shaped answer as
  JSON, `--text` the reply's text. It always bills the Claude subscription (it
  drops `ANTHROPIC_API_KEY`), and by default gives the model no tools,
  settings, hooks, MCP servers or CLAUDE.md, since prompts often carry
  untrusted text. `--tools` opts named tools back in. A refused call says why,
  including the reason `claude -p` gives only in its JSON output.
  `claude-json` is a link to it that keeps the old `-s SCHEMA` form.
- **agent-commit-gate**: gives agents' commits a repo's slow checks.
  `agent-commit-gate --install REPO...` adds a line to the top of the repo's
  pre-commit hook: when `$AI_AGENT` is set, as Claude Code sets it, the hook
  first runs `agent-pre-commit`, an executable kept beside it, and fails the
  commit if that fails. People keep the quick hook; the agent also gets the
  slow checks, such as the whole test suite. Git runs the hook after `commit
  -a` or `commit PATHS` updates the index, so the check sees what is
  committed. `install.py` also registers it as a Claude Code hook that
  refuses `git commit --no-verify`.
- **openrouter-credits**: prints an OpenRouter account's credits, bought, used
  and left. The key comes from `OPENROUTER_API_KEY`, else from `./.env` (or
  `--env-file`); it is never printed.
- **edit-script-gate**: a Claude Code hook, which `install.py` registers, that
  refuses a Bash command running inline Python (a heredoc or `-c`) whose code
  reads a file, `.replace()`s text and writes that same file back, and points
  to `apply-edits`. Only the Python's own code counts, and pattern-based edits
  (`re.sub`, `sed`) pass.

- **build-state**: `/build`'s bookkeeping. `start` records the base commit and
  the repo's fingerprint (every other ref, and the config with its values
  hashed), `refactor` the refactor base. `finish` folds `fixup!` commits into
  the commits they fix, leaving every other commit separate, then lists the
  commits since the base and the test files changed since the refactor base,
  and fails naming any ref or config key that moved. The state lives in the
  git dir; `show` prints it.

- **skill-patch**: keeps hand edits to other authors' skills. Edit the
  installed copy in `~/.agents/skills/NAME`, then `skill-patch save NAME
  --why "..."` writes `skill-patches/NAME.patch`, headed by its Why. A later
  edit with its own `--why` adds a layer and keeps the earlier reasons
  (`skill-patch -h`).
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
  log is `~/.cache/skills-news/cron.log`. `--since DIR` instead reports what
  changed from the skill copies in `DIR` to the installed ones: `install.py
  --update` copies the skills there first and prints this report after
  updating, so you see what each skill's update changed.

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
  the `.txt` path). `outline` lists the text's section headings with line
  numbers and `grep` its lines matching a regex, with context, so an agent
  can read a section instead of the whole paper. Takes arXiv ids and URLs, DOIs and Semantic Scholar
  paperIds, and prints a few lines per paper; `meta` adds an arXiv paper's
  latest version and its first- and latest-version dates, from one arXiv API
  request per call (cached for a day). Paces requests across every `paper`
  process sharing the cache, so a fan-out of agents stays under the limits:
  Semantic Scholar's at least 1.1 seconds apart, the arXiv API's at least 3;
  PDF downloads aren't paced. Retries rate limits, 5xx and
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
what's staged; `agent-pre-commit` adds the tests (about 50 seconds), and
`pre-commit` runs it first when an agent commits. Both run the tools
through `uv run`, so they need uv on `PATH`. `ruff.toml` brings the
extensionless scripts in `bin/` into both ruff checks.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
