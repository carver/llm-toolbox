---
name: overnight
description: Work through a repo's open GitHub issues unattended while the user is away, landing what needs no decision and leaving each decision as a question on its issue.
disable-model-invocation: true
---

# Overnight

You are the **orchestrator**: you pick issues, brief fresh agents, read their short reports and decide what happens next.
The implementing, reviewing and investigating happen in subagents, so your context grows by a few lines per issue and survives a whole night.
Each issue ends in exactly one **end state**, and the user wakes to a summary instead of a stream of questions.

## End states

Every issue ends in one of these, chosen by the implementer and confirmed by you:

- **Landed**: done with no decision needed from the user. The implementer commits in a worktree and runs `wt-ship --local --remove`. Never push the default branch.
- **Draft + question**: a decision is the user's, and the recommended option is more than 55% likely right. Build that option on a branch from `origin/<default>`, push it, open a draft PR, and post the question on the issue linking the PR.
- **Question**: 55% or less. Post the question and write no code.
- **Findings**: an investigation. Post findings with evidence, tick what they answer, and file each proposed fix as a new issue. Close the issue when every item is answered.
- **Gave up**: a check failed twice for the same reason, about 2 hours passed, or a second review bounced. Push what exists as a draft PR and say on the issue where it stopped and why.

A question on an issue carries the context, the question, 2 to 4 options with tradeoffs, and the recommended option with why.
It then gets the `needs-owner` label, and the implementer releases its claim.

## 1. Set up, with the user still here

1. Read the repo's agent docs (`AGENTS.md` / `CLAUDE.md`) for its rules on pushing, deploys, owner-only work and checks.
2. Make the run folder, `keep/<project>/overnight-<date>/` (durable, outside the repo). Copy [`protocol.md`](protocol.md), [`implementer.md`](implementer.md) and [`reviewer.md`](reviewer.md) into it, and fill in the repo, the default branch, the commit attribution lines and the repo's own rules. Start `log.md` there with the header `#N | end state | sha or PR | rerun needed? | note`.
3. Create the `needs-owner` label if the repo lacks it.
4. **Triage**: split the open issues (`issue-todo open --titles`) across 2 to 4 agents. Each one reads its issues and writes one line per issue to `triage-<batch>.md`: `#N | class | size | files | end state | note`. Classes are agent, investigate, owner-only, umbrella (name its open sub-issues), blocked, wait (dated) and claimed. Sizes are S, M and L.
5. Compile `queue.md` in this order:
   1. questions only (cheap, and the user sees them first)
   2. small landings
   3. findings
   4. draft + question
   5. large landings

   Leave out owner-only work, umbrellas, claimed issues and issues waiting on a date. Mark which landings need a server rerun or other follow-up from the user.
6. Ask the user to veto or opt in to the costly items (anything that spends shared CI, money or a staging quota, or touches a stale claim), and which sensitive areas (auth, privacy, data deletion) should end as drafts even when ready to land.
   Record each answer in `protocol.md`.

Done when the user has answered and `queue.md` and `protocol.md` hold the answers.

## 2. The loop

Start the bundled `loop` skill in dynamic mode (no interval) with a prompt naming the run folder, then for each round:

1. Take the next item from `queue.md` whose files overlap least with the issues in flight. Run 3 to 6 agents at once; fewer when the machine's shared queue (`heavy status`) is busy.
2. Spawn a fresh implementer: "Follow `<run folder>/implementer.md` for issue #N", plus that issue's triage note and the in-flight issues whose files to stay out of. Batch several question-only or findings issues into one agent.
3. When it reports `READY FOR REVIEW`, spawn a **fresh** reviewer with `reviewer.md`, the worktree, the base and the questions you want probed. Give it the code, not your conclusion.
4. Read the findings and decide, then send the decision to the implementer with `SendMessage`: fix what's real as fixups, then land, or turn it into a draft + question. A finding that changes what the user is promised, deletes data, or contradicts the issue's text turns a landing into a draft + question.
5. Re-arm `ScheduleWakeup` at about 1500 s as a fallback heartbeat. Agent notifications are the real wake signal.

On each heartbeat, check every in-flight agent for progress: its worktree, commits and `log.md` line.
One with nothing after about 90 minutes gets a status message. Still nothing by the next heartbeat: stop it with `TaskStop`, release its claim, log the stall, and restart the issue as question-only with a short brief that puts a timeout on every fetch.

When a finding applies to every agent (a shared file, a misread rule), fix the briefs in the run folder and message the agents in flight.

Done when the queue is empty and no agent is running.

## 3. Hand-off

1. Write `SUMMARY.md` in the run folder:
   - **Do first**: the push, each server rerun with its stage, installs to run, cleanup.
   - **Landed**: the issues, and how far the default branch is ahead of origin.
   - **Drafts**: each PR with its issue, plus the collisions between drafts (migration numbers, skill or package versions) and which to renumber at merge.
   - **Questions only.**
   - **Investigations.**
   - **Worth knowing**: surprises, stalls, process fixes made mid-run.
2. Stop the loop, and send one `PushNotification` naming the counts and the summary's path.
3. Give the user the summary in chat, and offer it as a page in one line.

## Lessons from earlier runs

- Each agent keeps its scratch files in `<scratchpad>/issue-N/`. Agents share one scratchpad, and a shared `msg.txt` once swapped two commit messages.
- A draft branches from `origin/<default>`, never the local default branch, which holds the night's unpushed landings. If it must stack on them, the PR says so.
- Drafts collide with each other: two PRs take the same migration number or skill version. Name the collision in each PR and in the summary.
- A review checks each claim the change rests on against the source, including the issue's own premise. Reviews have found a lifecycle probe that could only answer "yes", a per-account rate limit that a second token on the same account doesn't escape, and a skill description that also fired on non-safety work. Give an A/B a control arm.
- A measured gain within noise doesn't pay for added machinery. Close the issue with the numbers and push the branch for reference.
- A flake that shows once gets its mechanism found before landing. A fake that started 40 ms faster exposed a SIGPIPE race.
