# Coding standards

The rules a reviewer holds this repo's code to. The global standards in `~/.agents/skills/coding/standards.md` apply as well; this file wins where the two conflict. A rule that a check already enforces is listed with the check, so a reviewer can skip it.

## Layout

- `bin/`: the tools, one executable Python script each, with no extension. `install.py` links each into `~/.local/bin/`, so an edit is live at once on every machine that shares the repo.
- The repo root: scripts for working on this repo itself, such as `./t`, which runs its tests. They stay out of `bin/`, so they don't land on every shell's PATH.
- `lib/`: modules two or more tools share. A tool imports them by path from its own location, since it runs through a link.
- `skills/<name>/SKILL.md`: this repo's skills. `install.py` links them into `~/.claude/skills/` and `~/.agents/skills/`. Changes to another author's skill live in `skill-patches/`, made with `skill-patch`, never by editing the installed copy.
- Tests sit at the repo root as `test_<tool>.py`, with shared fixtures in `conftest.py`.
- `GLOBAL_CLAUDE.md` is the user's global guidelines, linked to `~/.claude/CLAUDE.md` and `~/.agents/AGENTS.md`.

## Tools in `bin/`

- The first line is `#!/usr/bin/env python3` for a tool that needs only the standard library, or `#!/usr/bin/env -S uv run --quiet --script` with an inline `# /// script` block that pins every dependency. Never a `.venv`: the host and the sandbox share the repo, and a venv works for one machine's Python.
- The module docstring is the help: `-h` prints it, and it says what the tool does, its options, its environment variables and its exit statuses. The tool's README entry says the same in brief. Every environment variable `-h` names, the README entry names too (`test_readme_env.py`).
- Agents read the output, and every character costs tokens. The default output is the short form: a summary line, the few lines that matter, never a whole log. A longer form goes behind a flag.
- A failure prints one line saying what failed and what to do, on stderr, and exits non-zero. Distinct outcomes get distinct exit statuses, listed in the docstring, so a caller can branch without parsing text.
- A knob that exists for the tests, such as a shorter grace period, is an environment variable named after the tool (`MUTATE_CHECK_GRACE`), and the docstring says so.
- A tool that starts processes stops what it started, its children's children included, when it times out or is stopped, and puts back anything it changed. It never matches processes by command line with `pkill -f`; it uses `safekill`, `safeps` or a PID it holds.
- Some tools are copied into other repos, such as `pre-commit-run` and `slop-lint`. A change to one names the copies in its commit message, so the owner can copy it out.

## Tests

- Every tool has tests, and they run it the way an agent does: as a subprocess, through its arguments, stdin and environment, checking its exit status and output. A shared module in `lib/` may be tested by import.
- No test touches the network, the user's home directory or this repository. Fakes go first on `PATH` or behind the tool's override variable. A test that needs a git repository makes one in `tmp_path`; `conftest.py` strips the `GIT_*` variables a hook or rebase would leak. The commit hook runs the suite in a copy of the index outside any checkout, so a test that reads this repo's git state fails there.
- A change to the repo's prose alone (`README.md`, `skills/` and the other paths in the map at the top of `.githooks/agent-pre-commit`) runs only the tests the map names for it. A test that reads one of those paths goes on that path's line, or a change to the path won't run it.
- The suite runs in parallel (`pytest.ini`), so tests share no files, ports or global state.
- A test waits for a condition by polling it with a deadline, never with a fixed sleep. A test that starts a process cleans it up even when it fails: `leftover_processes.py`, whose fixtures `conftest.py` gives every test and module, kills what a test or a module's fixtures leave running and fails it, naming each process.
- A sleep of 0.2 s or more that is the scenario itself, a window in which something must not happen, says why in a `# fixed sleep: <why>` comment on its line or the line above; a slow machine may then let a bug pass, but must never fail the test. A use of SIGSTOP says why in a `# SIGSTOP: <why>` comment on its line: the stopped process may hold a lock the rest of the test waits on (`test_timing_rules.py`).
- Expected values come from the tool's documented behaviour, not from pasting what it printed.

## Skills

- A skill is written with the `writing-for-agents` skill, and its prose passes `slop-lint`.
- Every skill, slash command and `~/.agents/skills/` path a skill, the guidelines or a patch names is one `install.py` installs (`test_skill_refs.py`).

## Checks

`./t FILES` runs the tests you name (README, Developer setup). `.githooks/pre-commit` runs `bin/pipefail-lint` on the staged files that name pipefail, then `ruff check` and `ruff format --check` on what's staged, and formats a staged file with no unstaged changes. `ruff.toml` brings the extensionless scripts in `bin/` into both. An agent's commit then runs `.githooks/agent-commit-check`, which runs no test, so run the tests you wrote or touched before committing. `wt-ship` runs `.githooks/agent-pre-commit` on each commit when it lands: every test, or for a change to the repo's prose alone, the few its map names. The README's Checks section has the details.
