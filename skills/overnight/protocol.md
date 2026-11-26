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
- A failure that shows only under load gets its mechanism reported. It isn't retried blindly.

## The owner's answers for this run

- Opted in: `<costly items approved>`
- Declined: `<items skipped>`
- End as drafts even when ready: `<sensitive areas>`
- Other: `<stale claims to take over, anything else>`

## Log

`log.md`: one line per issue, `#N | end state | sha or PR | rerun needed? | note`.
