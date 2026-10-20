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

Run `build-state start` in the repo. It records the **base** (HEAD) and the **repo fingerprint** in the git dir; `build-state show` prints them again after a context reset.

Done when every design fork is answered, the user has confirmed the seams (or you've recorded them), and the brief would let a reviewer who never saw this chat judge the change.

## 2. Implement

Load the `coding` and `tdd` skills. Then cycle, one test at a time: write one test at a seam, run it and see it red, then write the code that turns it green. A test that is green on its first run never went red: delete it, or change it until it fails for the reason you meant. Leave code quality to the review that follows. Commit per `coding`. When a choice would break a rule in the brief, or widen what a failure stops, ask the user instead of picking.

Done when `git log <base>..HEAD` shows your commits and the repo's checks pass.

## 3. Review

Run `/code-review` with `<base>` as the fixed point and the brief file as the spec. When `git diff --shortstat <base>` shows 30 changed lines or fewer (insertions plus deletions), spawn one subagent instead, given both of `/code-review`'s sub-agent briefs (Standards and Spec), and have it report the two axes under separate headings.

Code that drives paid or rate-limited runs, such as an experiment or a sweep, gets only a pilot of a few cells before this review. The full run waits for triage, because a bug found after a full run means paying for that run twice.

## 4. Triage

Sort every finding into one of:

- **Spec gap**: missing or wrong behaviour. Fix it now, red/green, and rerun the checks.
- **Fix**: a documented-standard violation, or a judgement-call smell whose fix makes this code clearer.
- **Skip**: a judgement call that doesn't pay here, or one outside this change. Give a one-line reason.
- **Ask**: a finding that changes a decision the user made. Ask the user before going on.

Show the user the triage as a short list, then go on without waiting unless something is under Ask.

Done when every finding has a bucket, every spec gap is fixed with checks green, and every Ask is answered.

## 5. Refactor

Run `build-state refactor` to record the **refactor base**. Apply the Fix list, nothing else. Behaviour stays the same: the tests pass unchanged unless a finding is about a test. Commit per `coding`.

## 6. Verify

- Run the repo's checks.
- Run `build-state finish`. It folds each `fixup!` commit into the commit it fixes and leaves every other commit separate, then lists the commits since the base and the test files changed since the refactor base. When it fails because a ref or config key moved, find what changed and tell the user before anything else.
- Read `git diff <refactor base>..HEAD` for each listed test file. Every test change must trace to a finding; a loosened assertion or deleted test that keeps things green is a failure to undo.
- The commit list shows logically isolated commits.

## 7. Report

Tell the user: the commits, the findings you skipped and why, and any new entries in `uncertainties.md`.
