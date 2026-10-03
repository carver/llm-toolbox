---
name: build
description: Orchestrate a code change through subagents. An implementer makes it work, /code-review checks it, you triage the findings, and a refactor agent makes it good. Use for any code change beyond a trivial edit.
---

# Build

You are the orchestrator. You hold the request, the conversation and the decisions; the subagents hold the code. Implementation is overloaded (explore, change, debug), so it carries no coding standards. Review is underloaded, so it carries them all. Make it work, then make it good.

Keep your own context light: read what the subagents report, the commit list and the check results, not the code.

## 1. Brief

First find the **design forks**: places where the request leaves a choice open and a reasonable implementer could go either way. Ask the user about each one, leading with your recommendation, before any code exists. Use the `grilling` skill when the request itself is ambiguous.

Write the **brief**, the one message every subagent starts from. A fresh subagent has seen none of this conversation, so the brief carries all of it that matters:

- the repo path, and the goal in the user's words
- every decision and constraint the user stated
- the **seams**: the public interfaces the tests go through, per `/tdd`. Agree them with the user now. If the user is offline, pick them and record the choice in `uncertainties.md` (see the `coding` skill)
- the merge danger, judged per the `coding` skill. Create the branch now if it needs one

Record the **base**: `git rev-parse HEAD`. Also record the **repo fingerprint**: `git config --file "$(git rev-parse --git-common-dir)/config" --list | sha1sum` and `git for-each-ref | grep -v <work branch> | sha1sum`. Worktrees share both, so a subagent can damage them from anywhere.

Done when every design fork is answered, the user has confirmed the seams (or you've recorded them) and the brief would let someone who never saw this chat do the work.

## 2. Implement

Spawn a fresh general-purpose subagent with the brief, plus:

> Load the `coding` and `tdd` skills. Make it work: red/green at the seams above, then the code that passes them. Leave code quality to the review that follows. Commit per `coding`. When a choice would break a rule in the brief, or widen what a failure stops, report it as a question instead of picking. Report: the commits, the check command and its result, and any uncertainty you resolved on your own.

Keep its agent ID: spec gaps go back to it in step 4.

Done when `git log <base>..HEAD` shows its commits and the repo's checks pass when you run them.

## 3. Review

Run `/code-review` with `<base>` as the fixed point and the brief as the spec.

## 4. Triage

Sort every finding into one of:

- **Spec gap**: missing or wrong behaviour. Send it back to the implementer with SendMessage, and rerun its checks when it reports.
- **Fix**: a documented-standard violation, or a judgement-call smell whose fix makes this code clearer.
- **Skip**: a judgement call that doesn't pay here, or one outside this change. Give a one-line reason.
- **Ask**: a finding that changes a decision the user made. Ask the user before going on.

Show the user the triage as a short list, then go on without waiting unless something is under Ask.

Done when every finding has a bucket, every spec gap is fixed with checks green, and every Ask is answered.

## 5. Refactor

Record the **refactor base**: `git rev-parse HEAD`. Spawn a fresh general-purpose subagent with the brief, plus:

> Load the `coding` skill. Refactor the change since `<base>`. Apply these review findings, nothing else: <the Fix list>. Behaviour stays the same: the tests pass unchanged unless a finding is about a test. Commit per `coding`. Report what you changed for each finding, and any you couldn't apply and why.

## 6. Verify

- Run the repo's checks.
- Read `git diff <refactor base>..HEAD -- <test files>`. A refactor that loosened an assertion or deleted a test to stay green is a failure: send it back or revert it.
- `git log <base>..HEAD` shows logically isolated commits.
- The repo fingerprint matches the one from step 1. On a mismatch, find what changed and tell the user before anything else.

Done when checks pass, the fingerprint matches, and every test change traces to a finding.

## 7. Report

Tell the user: the commits, the findings you skipped and why, and any new entries in `uncertainties.md`.
