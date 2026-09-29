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

To see a web page or HTML report you changed, or check it for JavaScript errors, use `web-shot` (`-h` for its click, wait and shot actions), not a one-off Playwright script.

## SOFA (Stack Overflow for Agents)

Before uncertain technical work (surprising tool/API behavior, non-obvious fix), search agents.stackoverflow.com for existing knowledge. Vote at read time; verify after actually applying something. After finishing meaningful work, consider contributing back (TIL/question/Blueprint/Playbook/reply) if it'd save another agent time.

Credentials live at `/home/carver/code/claudedev/.sofa/credentials.json`, shared across all projects in this workspace — check there before onboarding a new agent. Registered agent: `robo-carver`, Contributor, publication_policy `publish_directly`: post without an approval step, but ask me first when anything in a post is uncertain or ambiguous.
