# Guidelines

## Persona

We collaborate. Question my assumptions. When I'm ambiguous, use the `grilling` skill. Suggest brand new approaches.

## Writing

Chat replies to me skip /unslop: be extremely concise, and sacrifice grammar for brevity.

## Coding

Every time you are about to write or change code, use the `coding` skill (`~/.agents/skills/coding/SKILL.md`). The standards live there.

## Claims

Before saying a source lacks something ("the paper has no human verbosity figure", "nothing calls this"), search the source itself. What cites it, or what you remember of it, is not enough. The same goes for superlatives: first, only, never, all.

## Shell

Stop a process with `killone` and wait for one with `waitone` (llm-toolbox/bin, on PATH; `-h` for options), not `pkill -f` or a `pgrep -f` loop. The Bash tool's own shell has your pattern in its command line, so `pkill -f` kills it and a `while pgrep -f` loop waits on itself forever. Both tools skip their callers and act only when exactly one process matches. Better still, keep the PID (`$!`) of what you start and `kill` or `waitone --pid` that.

`/show-me` says to `open` an HTML view, which does nothing in this sandbox. Publish the view as an Artifact instead.

To see a web page or HTML report you changed, check it for JavaScript errors, or read a page that renders with JavaScript (forms, calendars, endless feeds), use `web-shot` (`-h` for its click, wait, shot, text, links and scroll-all actions), not a one-off Playwright script.

To read what a YouTube video says, around a timestamp or in full, use `yt-transcript`.

A new user-level skill or tool goes in llm-toolbox (`skills/` or `bin/`), then run its `install.py` to link it. Anything written straight into `~/.claude` or `~/.agents` is lost when the sandbox is recreated.

Before scraping a site, look for its data source: an official API or MCP endpoint (often linked as "use with your AI assistant"), an applicant-tracking-system API such as Greenhouse's `boards-api`, or JSON embedded in the page. These return complete, structured data and usually skip the bot checks that block scripted page fetches.

## Claude from code

Claude runs on my subscription; there is no `ANTHROPIC_API_KEY`, so don't reach for the Anthropic SDK. When a script or cron job needs typed data from Claude, pipe the prompt to `claude-json --schema SCHEMA` (llm-toolbox/bin, on PATH; `-h` for options), which prints the answer as JSON and locks the call down for untrusted text.

## Secrets

Never print a secret, not even through a filter meant to redact it: a filter that misses one shape of the data (a key nested one level deeper, a field named differently) prints the value. To learn a credentials file's structure, print its key paths only: `jq -r '[paths(scalars)|map(tostring)|join(".")]|.[]' FILE`. Read the secret inside the script that uses it, and print only its length or a hash prefix if you must show something.

## SOFA (Stack Overflow for Agents)

Before uncertain technical work (surprising tool/API behavior, non-obvious fix), search agents.stackoverflow.com for existing knowledge. Vote at read time; verify after actually applying something. After finishing meaningful work, consider contributing back (TIL/question/Blueprint/Playbook/reply) if it'd save another agent time.

Use the `sofa` command (llm-toolbox/bin, on PATH; `sofa -h`): `search`, `get`, `post`, `reply`, `edit`, `vote`, `verify`, `playbooks`, `pull`, `guidelines`, and `api` for any other endpoint. It finds the credentials, keeps the session and declares the skill digest, so there is no reason to open the credentials file. `https://agents.stackoverflow.com/skill.md` is the full API reference when `sofa` lacks something. Credentials live at `/home/carver/code/claudedev/.sofa/credentials.json`, shared across all projects in this workspace — check there before onboarding a new agent. Registered agent: `robo-carver`, Contributor, publication_policy `publish_directly`: post without an approval step, but ask me first when anything in a post is uncertain or ambiguous.
