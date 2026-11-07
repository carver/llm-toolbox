# Guidelines

## Persona

We collaborate. Question my assumptions. When I'm ambiguous, use the `grilling` skill. Suggest brand new approaches.

## Writing

Chat replies to me skip /unslop: be as concise as possible, without hurting clarity.

## Coding

To write or change code, run `/build`: you implement and refactor, and subagents review. A trivial edit, such as a one-file change with no behaviour worth a test, you make yourself with the `coding` skill (`~/.agents/skills/coding/SKILL.md`). If you wrote or changed a test, it isn't trivial: run `/build` with its reviews, small follow-ups late in a session included. With no subagent tool, as inside a workflow agent, run `/build`'s review step yourself: read the prompt file `build-state review-prompt BRIEF` names and review your own diff against it, both headings, before the refactor.

Before your first edit in a repo's main tree, claim it: make sure `.git/info/exclude` lists `CLAUDE_WORKING` (git reads that ignore list on the host too, and never commits it), then create the flag with `(set -C; echo "$CLAUDE_CODE_SESSION_ID $(date -Is)" > CLAUDE_WORKING)`, which fails when the flag already exists. A flag holding your own session id is still yours. Claim once per working stretch: keep the flag across a run of commits, and delete it once your changes are pushed, or when you stop working in the tree.

The main tree is someone else's when the claim fails on another session's id, or when it is dirty at the start of your session. Leave their changes alone, tell me the flag's age so I can clear one a crashed session left, and build in a detached worktree beside the repo, named for its topic: `git worktree add --detach ../<repo>-wt-<topic>`. Other sessions may hold worktrees there too, so check `git worktree list` first. It has no `.venv` or untracked files, so set up the environment the repo's README describes before running checks there. Commit in the worktree. To finish:

1. In the worktree, run `wt-ship --local --remove` (`-h`). It rebases onto the main tree's branch, folding your fixups, reruns the checks, claims the main tree for the merge, fast-forwards it, and removes the worktree; when another session lands first, it rebases and checks again until it lands, up to 10 times. On a rebase conflict, resolve the hunks with `conflicts` (`-h`), `git rebase --continue`, and run it again. When another session holds the main tree's claim, it stops: tell me the flag's age. When the uncommitted changes there touch the same files, it stops: ask me.
2. In a repo whose rules say to push, run `wt-ship --remove` instead: it rebases onto `origin/<branch>`, reruns the checks, pushes and confirms the push, and says when the main tree's branch is behind its remote, which you then tell me. In any other repo, ask me before pushing.

## Research subagents

A brief that sends a subagent to read papers, or LessWrong and Alignment Forum posts, tells it to fetch them with `paper` (`paper -h`; a post goes by its URL), start each paper from `paper outline ID` and read only the sections it needs by line range, and find single facts with `paper grep ID PATTERN`. When several agents read in parallel, assign each paper to one agent. `paper text` caches each paper's full text on the host disk, so a paper that two agents do need is downloaded and converted once.

## Claims

Before saying a source lacks something ("the paper has no human verbosity figure", "nothing calls this"), search the source itself. What cites it, or what you remember of it, is not enough. The same goes for superlatives: first, only, never, all. A keyword search that finds nothing, such as `paper sweep`, calls for synonyms and another phrasing, not a claim that no such work exists; list the queries you ran.

## Shell

Stop processes with `safekill --one -f PATTERN` (exactly one must match) or `safekill --all -f PATTERN` (every match); without `-f` the pattern is the exact process name (`uv`, `python3`), not the command line, and wait for one with `waitone` (llm-toolbox/bin, on PATH; `-h` for options), not `pkill -f`, `pgrep -f | xargs kill` or a `pgrep -f` loop. The Bash tool's own shell has your pattern in its command line, so `pkill -f` kills it and a `while pgrep -f` loop waits on itself forever. Both tools skip their callers, and so does `safeps [-f] PATTERN`, which lists matching processes with their PIDs and ages.

Start a background job with `bgrun -o LOG -- CMD`. It prints the job's own PID, where `cd DIR && nohup CMD &` gives `$!` a subshell's PID. Then `kill` or `waitone --pid` that PID. A non-empty LOG from an earlier run is moved aside to `LOG.<time of its last write>`, not overwritten. To be told when it ends, run `bgrun --wait -o LOG -- CMD` as a background call: it prints the PID, then the job's exit status and LOG's last lines.

To make several text replacements in one or more files, pipe blocks to `apply-edits` rather than writing a Python heredoc script: a `--- PATH` line starts each file, then each block is `<<<<<<< SEARCH`, the old text, `=======`, the new text, `>>>>>>> REPLACE`, every marker on its own line. It applies all or nothing, and a miss shows the closest text.

When ruff reports a line too long (E501) in a docstring or a comment, run `rewrap FILE...` rather than rewrapping by hand: it reflows only the paragraphs past the line length, and lists the lines it can't fix.

To see a web page or HTML report you changed, check it for JavaScript errors, or read a page that renders with JavaScript (forms, calendars, endless feeds), use `web-shot` (`-h` for its click, wait, shot, text, links and scroll-all actions), not a one-off Playwright script.

To start a new Rust desktop app, run `new-egui-app DIR` (`-h` for options) rather than copying an earlier app's setup.

To read what a YouTube video says, around a timestamp or in full, use `yt-transcript`.

A new user-level skill or tool goes in llm-toolbox (`skills/` or `bin/`), then run its `install.py` to link it. Anything written straight into `~/.claude` or `~/.agents` is lost when the sandbox is recreated. To change another author's skill, edit it in `~/.agents/skills`, then run `skill-patch save NAME --why "..."`; an edit without a patch is lost at the next update.

Before scraping a site, look for its data source: an official API or MCP endpoint (often linked as "use with your AI assistant"), an applicant-tracking-system API such as Greenhouse's `boards-api`, or JSON embedded in the page. These return complete, structured data and usually skip the bot checks that block scripted page fetches.

## Profiling

Before you time anything, check the load with `uptime`. Other sessions share this machine's cores, and a load above the core count (`nproc`) slows every timing you take: wait for it to drop, or give the load beside the numbers.

To find what's slow, start with a sampling profiler (`py-spy` for Python, `--subprocesses` for the interpreters it spawns): it disturbs what it measures far less than cProfile, `strace -f` or a hand-built timing loop. Compare timings with `hyperfine`, not repeated runs by hand. Reach for deterministic tracing only for what sampling can't tell, such as an exact call count.

## Claude from code

Claude runs on my subscription; there is no `ANTHROPIC_API_KEY`, so don't reach for the Anthropic SDK. When a script or cron job needs an answer from Claude, pipe the prompt to `claude-fresh --json SCHEMA` for typed data, or `claude-fresh --text` for prose (llm-toolbox/bin, on PATH; `-h` for options). It locks the call down for untrusted text.

## Secrets

Never print a secret, not even through a filter meant to redact it: a filter that misses one shape of the data (a key nested one level deeper, a field named differently) prints the value. To learn a credentials file's structure, print its key paths only: `jq -r '[paths(scalars)|map(tostring)|join(".")]|.[]' FILE`. Read the secret inside the script that uses it, and print only its length or a hash prefix if you must show something.

## SOFA (Stack Overflow for Agents)

Before uncertain technical work (surprising tool/API behavior, non-obvious fix), search agents.stackoverflow.com for existing knowledge. Vote at read time (upvote what helped); verify after actually applying something. Post only to fill a gap you hit: when you searched for an answer and found none, post what you learned (question, reply or Blueprint). Skip TILs about problems you never searched for.

Use the `sofa` command (llm-toolbox/bin, on PATH; `sofa -h`): `search`, `get`, `post`, `reply`, `edit`, `vote`, `verify`, `playbooks`, `pull`, `guidelines`, and `api` for any other endpoint. It finds the credentials, keeps the session and declares the skill digest, so there is no reason to open the credentials file. `https://agents.stackoverflow.com/skill.md` is the full API reference when `sofa` lacks something. Credentials live at `/home/carver/code/claudedev/.sofa/credentials.json`, shared across all projects in this workspace — check there before onboarding a new agent. Registered agent: `robo-carver`, Contributor, publication_policy `publish_directly`: post without an approval step, but ask me first when anything in a post is uncertain or ambiguous.
