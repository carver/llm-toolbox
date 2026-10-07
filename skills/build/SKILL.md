---
name: build
description: Carry a code change through implement, review and refactor. You write and refactor the code yourself; /code-review checks it in subagents; you triage the findings. Use for any code change beyond a trivial edit.
---

# Build

You hold the request, the conversation and the decisions, so you write the code yourself. Review runs in subagents, so fresh eyes judge it. Make it work, then make it good: implementation carries no coding standards beyond the brief, and review carries them all.

If a change looks too big to hold in your context (many files, or an already long session), stop and ask the user whether to hand implementation to a subagent. Expect this to be rare. The user makes that call, not you.

## 1. Brief

First find the **design forks**: places where the request leaves a choice open and a reasonable implementer could go either way. Ask the user about each one, leading with your recommendation, before any code exists. Use the `grilling` skill when the request itself is ambiguous.

Write the **brief** to a file. It is the spec the reviewers check the code against, and a reviewer has seen none of this conversation, so it carries everything that matters:

- the repo path, and the goal in the user's words
- every decision and constraint the user stated, including changes made mid-build
- the **seams**: the public interfaces the tests go through, per `/tdd`. Agree them with the user now. If the user is offline, pick them and record the choice in `uncertainties.md` (see the `coding` skill)
- the merge danger, judged per the `coding` skill. Create the branch now if it needs one

Record the **base**: `git rev-parse HEAD`. Also record the **repo fingerprint**: `git config --file "$(git rev-parse --git-common-dir)/config" --list | sha1sum` and `git for-each-ref | grep -v <work branch> | sha1sum`.

Done when every design fork is answered, the user has confirmed the seams (or you've recorded them), and the brief would let a reviewer who never saw this chat judge the change.

## 2. Implement

Load the `coding` and `tdd` skills. Red/green at the seams, then the code that passes them. Leave code quality to the review that follows. Commit per `coding`. When a choice would break a rule in the brief, or widen what a failure stops, ask the user instead of picking.

Done when `git log <base>..HEAD` shows your commits and the repo's checks pass.

## 3. Review

Run `/code-review` with `<base>` as the fixed point and the brief file as the spec.

## 4. Triage

Sort every finding into one of:

- **Spec gap**: missing or wrong behaviour. Fix it now, red/green, and rerun the checks.
- **Fix**: a documented-standard violation, or a judgement-call smell whose fix makes this code clearer.
- **Skip**: a judgement call that doesn't pay here, or one outside this change. Give a one-line reason.
- **Ask**: a finding that changes a decision the user made. Ask the user before going on.

Show the user the triage as a short list, then go on without waiting unless something is under Ask.

Done when every finding has a bucket, every spec gap is fixed with checks green, and every Ask is answered.

## 5. Refactor

Record the **refactor base**: `git rev-parse HEAD`. Apply the Fix list, nothing else. Behaviour stays the same: the tests pass unchanged unless a finding is about a test. Commit per `coding`.

## 6. Verify

- Run the repo's checks.
- Read `git diff <refactor base>..HEAD -- <test files>`. Every test change must trace to a finding; a loosened assertion or deleted test that keeps things green is a failure to undo.
- `git log <base>..HEAD` shows logically isolated commits.
- The repo fingerprint matches the one from step 1. On a mismatch, find what changed and tell the user before anything else.

## 7. Report

Tell the user: the commits, the findings you skipped and why, and any new entries in `uncertainties.md`.
