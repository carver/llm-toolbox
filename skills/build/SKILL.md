---
name: build
description: Carry a code change through implement, review and refactor. You write and refactor the code yourself; a review subagent checks it against /code-review's two axes; you triage the findings. Use for any code change beyond a trivial edit.
---

# Build

You hold the request, the conversation and the decisions, so you write the code yourself. Review runs in a subagent, so fresh eyes judge it. Make it work, then make it good: implementation carries no coding standards beyond the brief, and review carries them all.

If a change looks too big to hold in your context (many files, or an already long session), stop and ask the user whether to hand implementation to a subagent. Expect this to be rare. The user makes that call, not you.

## 1. Brief

First find the **design forks**: places where the request leaves a choice open and a reasonable implementer could go either way. Ask the user about each one, leading with your recommendation, before any code exists. Use the `grilling` skill when the request itself is ambiguous.

Write the **brief** to a file. It is the spec the reviewers check the code against, and a reviewer has seen none of this conversation, so it carries everything that matters:

- the repo path, and the goal in the user's words
- every decision and constraint the user stated, including changes made mid-build
- the **seams**: the public interfaces the tests go through, per `/tdd`. Pick them yourself and write them down; this replaces `/tdd`'s step of confirming seams with the user. Ask only when the choice of seam is itself a design fork, such as a new public interface whose shape is open
- the merge danger, judged per the `coding` skill. Create the branch now if it needs one
- the skills you loaded for the change, each with its SKILL.md path, so the reviewer can read them without searching

Run `build-state start` in the repo. It records the **base** (HEAD, or `--base REV` when your commits for this change already started) and the **repo fingerprint** in the git dir; `build-state show` prints them again after a context reset.

Done when every design fork is answered, the brief names the seams, and the brief would let a reviewer who never saw this chat judge the change.

## 2. Implement

Load the `coding` and `tdd` skills. Then cycle: stub the new names (empty functions and methods), write a test at a seam, or a few at once, run them and see every one red on its assertions, then write the code that turns them green. A test that is green on its first run never went red: delete it, or change it until it fails for the reason you meant. A test the request asks for on code that already exists goes red by breaking that code briefly, with `mutate-check` (`-h`), which puts the code back. Leave code quality to the review that follows. Commit per `coding`, and keep every commit local until step 6 is done, even in a repo whose rules say to push each commit. When a choice would break a rule in the brief, or widen what a failure stops, ask the user instead of picking.

Done when `git log <base>..HEAD` shows your commits and the repo's checks pass.

## 3. Review

Spawn one review subagent in the foreground, and wait for its report before doing anything else. Its prompt is what `build-state review-prompt BRIEF` prints: `<base>` as the fixed point, the brief file as the spec, and `/code-review`'s Standards and Spec briefs with their standards sources and smell baseline, reported under separate headings. Append any ask this change needs, such as a sweep for leftovers of a rename. The prompt pins the commit under review; don't rebase or rewrite the tree until the report is in, since the reviewer runs the tests in it.

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
- Push now, if the repo pushes: the change is reviewed and refactored.

## 7. Report

Tell the user: the commits, the findings you skipped and why, and any new entries in `uncertainties.md`.
