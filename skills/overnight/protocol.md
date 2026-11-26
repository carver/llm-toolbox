# Overnight protocol: <repo>, <date>

The orchestrator and every agent follow this file. The orchestrator fills in the placeholders before the loop starts.

Repo: `<owner/repo>`, main tree `<path>`, default branch `<branch>`.

## End states

Exactly one per issue, as the `overnight` skill defines them: landed, draft + question (more than 55% sure of the recommended option), question (55% or less), findings, or gave up.

## Rules

- Never push the default branch. Push only topic branches for draft PRs.
- The repo's own rules apply: `<AGENTS.md / CLAUDE.md, CODING_STANDARDS.md>`.
- Owner-only work is skipped: anything run on servers or from the owner's machine, credentials, releases. A change to a file that reaches a server only through the owner's rerun says so in the commit, the issue and the log line.
- Umbrella issues are worked through their sub-issues.
- An issue labelled `needs-owner` is skipped unless the owner has replied since the question.
- Public repos: no secrets, private data or test-account details in anything posted.
- A test that fails once, under load or not, keeps its full output in a log file. Find its mechanism, report it, and rerun the test many times (a loop of 50 or more, or `HYPOTHESIS_PROFILE=deep`) before `READY FOR REVIEW`: a fake that never read stdin failed 3 times in 4000 calls.
- Every `curl` gets `--max-time`, and every other fetch a timeout. A hung fetch shows only as an agent that stopped reporting.

## The owner's answers for this run

- Opted in: `<costly items approved>`
- Declined: `<items skipped>`
- End as drafts even when ready: `<sensitive areas>`
- Other: `<stale claims to take over, anything else>`

## Attribution

Commit messages end with:

```
<commit attribution lines, such as Co-Authored-By and session lines>
```

PR bodies end with:

```
<PR attribution lines>
```

## Log

`log.md`: one line per issue, `#N | end state | sha or PR | rerun needed? | note`, where a timing left for the end of the run goes under rerun as its exact command, plus, in Workflow mode, a `ready_for_review` line before it from an implementer that stopped for review. An issue's last line holds its end state.
