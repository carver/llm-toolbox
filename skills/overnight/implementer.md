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
3. **Code** in a worktree made the way the repo says (`bin/new-worktree TOPIC`, or `git worktree add --detach ../<repo>-wt-<topic>`). Load the `coding` skill. Test first where behaviour changes. Commit there, as a background call when the repo's checks queue.
4. **Stop for review** after committing, unless the change is trivial (no behaviour worth a test). Report `READY FOR REVIEW: <worktree> base <sha>` with what you built, what you chose and what the reviewer should probe. The orchestrator sends the findings back. Fix what's real as fixup commits, then go on.
5. **Finish:**
   - **Land**: from the main tree, `bgrun -o <log> -- wt-ship -C <worktree> --local --remove`, then `waitone --pid PID -t 170` until it exits. Act on its output.
   - **Draft**: `git fetch`, rebase onto `origin/<default>`, push `HEAD:refs/heads/overnight-<topic>`, and open `gh pr create --draft`, writing its body with the `pr` skill. Post the question on the issue, linking the PR. Label the issue `needs-owner` and remove the worktree.
6. **Wrap up.** Tick what you did (`issue-todo tick N MATCH --note <sha>`), release the claim unless your landing closes the issue, and append your line to `log.md`.
7. **Report** in at most 8 lines: the end state, the sha or PR, any rerun or other step the owner must take, and anything surprising.

Rules:
- Scratch files (commit messages, logs, drafts) go in `<scratchpad>/issue-N/`, never the scratchpad's top level, since other agents share it. After each commit, check that `git log -1` shows your own message.
- Give every network fetch a timeout. When you are stuck on a choice that data you can't get would settle, post the question rather than keep researching.
- Measure a speed-up under `heavy --alone`, one job per measurement. A run that warns about outside CPU is no timing.
- Commit messages and PR bodies end with the attribution lines in `protocol.md`.
