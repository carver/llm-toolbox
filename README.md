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
whole update story. A tool whose first line runs `uv run --script` needs uv on
`PATH`, which finds or fetches a Python 3.12 or later for it.

It also sets the user-scope Claude Code settings in `install.py`'s
`SETTINGS_VALUES`, such as loading a repo's `AGENTS.md` alongside the
workspace `CLAUDE.md` (by default Claude Code skips `AGENTS.md` when any
`CLAUDE.md` is found). A value already set to something else is reported and
left alone.

The repo's post-commit and post-merge hooks run `install.py --links` in the
main worktree, which prunes stale links and links new tools and skills, so a
tool or skill is ready once it's committed or pulled. Re-run the whole install
by hand after a fresh clone or a change to `install.py`. It is idempotent and only touches
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
  they list what `-f` would match. A match that another match started, such as
  a job and the wrapper shell that launched it, is marked "(started by PID)",
  and the refusal of `safekill --one` or `waitone` suggests anchoring the
  pattern with `^`. For the tests, `$WAITONE_PICKED` names a file waitone
  writes the PID it picked to before it waits.
- **bgrun**: `bgrun -o LOG -- CMD` starts CMD in the background, in its own
  session with stdin from /dev/null and its output in LOG, and prints CMD's
  own PID. `$!` after `cd DIR && nohup CMD &` is a subshell's PID, and killing
  it leaves CMD running. `-C DIR` sets CMD's directory. A non-empty LOG from
  an earlier run is renamed to LOG plus the time of its last write
  (`run.log.2026-10-22T17-32-50`); `--append` adds to it instead, after a
  `=== bgrun TIME: CMD` line, and `--truncate` replaces it.
  `--wait` then waits for CMD, prints its exit status and LOG's last lines
  (`--tail N`, default 10) and exits with CMD's status: run it as an agent's
  background job to get one notification when CMD ends, while CMD survives if
  the waiting bgrun is killed. CMD gets the caller's environment, without the
  `VIRTUAL_ENV` and PATH entry of the uv environment bgrun itself runs in (a
  `VIRTUAL_ENV` the caller had set is gone too: uv replaced it first).
- **apply-edits**: replaces old text with new across files, all or nothing,
  from Aider-style `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` blocks
  under `--- PATH` lines on stdin (bare markers and Aider's bare path lines
  work too). Each old text must occur exactly once; on a miss nothing is written
  and the closest text in the file is shown as a diff. In a Python file, an old
  text not found as it is still matches across formatting that doesn't change
  the program, as when `ruff format` ran after the block was written:
  whitespace Python ignores (a call ruff wrapped), trailing commas,
  parentheses that change nothing, and how a string or number is spelled
  (quotes, escapes, a u prefix, letter case). A docstring ruff trimmed or a
  comment it respaced still misses. A match never crosses a statement break,
  indentation, a comment or a value that differs, and one beyond whitespace
  needs the file, with the old text in place, to parse to the same syntax
  tree. In any other file, a miss still matches across whitespace a formatter
  such as rustfmt changes (a call split over lines, deeper indentation, a `{`
  moved) and the trailing comma it adds, never inside a string or comment, nor
  across a line break between two words or after a `;`, and every line keeps
  its depth against the first. Either kind of loose
  match must be unique, and the output names it. For edits that would otherwise
  be a heredoc Python script. `--cached` edits each file's staged copy in git's
  index instead and leaves the work tree alone, to commit part of a change: the
  blocks turn the staged text into the part to commit. It refuses a path with no
  index entry of its own, an unmerged one, a symlink or submodule, and one marked
  skip-worktree or assume-unchanged.
  `apply-edits -h` shows the format.
- **slop-lint**: flags the tells from the unslop skill that a regex can catch,
  in markdown and text files: em and en dashes, curly quotes, and fixed AI
  words and phrases (`delve`, `in order to`, `utilize`, `north star`), each
  with its plain replacement. It skips code, URLs, HTML comments and any line
  marked `slop-lint: ignore`. `slop-lint FILE...`, `-` for stdin, or
  `--commit-msg FILE` as git's commit-msg hook. Prints nothing on a pass and
  exits 1 on a finding. Repos copy it into their own `bin/` for people
  without llm-toolbox.
- **rewrap**: `rewrap FILE...` reflows each Python docstring paragraph and
  comment block that holds a line past ruff's line-length (or `--width`), and
  leaves every other line as it is; ruff format doesn't wrap prose, so E501 in a
  docstring or comment otherwise means rewrapping by hand. It lists the lines
  still too long (code, strings, kept examples) and exits 1 while any is left.
  `--check` reports without writing.
- **pipefail-lint**: `pipefail-lint FILE...` flags, in each shell script that
  sets pipefail, a pipeline stage that can stop reading its input early (`grep
  -q`, `-m`, `-l`, `head`, sed's `q`, awk's `exit` outside END, `read`,
  `cmp`): the writer may die of SIGPIPE, which fails the pipeline under
  pipefail, now and then. filedrawer's `gh api ... | grep -qx main` took the
  wrong branch in 11 of 400 runs that way, and shellcheck has no rule for it.
  One `FILE:LINE:` line per finding, and the fix: capture first, `out=$(cmd)`,
  then `grep -q ... <<<"$out"`. A pipeline whose status nothing reads (`||
  true`, `local x=$(...)`, a command's `$(...)` word, `<(...)`) is spared,
  and `# pipefail-lint: ignore` on one of its lines, or the line above,
  spares one it flags wrongly. Files that aren't shell scripts (by name or
  shebang) are skipped, so a hook can pass every staged file; this repo's
  pre-commit hook lints the staged ones that name pipefail. It parses with
  tree-sitter-bash, through `uv run --script`.
- **mutate-check**: `mutate-check -- CMD` breaks code briefly with apply-edits
  blocks from stdin (or from `--edits FILE`, for a run under bgrun, which
  gives its job no stdin), runs CMD (a test run), and puts every file back, also
  when stopped. It exits 0 when CMD failed (the tests catch the break) and 1 when it
  passed, printing one line and at most 3 of pytest's `E ` lines. It exits 3,
  inconclusive, when no test judged the break: the build failed (a Rust compile
  error, such as unused code under `-D warnings`), pytest collected nothing, a
  `cargo test` or `cargo nextest` run failed without running a test (a filter
  nextest refused or that matched nothing), or CMD ran past `--timeout` (600
  seconds by default). Stopping CMD stops its process group: SIGTERM, then
  SIGKILL after `$MUTATE_CHECK_GRACE` seconds. For checking that a test which
  passed on its first run guards what it's meant to.
- **conflicts**: `conflicts FILE` lists a file's conflict hunks, numbered, with
  each side and its marker label; `conflicts FILE N SIDE` (or `all SIDE`)
  resolves them as `ours`, `theirs`, `both` or `base`, and the triples repeat,
  for several files or several hunks of one, numbered as listed. apply-edits
  can't hold the `=======` line, so without it agents deleted marker lines by
  number. A call resolves every triple or none: arguments out of place are a
  usage error, so a word such as `all` is never opened as a file. It refuses a
  hunk that never closes, lacks its separator or nests, and a closing or base
  marker outside a hunk; treats a separator outside a hunk as content (a
  reStructuredText underline), as it does a hunk the merge's inputs already
  held (a test fixture's); reads the marker length from `conflict-marker-size`;
  and never stages the file. `conflicts --continue` finishes the stopped
  rebase, cherry-pick, merge, revert or am: while a file git has unmerged, or
  one staged by hand since the stop, would be committed with a hunk (it stages
  them in a copy of the index to see), it names each with its count and stages
  nothing, and so it does for a conflict git wrote no markers into (a deleted
  side, binary), which is yours to `git add` or `git rm`. Otherwise it stages
  them and runs the continue with `GIT_EDITOR=true`, then lists the next stop's
  conflicts. An agent had chained `git add` and
  `git rebase --continue` after a resolve that failed, and committed the
  markers.
- **wt-ship**: ships a worktree's commits. In a repo that pushes, it rebases
  onto the remote branch (the main tree's, by default), refuses leftover
  conflict markers, refuses a commit message that names, by its ID, a commit
  the branch doesn't hold (7 to 64 hex digits that resolve to a commit here,
  but not on the line `git cherry-pick -x` adds when a branch no linked
  worktree has out, a tag or a remote-tracking ref holds the source),
  since a rebase changes an unlanded commit's ID (`build-state reword SHA`
  rewords it), and checks every commit it would push with the repo's
  `agent-pre-commit`, through `agent-commit-gate --run`, which hands each the
  `CHECK_BASE` of the commit before or the main branch's tip: each commit with
  no pass on record, oldest first, checked out in turn in the worktree, first
  outside `heavy`'s queue (`--no-queue`), so a docs-only commit never waits,
  then those that need the queue in one `heavy` job, at the queue's front when
  it lands under the main tree's claim, so no other job comes between them. The
  first failure stops it, naming the commit to fix with a fixup. While the work
  tree holds an earlier commit, a marker in
  the worktree's git dir makes `agent-commit-gate` refuse commits there, and a
  run killed before it put the tip back leaves the next one naming the tip. A
  repo that keeps the check in its tree fails a commit that drops it, or keeps a
  link to it that leads to no executable file in that commit. Then it
  pushes with one retry when another push wins the race, and confirms the
  remote holds HEAD. Then it prints the range, a notice for other sessions and
  whether the main tree is behind; `--remove` removes the worktree, and `-C DIR`
  runs it on the worktree DIR from outside, so the call's shell isn't left in
  the folder it removed. In a repo
  that doesn't push, `--local` lands the commits on the main tree's branch
  instead: it rebases before it waits, checking nothing, then claims the main
  tree with the `CLAUDE_WORKING` flag, recording `CLAUDE_CODE_SESSION_ID`, the
  host and its PID, rebases again if the branch moved meanwhile, checks each
  commit once under the claim, and fast-forwards the main tree; a failing
  check releases the claim. Should the branch move under the claim anyway,
  from the owner or a tool that ignores the flag, it rebases and checks again,
  up to 10 times. While another session holds the flag it waits its
  turn in a queue in the git dir, for up to `$WT_SHIP_WAIT` seconds behind one
  holder (30 minutes, starting again when the holder changes; `--no-wait`
  stops at once), looking every `$WT_SHIP_POLL` seconds, and clears a flag or entry
  whose process on this host has ended. A wait of `$FIFO_LOCK_SECONDS` (5) for
  the queue folder's lock names the process holding it, and how to continue it
  when it's stopped. It stops before any check on uncommitted changes, or
  untracked files, in the main tree that touch the same files.
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
  `--cookie NAME=VALUE` for an app that keeps your login in a cookie, and
  `--color-scheme dark` or `--reduced-motion` for what the page's
  `prefers-color-scheme` and `prefers-reduced-motion` media queries see), clicks
  and runs scripts in it (`eval` prints the value as JSON), saves screenshots
  of the page or of one element (a shot over 1500px tall also as pieces of at
  most 1400px), prints a page's text or links (for JS-rendered forms and
  calendars; `scroll-all` loads endless feeds first), and reports the page's
  JavaScript errors (exit 3), repeats collapsed; a page that doesn't load or an
  action that fails gets one line and exit 1. It says on stderr when the
  page scrolls sideways, naming the outermost elements that widen it. A local
  HTML file without a doctype, such as an Artifact file, loads after the
  skeleton its publisher adds, not in quirks mode. Falls back to the newest
  cached Chromium when Playwright wants a build that isn't downloaded.
- **sofa**: Stack Overflow for Agents from the command line: search, read,
  post, reply, edit, vote, verify, Playbooks, your own posts (`mine`, with
  `--grep`), and a raw `api` call. A search that finds nothing says `no
  results`. Reads the
  API key from `$SOFA_API_KEY`, `$SOFA_CREDENTIALS` or the nearest
  `.sofa/credentials.json` and
  never prints it; caches the session in `$XDG_CACHE_HOME/sofa/`
  (`~/.cache/sofa/` by default). `$SOFA_BASE_URL`
  points it at another server, for tests; `$SOFA_CLIENT_NAME` and
  `$SOFA_MODEL_NAME` set the names each request declares.
- **claude-fresh**: one headless call to a fresh Claude, for scripts and cron
  jobs. Prompt on stdin; `--json SCHEMA` prints the schema-shaped answer as
  JSON, `--text` the reply's text. It always bills the Claude subscription (it
  drops `ANTHROPIC_API_KEY`), and by default gives the model no tools,
  settings, hooks, MCP servers or CLAUDE.md, since prompts often carry
  untrusted text. `--tools` opts named tools back in. A refused call says why,
  including the reason `claude -p` gives only in its JSON output.
  `claude-json` is a link to it that keeps the old `-s SCHEMA` form.
- **pre-commit-run**: runs the pre-commit framework from a repo's pre-commit
  hook. A person sees its output as it runs; an agent (`AI_AGENT` set) sees it
  only on a failure, and every refusal ends with `COMMIT REFUSED`. It refuses
  first when `.pre-commit-config.yaml` has unstaged changes, which pre-commit
  won't run with (`PRE_COMMIT_VERSION` picks the version). For an agent, when a
  formatter hook rewrites staged files that had no unstaged changes, it stages
  them and runs pre-commit once more, so a formatting refusal doesn't cost a
  retry. `pre-commit-run --install REPO` copies it into `REPO/bin/` and adds its
  call to the repo's hook, for a new project or an old one.
- **agent-commit-gate**: gives agents' commits a repo's slow checks.
  `agent-commit-gate --install REPO...` puts a line first in the repo's
  pre-commit hook: when `$AI_AGENT` is set, as Claude Code sets it, the line
  runs the rest of the hook, then `agent-pre-commit`, an executable kept
  beside it, and fails the commit if either fails. People keep the quick hook;
  the agent also gets the slow checks, such as the whole test suite. The check
  starts once the hook's formatters have rewritten and staged what they
  change, and git runs the hook after `commit -a` or `commit PATHS` updates
  the index, so the check sees what is committed. The line reruns the whole
  hook to do this, so `--install` moves it to the top when a command sits
  above it, and replaces an older line. The trees that passed go on a record
  in the repo's common git dir, which every worktree shares, keyed by the tree
  and the check's bytes; a pass with unstaged changes to tracked files, before
  the check or after it, isn't recorded. A check that changes what's staged,
  itself or through a `git add` while it waits its turn, refuses the commit:
  it didn't check what git would commit. When the tree
  being checked is HEAD's own and on the record, as in an amend that changes
  only the message, the check is skipped. Otherwise the check gets
  `$CHECK_BASE`, the nearest of HEAD's last 100 first-parent commits whose tree
  passed it, and may run only what the changes since need: `git diff --cached
  --name-only "$CHECK_BASE"` lists them at commit time and on a `wt-ship`
  recheck. A check that ignores it runs in full, as does
  any with no base found; `agent-quick-check` gets the same base. An agent
  without llm-toolbox, such as a teammate's, runs the check every time.
  No check the gate runs gets a variable whose name marks a secret (a
  `_`-separated part that is KEY, TOKEN, SECRET, PASSWORD or PASS, in any
  case, as in GH_TOKEN), nor any name the file `$AGENT_SECRETS_FILE` names
  exports (its `export` commands, read without running it; nothing more when
  it's unset or unreadable), nor BASH_ENV, through which bash would source them
  back, since a test's failure message once printed a secret's value;
  `heavy -- CMD` keeps them for a job that needs one. The check gets
  `AGENT_SECRETS_FILE` set to empty, which tells a login or interactive bash
  it starts to source no secrets file (sandbox-setup's secrets block reads it
  so).
  The check's output shows only when
  it fails, ending with `COMMIT REFUSED`; a pass prints one line. A small
  fixup!, squash! or amend! commit (found from the running `git commit`'s
  arguments) runs `agent-quick-check` instead, when the repo keeps one beside
  `agent-pre-commit`, and otherwise skips the check, since `wt-ship` checks
  each commit it lands with no pass on record. Small means at most 20 lines
  changed, in
  files it only modifies in place (no mode change, no binary, symlink or
  submodule), none of them a test: a lint or syntax repair. Any
  other fix commit runs the full check. A repo's `agent-quick-check` holds only
  its fast static checks, such as a linter or a type checker, and no tests. A
  repo that keeps an `agent-commit-check` beside them runs it at each commit
  that isn't a small fix, in the full check's place, with its passes on a record of their own,
  so `wt-ship` runs the full check on each such commit when it lands; the line
  marks a commit's run with `AGENT_CHECK_AT_COMMIT=1`, and a hook on an older
  line runs the full check, as before.
  `install.py` also registers it as a Claude Code hook that refuses `git commit
  --no-verify`, except in a repo with no agent check, such as a scratch repo.
  The check runs once it's its turn in `heavy`'s queue, whose waiting lines
  print as they come. A check whose change may need no build or test, such as
  filedrawer's for a docs-only commit, can skip the queue by naming
  `$CHECK_QUEUE_EXIT` in its file: it then runs first outside the queue, on one
  core with `CARGO_BUILD_JOBS=1`, with that variable set to 75. Before it starts
  a build or a test it exits 75, printing why, and waits its turn to run again
  in the queue; any other exit is its outcome. This holds for
  `agent-quick-check` too, which otherwise runs outside the queue.
- **heavy**: `heavy -- CMD` runs a CPU-heavy command once it's its turn and its
  cores are quiet. Every session on the VM shares two lanes, each running one
  job at a time at full priority, first come first served: `heavy --long` jobs
  on the last quarter of the cores, normal ones on the rest while a long job
  runs or waits and on all of them otherwise (with fewer than 4 cores, long
  jobs share the normal lane). A lane's head starts once its cores stay under
  half busy for `$HEAVY_QUIET_SECONDS` (10), sampled every `$HEAVY_POLL`
  seconds, or at the first quiet sample when it arrived to a free lane; then
  CMD runs in the foreground with the terminal, pinned to those cores
  (`CARGO_BUILD_JOBS` to match), and heavy exits with its status. With
  `--share`, a job can start beside a lane's holder at nice 10: the first such
  waiter whose usual cores fit beside the holder's in three quarters of the
  cores, or any once the holder's cores have averaged under 30% busy for
  `$HEAVY_SHARE_SECONDS` (90), with 4 GiB of memory free either way. A sharer
  can run several times slower than alone, so `--share` suits work without
  wall-clock limits, such as a build; many test suites have them, from the
  runner (pytest-timeout, nextest's slow-timeout) or in the tests, and commit
  checks never share. A command's usual cores come from its last runs that
  exited 0, shared by every worktree of its repo. Nothing shares beside a
  `heavy --alone` job, for timings. `heavy --quick` marks seconds of work, such
  as a few tests after an edit, on trust: when the normal lane's cores average
  under `$HEAVY_QUICK_BUSY` (0.25) busy over `$HEAVY_QUICK_SECONDS` (2) and
  4 GiB of memory are free, it runs at once outside the queue, whoever holds
  the lane (but never beside `--alone`), first in line for the OOM killer;
  otherwise it waits at the queue's front, after earlier front entries such as
  a landing's checks and ahead of every other waiter, until the
  cores go that quiet or it heads the lane, where it starts at the first quiet
  sample. One that ran past `$HEAVY_QUICK_MAX` (60) seconds hears, as it ends,
  to drop `--quick` for runs like it. It goes with none of `--long`, `--alone`
  and `--share`, and may run as a foreground Bash call, with a 600000 ms
  timeout; heavy-gate refuses the call without it. Stopped, it stops CMD's
  process group,
  with SIGKILL after `$HEAVY_GRACE` seconds. `heavy status` shows each lane's
  holder, sharer, waiters, their usual cores and how busy the lane's cores
  are. A third job in a lane, or a head stalled for `$HEAVY_STALL_SECONDS`
  (600), gets a hint to push a topic branch and open a PR for CI's results
  instead. A heavy inside a job (`$HEAVY_SLOT`), or inside a `--quick` run
  outside the queue, runs at once. The queue and
  its history live in `$HEAVY_DIR`
  (`/tmp/heavy`). A Ctrl-Z while heavy holds the queue's lock, a moment at each
  look, stops it once it lets go; a wait of `$FIFO_LOCK_SECONDS` (5) for that lock
  names the process holding it, and how to continue it when it's stopped. For
  the tests, `$HEAVY_BUSY` names a file that stands in for how busy the cores
  are, `$HEAVY_MEMINFO` and `$HEAVY_VMSTAT` ones for `/proc/meminfo` and
  `/proc/vmstat`, and `$HEAVY_CORES_USED` a run's measured cores. A command that fails while the out-of-memory killer ends a process
  hears so, and to run it again if the failure looks odd.
- **issue-todo**: a GitHub issue's task-list items across its body and every
  comment (`issue-todo show N`), which `gh issue view` splits between two
  commands, and ticking one where it lives (`issue-todo tick N MATCH --note
  TEXT`, with `--undo` and `--dry-run`). `issue-todo open` lists every open
  issue's open items, a cheap answer to "what's left to do here", and `--titles`
  one line per open issue (number, title, count of open items), to find an issue
  by its subject. `issue-todo read N...` prints each issue's body and every
  comment, with author and date. `issue-todo claim N` claims an issue for this
  session (`--as NAME`, else `CLAUDE_CODE_SESSION_ID`'s first 8 characters) in a
  marked comment, and refuses while another session's claim stands; `release N`
  frees it. It writes through `gh api`.
- **openrouter-credits**: prints an OpenRouter account's credits, bought, used
  and left. The key comes from `OPENROUTER_API_KEY`, else from `./.env` (or
  `--env-file`); it is never printed.
- **shape**: `shape FILE...` prints a file's structure and none of its values,
  for a file an agent mustn't print, such as credentials or private data: each
  key path with its type and list lengths (`.runs[].score float|null (2 of
  3)`), in JSON, YAML or a markdown file's front matter, whose header counts the
  body lines after it. `-` reads stdin, such as a server's reply. A key that
  doesn't look like a field name (a title or an email used as a key) is counted
  under `.*`, not shown. A file it can't parse fails in one line that names the
  file and quotes none of it.
- **scratch-usage**: lists what fills Claude Code's scratch folders under
  `/tmp/claude-<uid>/`, which share the sandbox's small disk and are never
  cleaned up. Each line is a session folder, or anything else an agent left
  directly under the root or a project folder, biggest first: its disk usage,
  the age of its newest write, its absolute path (the folders' names start
  with `-`, which `du` and `find` read as an option), and the oldest live
  process using it (its cwd is there or it holds a file there open; a running
  session's claude process holds its `tasks` folder). `--limit N` lines (10),
  then the total and the free space on the root's filesystem. It deletes
  nothing. `--warn-if-full` prints one line naming the tool when the
  filesystem holding /tmp is at least 90% used, as df counts it, and nothing
  otherwise; `install.py` registers it as a SessionStart hook, so every
  session hears when the disk is nearly full. For the tests,
  `$SCRATCH_USAGE_ROOT` stands in for the root and `$SCRATCH_USAGE_DF` names a
  file of the filesystem's size, used and available bytes.
- **edit-script-gate**: a Claude Code hook, which `install.py` registers, that
  refuses a Bash command running inline Python (a heredoc or `-c`) whose code
  reads a file, `.replace()`s text and writes that same file back, and points
  to `apply-edits`. Only the Python's own code counts, and pattern-based edits
  (`re.sub`, `sed`) pass. So do edits of files that are all provably under
  `/tmp/` (scratch copies, an agent's scratchpad): a literal path, `Path()` of
  one, or `sys.argv[i]` from the command line, directly or through a name
  bound once; a shell variable counts when it's set once, before the python
  command, to a plain value.
- **heavy-gate**: a Claude Code hook, which `install.py` registers, that
  refuses a Bash call running `heavy -- CMD` unless it was made with
  `run_in_background: true`, since heavy's queue has no time limit and the
  Bash tool kills a call at its own; the refusal names `bgrun --wait -o LOG --
  heavy -- CMD` as a background call, and `heavy --quick -- CMD` for a run of
  seconds, which passes in the foreground only when the call's own timeout is
  600000 ms (a `timeout` command doesn't count), since it may wait minutes for
  the running job. Background or not,
  it refuses heavy around bgrun, git commit or wt-ship. It reads the command as
  shell words and looks only at what runs, through wrappers such as `timeout`
  and `nice`, `bash -c`, a heredoc fed to a shell and `$(...)`. A word that only
  mentions heavy passes, as do `heavy status`, `heavy -h`, `bgrun` without
  `--wait`, and a heavy with `HEAVY_DIR` or `HEAVY_SLOT` set in front of it.
- **pipe-status-gate**: a Claude Code hook, which `install.py` registers, that
  refuses a Bash command piping `git commit`, `wt-ship` or `bgrun --wait` into
  another command. A pipeline exits with its last command's status, so in
  `git commit -q -F MSG 2>&1 | tail -2` a commit its hook refused reads as a
  success: an agent built on a commit that didn't exist that way. A pipe after
  a compound command around one, as in `( ... ) | tail` or a loop's `done |
  tail`, counts too. A command passes when pipefail is on for its pipeline,
  from a `set -o pipefail` earlier in its script and outside any compound
  command, and so do help as the first argument (`wt-ship -h | head`), `git
  commit --dry-run`, a pipe into `git commit`, `||`, and a `|` in quotes, a
  heredoc's body or a comment. It reads the command as heavy-gate does, through
  `lib/shellparse.py`, nested scripts included.
- **gh-body-gate**: a Claude Code hook, which `install.py` registers, that
  refuses a `gh issue` or `gh pr` `create`, `comment` or `edit` whose body
  `slop-lint` flags, before the text is public, with slop-lint's lines and the
  way out: fix the text, or end a line it wrongly flags with
  `<!-- slop-lint: ignore -->`. It reads the body from `--body-file`/`-F` (a
  file, or a heredoc on stdin) and from `--body`/`-b` text, where a
  `$(cat <<'EOF' ... EOF)`, `$(cat FILE)` or `$(< FILE)` counts as the text it
  prints. A file the same call writes is judged by the heredoc a bare `cat`
  writes into it, and passes when anything else may write it. An edit is judged
  only on lines the current body lacks, which it fetches with `gh ... view`. It
  finds gh as heavy-gate does, through `lib/shellparse.py`, and lets pass any
  command it can't parse, any body it can't read, and slop-lint failing.
  `gh api`, `gh pr review` and titles aren't judged. For the tests,
  `$GH_BODY_GATE_SLOP_LINT` names another slop-lint and
  `$GH_BODY_GATE_DEADLINE` another deadline.
- **issue-view-hint**: a Claude Code hook, which `install.py` registers. After a
  `gh issue view` that printed task-list boxes, or any with `--comments`, it
  adds a note for the agent: the body and the comments come out separately, and
  `issue-todo show N` lists both. It never blocks, and other commands get no
  note.

- **build-state**: `/build`'s bookkeeping. `start` records the base commit
  (HEAD, or `--base REV`) and the repo's fingerprint (every other ref but the
  stash and other worktrees' branches, and the config with its values hashed),
  `refactor` the refactor base. `finish` refuses leftover conflict markers,
  folds `fixup!` commits into the commits they fix, leaving every other commit
  separate and the tree as it was (a fix that conflicts gets the later commit
  it was made on top of and the `reword` command that points it there). It
  runs no check: a fold's new tree has no pass on the gate's record, so
  `wt-ship` checks it when it lands, and a fix folded into a commit before the
  code it needs fails there, naming that commit. Then it lists the commits since the
  base and the test files changed since the refactor base, and fails naming
  any ref or config key that moved (a tag only when it points at a commit the
  fold may rewrite); `core.hooksPath`, another branch's settings, the work
  branch's remote and merge, each when it wasn't set at start (a `git push -u`
  for CI), and a remote-tracking ref other than the target's or the upstream (a
  topic pushed for CI, or what a fetch moved, counted unless it holds a work
  commit) it only reports, and goes on. In a detached worktree, the
  branch it lands on (the main tree's, when HEAD shares history with it) may
  move: finish then names it, and says to rebase onto it while HEAD lacks one
  of its commits. `review-prompt
  BRIEF` writes the review subagent's prompt for the commit at HEAD to the git
  dir, with `/code-review`'s two briefs and its standards section copied from
  its SKILL.md, and prints a short prompt pointing at that file, to pass to the
  subagent; it says on stderr when the repo has no `CODING_STANDARDS.md`. When
  the repo has an `agent-pre-commit`, which `wt-ship` runs on every commit
  when it lands, the prompt tells the reviewer to run only the tests a finding
  needs rather than the whole suite.
  `reword SHA [FILE]` replaces a commit's message (FILE, or stdin), keeping
  every tree; it refuses during a rebase, with uncommitted changes, with merges
  after the commit, a later `fixup!` naming it, or the commit already pushed.
  The state lives in the git dir; `show` prints it.

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
  subagents' and workflow agents' logs, for a retrospective. Prints what each
  agent and tool cost (calls, errors, estimated tokens of tool output, API
  tokens in and out), its five biggest tool outputs and its longest calls,
  then each subagent's timeline: user turns, the assistant's text, one line per
  tool call with its output's size and, for a call of a minute or more, how
  long it took (`LONG` from 40 minutes, when the prompt cache may expire),
  a line where a request rebuilt the expired prompt cache, with the idle
  time before it (also listed in the summary),
  one line per background task notification
  (id, status, summary), and every error in full. A hook that failed or was
  cancelled (by a timeout, say) gets a line with its error's last line, under
  the tool call it ran for or, if it ran for none, where it ran; the summary
  counts hook errors per hook. Workflow agents, named
  `<run>:<label>`, show only their failed calls and hooks unless `--timelines`
  is given. `--errors` prints the short form: the summary, then under each
  agent its failed calls, failed hooks and failed task notifications, and the
  main session's user turns, nothing else. An error over 1000 characters keeps
  its first 200 and last 800. For a model reading the whole session,
  `--whole-text` prints the assistant's text whole (user turns up to 10000
  characters) and `--tails` each successful output's last three lines.
  Images count about width × height / 750 tokens, from their header. Defaults to the
  newest session for the current directory (or its nearest parent with sessions),
  under `$CLAUDE_CONFIG_DIR` (default `~/.claude`).

- **session-scan**: has a cheaper model read a whole session's digest, every
  agent, for /upskill. Writes `session-digest --timelines --whole-text --tails`
  to `--out DIR` (`--no-tails` leaves the output tails out), cuts
  it into chunks of whole agent sections (`--chunk-chars`, default 60000; a
  longer section is cut at a user turn), and sends each, after the digest's
  summary and the user's turns, to `claude-fresh` (`-m`, default sonnet; `-j`
  at once, default 4) with /upskill's six categories. Prints the candidate
  findings it gets back, each with the digest lines it cites and the agent
  whose section they are in, then calls made the same way (numbers aside) in
  two agents or more, with their lines, then each chunk's candidate count.
  Candidates with an unknown category or lines outside their chunk are dropped
  and counted; a failed chunk is retried once after `SESSION_SCAN_BACKOFF`
  seconds (default 30), and `--chunks N,M` rereads chunks of an earlier run in
  DIR, such as a failed one or those a stopped run never read. Exit 3 when a
  chunk failed twice.

- **yt-transcript**: prints a YouTube video's captions, or the ones around
  the URL's timestamp (90 seconds before, 60 after, by default), each line
  prefixed with its clock time. For finding what a video said at a moment
  someone bookmarked. Caches each transcript in `claudedev/cache/yt-transcript/`
  (`$YT_TRANSCRIPT_CACHE` overrides), and exits 3 when YouTube refuses the
  request (it blocks IPs that fetch a lot), so callers can retry later.

- **gh-src**: `gh-src OWNER/REPO REF` prints the path of a checkout of a GitHub
  repo at a tag, branch or commit SHA, for reading and grepping another
  project's source rather than fetching it file by file with `gh api`. The first
  call makes a shallow clone in `claudedev/cache/src/` (`$GH_SRC_CACHE`
  overrides), shared by every session; later calls print the path without the
  network. Each commit's checkout never changes, so `--refresh`, which fetches a
  moved branch again, leaves readers of the old one their files.
  `$GH_SRC_URL_BASE` points it somewhere other than GitHub, for the tests.

- **paper**: a paper's metadata (`meta`), the papers citing it (`cites`) and
  keyword search (`search`) from Semantic Scholar, every paper matching a boolean
  query (`sweep`: Semantic Scholar's bulk search, written to a TSV file reused for
  a day, printing the 20 most cited and the 20 newest; a second, newest-first
  fetch runs when there are more matches than `--max`; keep queries to 2-3 key
  terms with `|` for synonyms, and take a thin result as a reason to rephrase,
  not as absence), a summary of its request log (`stats`), and its full text (`text`:
  downloads the arXiv or direct-URL PDF, or a DOI's open-access PDF found
  through OpenAlex, runs `pdftotext -layout` and prints the `.txt` path). `outline` lists the text's section headings with line
  numbers and `grep` its lines matching a regex, with context, so an agent
  can read a section instead of the whole paper. `text`, `outline`, `grep` and `meta` also take a
  LessWrong or Alignment Forum post, by its LessWrong, Alignment Forum or GreaterWrong URL or as
  `lw:ID`: one request to LessWrong's GraphQL API caches its Markdown as `lesswrong/ID.md` and its
  title, date, authors, karma and URL beside it, and `outline` lists its Markdown headings. Takes
  arXiv ids and URLs, DOIs and Semantic Scholar
  paperIds, and prints a few lines per paper; `meta` asks Semantic Scholar for
  all its uncached ids in one batch request, so pass every id to one call, and
  adds an arXiv paper's latest version and its first- and latest-version dates, from one arXiv API
  request per call (cached for a day). Paces requests across every `paper`
  process sharing the cache, so a fan-out of agents stays under the limits:
  Semantic Scholar's at least 1.1 seconds apart, LessWrong's at least 1, the arXiv API's at least 3;
  PDF downloads aren't paced. Retries rate limits, 5xx and
  timeouts with backoff for up to 2 minutes, so callers needn't sleep and
  retry. When Semantic Scholar still fails, `meta`, `search` and `cites`
  answer from OpenAlex under a `source:` line, `meta` then from Crossref for a
  DOI OpenAlex doesn't have; OpenAlex counts far fewer citations for arXiv
  papers. When Semantic Scholar doesn't know a paper, `meta` asks OpenAlex,
  then Crossref for a DOI, and says which answered; it reports not found only
  when none knows it. `bib` prints a BibTeX entry per
  DOI or arXiv id (through its DataCite DOI) from doi.org's content
  negotiation, keyed by first author and year (`nash1951`), with `a`, `b`, …
  on keys that clash in one call. For an arXiv paper published since, it
  prints the version of record's entry (its DOI from Semantic Scholar or
  OpenAlex, checked against arXiv's title and first author, and looked up
  again after a week) with `eprint` and `archiveprefix` fields, unless
  `--preprint`;
  otherwise the arXiv entry keeps v1's year in its key but takes its title
  and authors from arXiv's latest version and gets a `version` field. Sends a
  Semantic Scholar API key from
  `$S2_API_KEY`, else from `claudedev/.semantic-scholar/api-key`, and an
  OpenAlex key from `$OPENALEX_API_KEY`, else from `claudedev/.openalex/api-key`,
  when there is one. Caches everything
  in `claudedev/cache/paper/`, or `~/.cache/paper/` on a machine without `claudedev/cache/` (`$PAPER_CACHE`
  overrides). Exits 1 when not found, when a URL is not a PDF or when a DOI
  has no BibTeX, 3 when the remote still refuses.

- **build-paper**: builds a project's Typst paper, `paper/paper.typ`, with
  paperkit (`lib/paperkit/`, whose README says how to write one): fills in its
  numbers, tables and figures from the data sources in `paper/data/`, compiles
  it, and checks it. The draft goes to `paper/build/DRAFT-paper.pdf` (with a
  PNG of each page under `--pages`); `--final` writes the unmarked submission
  only when every check passes, every link resolves and the tree is clean. The checks every
  paper gets (no missing key, failing `#claim` or `#todo`, no `;` or `#` Typst
  swallows) always run; `paper/paper.toml` turns on the rest (a page cap, the
  abstract's length, required sections, the template, the `citations.md`
  check, pushed logs, the PDF's size), each off without its key. A build over
  the page cap also reports the PDF's lines past it, each with the
  `paper.typ` line it comes from, the blank space at the bottom of each page,
  and the short last lines of paragraphs. The status block marks each
  problem that's new since the last build, and counts them. `watch`
  recompiles the draft on each save and rebuilds it on each commit. `find
  TEXT` lists the pages of the last build whose text holds TEXT (ignoring case
  and whitespace), with each page's PNG when that build drew them; `keys
  PREFIX...` prints each ledger key starting with a PREFIX, its value and rate on one line (`--how` adds how
  it was computed and its logs). It runs
  inside the project's own uv environment, so a data source can import the
  project's code, with paperkit's pinned dependencies added. `vendor` copies
  paperkit into the project (`paperkit/`, with a `bin/build-paper` that runs
  it) for collaborators without llm-toolbox, and `vendor --check` lists where
  that copy has drifted from the toolbox's; while a project has its own copy,
  this `build-paper` runs that copy too. With `$AI_AGENT` set, it refuses to
  build in a repo's main worktree, where the user's `watch` runs, and names
  the `git worktree add` command to use instead (`--here` overrides). Every
  output is written to a temporary file and renamed into place, so a
  concurrent reader never sees half of one. Started as a copy
  of apart-research-collusion-sprint-2026's `paper/` build.

## Checks

```bash
.githooks/agent-pre-commit
```

It checks what's staged, running the tests in parallel (`pytest.ini`; `pytest -n0 FILE` runs one file
without the few seconds of starting the workers) in a copy of the index outside any git checkout, so a
test that passes in the repo but reads its git state fails there. `install.py` points git at `.githooks`. Its
`pre-commit` runs `pipefail-lint` on the staged files that name pipefail, then the two ruff checks on what's staged. When only formatting fails, it formats each staged file that has no unstaged changes,
stages the result and names the file; any other unformatted file refuses the commit, as does every
file of a `git commit PATH`. `agent-pre-commit` adds the tests (about half a minute, most of it build-paper's builds);
`wt-ship` runs it on each commit when it lands. Both run the tools
through `uv run`, so they need uv on `PATH`. `ruff.toml` brings the
extensionless scripts in `bin/` into both ruff checks.

An agent's commit runs `.githooks/agent-commit-check` in `agent-pre-commit`'s
place: no tests, since `pre-commit` ran ruff, and the agent runs the tests it
wrote or touched before committing. `wt-ship` runs `agent-pre-commit` on each
commit when it lands, where `agent-commit-gate` hands it a `CHECK_BASE`,
and the hook runs only the tests the change since then can affect. A change to
`README.md`, `GLOBAL_CLAUDE.md`, `skills/` or `skill-patches/` runs the one to three
tests that read those files, under two seconds on one core, so the landing doesn't
wait for heavy's queue. A change to `CLAUDE.md`, `CODING_STANDARDS.md` or `docs/`
runs none. Any other path, the hook itself included, runs every test, and the hook
first asks the gate for heavy's queue (`CHECK_QUEUE_EXIT`, `agent-commit-gate -h`).
The map is at the top of the hook.

## Guidelines

`GLOBAL_CLAUDE.md` is named that way, rather than `CLAUDE.md`, so an agent working in
this repo does not read it as the repo's own instructions. A real file already at
either destination moves aside to `.bak`; a link there is replaced.
