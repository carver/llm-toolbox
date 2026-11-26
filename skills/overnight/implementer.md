# Brief for an overnight implementer

You are a subagent working one issue while the owner is away. You report to the orchestrator, which reviews your diff through a separate agent.

Read first:
- `protocol.md` beside this file, for the end states and rules.
- The repo's agent docs.
- The issue in full, comments included (`issue-todo read N`).
- Your triage note in the orchestrator's brief.

The main tree is read-only for you.

1. **Claim** the issue with `issue-todo claim N`, or with whatever newer pattern `issue-todo -h` documents. If the claim is refused, stop and report.
2. **Decide the end state.** For a question, post it and skip to step 6.
3. **Code** in a worktree made the way the repo says (`bin/new-worktree TOPIC`, or `git worktree add --detach ../<repo>-wt-<topic>`). A draft's worktree starts at `origin/<default>`, never the local default branch, which holds the night's unpushed landings: `git fetch`, then the repo's tool with its base option if `-h` shows one, else `git worktree add --detach ../<repo>-wt-<topic> origin/<default>`. Run `build-state start` in the worktree before your first commit. Load the `coding` skill. Test first where behaviour changes.
4. **Stop for review** after committing, unless the change is trivial (no behaviour worth a test). Write `<scratchpad>/issue-N/brief.md`: the issue's items, each marked covered or deferred, what you built, what you chose, and each claim the change rests on, marked checked (and how) or unchecked. Run `build-state review-prompt <scratchpad>/issue-N/brief.md` in the worktree. Report `READY FOR REVIEW: <worktree> base <sha>` with the pointer it printed, what you built, what you chose and what the reviewer should probe. A fresh fixer usually takes the findings from there; when the orchestrator sends them to you instead, fix them as the fixer does, then go on.
5. **Finish:**
   - **Land**: from the main tree, start `wt-ship -C <worktree> --local --remove` as a background job (below), then `waitone --pid PID -t 170` until it exits. Act on its output.
   - **Draft**: list the open PRs (`gh pr list --state open --json number,files`). Their files show the migration numbers they take; for a skill or package version, read it from `gh pr diff N`. Take the next number or version none of them holds. Run `git fetch`, then rebase onto `origin/<default>`, push `HEAD:refs/heads/overnight-<topic>`, and open `gh pr create --draft`, writing its body with the `pr` skill. Post the question on the issue, linking the PR. Label the issue `needs-owner` and remove the worktree.
6. **Wrap up.** Tick what you did (`issue-todo tick N MATCH --note <sha>`), release the claim unless your landing closes the issue, and append your line to `log.md`.
7. **Report** in at most 8 lines: the end state, the sha or PR, any rerun, timing command or other step the owner must take, and anything surprising. Your final report is your one channel to the orchestrator, a reply to its status message included: a subagent can't `SendMessage` it by name.

As the **fixer**, your brief names the worktree, the base, the implementer's report, the findings and the end state. Skip steps 1 to 4: fix the findings in that worktree in one `fixup!` commit for each commit they belong to, then do steps 5 to 7. Each commit waits in the shared queue for its checks, so a fixup per finding multiplies the waits. When your brief says **second review**, stop after the fixups instead and report `READY FOR REVIEW: <worktree> base <the head before your fixups>`, with no review-prompt pointer: `build-state review-prompt` covers the whole change from the implementer's base. Step 5 follows once the orchestrator sends the verdict. When the verdict is that the second review bounced, end as gave up: push what exists as a draft PR (step 5's Draft) and say on the issue where it stopped and why.

Rules:
- Scratch files (commit messages, drafts) go in `<scratchpad>/issue-N/`, never the scratchpad's top level, since other agents share it. After each commit, check that `git log -1` shows your own message.
- Background jobs keep their log and PID in `jobs/issue-N/` beside this file, so a later agent can settle any you leave running: `mkdir -p <J> && bgrun -o <J>/<name>.log -- CMD... > <J>/<name>.pid && echo <program> >> <J>/<name>.pid`, where `<J>` is that folder and `<program>` is CMD's program name (`git`, `wt-ship`, `heavy`). The file's first line is the PID to wait on.
- Commit in the background, since the commit hook queues its checks: a background job named `commit` running `git commit -F <scratchpad>/issue-N/msg.txt`, then `waitone --pid PID -t 170`, again after each exit 4, then read its log.
- Start builds and test runs the same way, as background jobs. After `bin/new-worktree` or another warm build, wait for that build before a targeted test run, which otherwise rebuilds in the queue.
- Edit files with your edit tool or `apply-edits` (`-h`). A Python script that reads, replaces and writes a file is refused, and nothing in its command runs.
- A change to a skill or other text that tells agents to run commands: run each changed command live, against the real service and a case other than your own repo, within the owner's opt-ins in `protocol.md`, before `READY FOR REVIEW`.
- When you are stuck on a choice that data you can't get would settle, post the question rather than keep researching.
- A timing (a speed-up, an A/B) waits for the end of the run, since the night's other work spoils it. Commit without it, put each timing's exact command, verbatim, in your report and your `log.md` line, and stop there; the orchestrator runs it last. Quote the command itself rather than a file that should hold it: one report said a README held a rerun step, and the README had no such change. A comparison longer than a minute or two in all (runs × variants × a run's length) is a `heavy-bench` run started bare with `bgrun`; a shorter one is one `heavy --alone` job per measurement. That holds when your brief names another method, such as `time` under plain `heavy`: use heavy-bench and say so in your report.
- Commit messages and PR bodies end with the attribution lines in `protocol.md`.
