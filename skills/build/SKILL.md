---
name: build
description: Carry a code change through implement, review and refactor. You write and refactor the code yourself; a review subagent checks it against /code-review's two axes; you triage the findings. Use for any code change beyond a trivial edit.
---

# Build

You hold the request, the conversation and the decisions, so you write the code yourself. Review runs in a subagent, so fresh eyes judge it. Make it work, then make it good: implementation carries no coding standards beyond the brief, and review carries them all.

If a change looks too big to hold in your context (many files, or an already long session), stop and ask the user whether to hand implementation to a subagent. Expect this to be rare. The user makes that call, not you.

## 1. Brief

First find the **design forks**: places where the request leaves a choice open and a reasonable implementer could go either way. Ask the user about each one, leading with your recommendation, before any code exists. Use the `grilling` skill when the request itself is ambiguous.

Write the **brief** to a file named for the change, such as `brief-<topic>.md`: subagents running builds at once share one scratchpad, and a plain `brief.md` gets overwritten. It is the spec the reviewers check the code against, and a reviewer has seen none of this conversation, so it carries everything that matters:

- the repo path, and the goal in the user's words
- every decision and constraint the user stated, including changes made mid-build
- when the change comes from an issue or a spec, each of its items, from the body and every comment (`issue-todo read N` prints both), marked covered, or deferred with the user's answer, so the Spec review checks each one. An item you would defer is a design fork. Reason: an issue's comment asked for three things, and a brief dropped one without anyone deciding to
- the **seams**: the public interfaces the tests go through, per `/tdd`. Pick them yourself and write them down; this replaces `/tdd`'s step of confirming seams with the user. Ask only when the choice of seam is itself a design fork, such as a new public interface whose shape is open
- the commits, in order, each with the items it covers, when the change is more than one
- the merge danger, judged per the `coding` skill. Create the branch now if it needs one
- the skills you loaded for the change, each with its SKILL.md path, so the reviewer can read them without searching

When the change adds a **gate** (a rule that refuses, flags, rate-limits or verifies something) or a dependency on an outside service, review the brief before any code exists. Spawn one subagent in the foreground with the brief's path and this ask: "Read the brief. For each gate or outside service it adds, find an honest case the rule wrongly hits; a way someone set on getting past it does so; and what happens when the service or data it relies on degrades: down, slow, rate-limited, changed in shape, or quietly wrong. For each deployment the code runs in, such as production, staging and dev, say what the rule or job does there and whom it reaches. Also find any decision that a later decision in the brief undoes. Report each as the case, the rule it concerns, and a fix; edit nothing." Point it at code by commit (`git show SHA:path`), not by a worktree's path: a worktree that lands while it reads is removed under it. Take each finding that changes a decision to the user as a design fork, leading with your recommendation, and fold the rest into the brief. Reason: on a change of link checks, side agents found four such cases only after the code had landed, and each one cost another build; on a daily email to the owner, that staging would mail them about CI's test entries surfaced only after the code was written.

Run `build-state start` in the repo. It records the **base** (HEAD, or `--base REV` when your commits for this change already started) and the **repo fingerprint** in the git dir; `build-state show` prints them again after a context reset.

When the change writes or rewrites text that agents or people read and act on (an installer, a skill, a help page, a reply's wording), review the brief the same way, with this ask: "Read the brief. Check each decision it makes against the text of the issue or spec it comes from, body and every comment, and against the design docs the repo points to. Report each decision one of them argues against, with the line that does; edit nothing." When the change also adds a gate, one subagent takes both asks. Reason: a brief chose to show agents a link to public GitHub issues, which the issue's own text and the design doc ruled out; only the review of the finished code caught it, and a fix cycle followed.

Done when every design fork is answered, the brief names the seams and accounts for every item of the issue or spec the change comes from, the brief review's findings are answered when the change adds a gate or writes text people or agents act on, and the brief would let a reviewer who never saw this chat judge the change.

## 2. Implement

When the brief names more than one commit, run steps 2 to 6 once for each, in order, and step 7 once at the end. Before each commit after the first, run `build-state start` again, so that commit's review, refactor base and fold cover it alone. Reason: a build of two commits was written in one tree and split afterwards; the review's fixups to the first commit then conflicted with lines the second had rewritten, and the hand fold left the first commit unable to build.

Load the `coding` and `tdd` skills. Then cycle: stub the new names (empty functions and methods), write a test at a seam, or a few at once, run them and see every one red on its assertions, then write the code that turns them green. A test that is green on its first run never went red: delete it, or change it until it fails for the reason you meant. A test the request asks for on code that already exists goes red by breaking that code briefly, with `mutate-check` (`-h`), which puts the code back. Leave code quality to the review that follows. Commit per `coding`. In a repo whose commit hook runs the checks, the commit is the check run: before it, run only the tests you wrote or touched. Reason: a full suite and clippy run by hand before each commit doubled the time spent queued, since each commit's hook ran them again. Keep every commit local until step 6 is done, even in a repo whose rules say to push each commit. A change that belongs to an earlier commit of this build, such as a fix, a case it missed, or a change the user asks for once they see it, goes in a `fixup!` of that commit (`git commit --fixup=<sha>`), even while you work on a later one; a later commit changes that code only for its own purpose. Reason: a later commit also fixed a case in an earlier commit's tool and its test, so the review's fixups of the earlier commit conflicted with it, and `build-state finish` couldn't fold them. When a choice would break a rule in the brief, or widen what a failure stops, ask the user instead of picking.

When the change alters behaviour that any text describes, sweep the workspace for text it made stale. That covers a tool's or script's behaviour and options, and also a server's or library's, which a doc, a design entry, a bundled skill agents read, a template or `-h` text may describe: a rule that now also removed series left a skill saying it removed only entries. Run `cd /home/carver/code/claudedev && rg -n --hidden -g '!{cache,keep}/**' -g '!*-wt-*/**' -g '!.git/' -e NAME -e 'OLD PHRASE' .`, with the tool's or feature's name and the key phrases of its old behaviour. Search `.` from the workspace root: rg matches those globs against paths relative to the path it searches, so given the absolute path it walks all of cache/ and keep/, over a million files, and takes minutes where this takes a second or two. Fix each stale hit, be it a doc, a header comment or `-h` text, or list it in the brief as out of scope. Reason: in one session, 6 of 7 reviews found stale text in files nothing pointed to, across three repos, and each cost a review round and a fixup.

When the change adds or changes Python or shell code, run `mutate-check --diff <base> -- <the targeted test command>` after its commits, with the base `build-state show` prints, as a background call under `heavy --long` (`mutate-check -h`), and leave the tree alone until it ends. Add a test for each survivor that matters, then run it again. Reason: in one session all three reviews ran mutate-check on the builder's diff and found mutants its tests let through, each costing a fix round.

Done when `git log <base>..HEAD` shows your commits, the repo's checks pass, and each stale hit is fixed or listed in the brief.

## 3. Review

Spawn one review subagent in the foreground, and wait for its report before doing anything else. Its prompt is what `build-state review-prompt BRIEF` prints: a short pointer to the file that holds the whole prompt, which you pass on as it is, without reading the file. That file gives `<base>` as the fixed point, the brief file as the spec, and `/code-review`'s Standards and Spec briefs with their standards sources and smell baseline, reported under separate headings. Append to the pointer any ask this change needs, such as a sweep for leftovers of a rename. The prompt pins the commit under review; don't rebase or rewrite the tree until the report is in, since the reviewer runs tests in it.

## 4. Triage

Sort every finding into one of:

- **Spec gap**: missing or wrong behaviour. Fix it now, red/green, as a `fixup!` of the commit it belongs to, and rerun the checks. `build-state finish` refuses any other commit between the review and the refactor base, since no review saw it.
- **Fix**: a documented-standard violation, or a judgement-call smell whose fix makes this code clearer.
- **Skip**: a judgement call that doesn't pay here, or one outside this change. Give a one-line reason.
- **Ask**: a finding that changes a decision the user made. Ask the user before going on.

Show the user the triage as a short list, then go on without waiting unless something is under Ask.

Done when every finding has a bucket, every spec gap is fixed with checks green, and every Ask is answered.

## 5. Refactor

Run `build-state refactor` to record the **refactor base**. Apply the Fix list, nothing else. Behaviour stays the same: the tests pass unchanged unless a finding is about a test. Commit each as a `fixup!` of the commit it tidies, so `build-state finish` folds it in. Reason: wt-ship runs the full check on each commit it lands, and in one landing 5 of 8 commits (review fixes and refactors) only refined code an earlier commit of the landing added, each a run of 96 to 160 seconds.

## 6. Verify

- Run the repo's checks, unless its commit hook runs them: then each commit ran them, and wt-ship runs the full check on every commit when it lands.
- Run `build-state finish`. It refuses a build whose review (`review-prompt`) or refactor base (`refactor`) wasn't recorded, or that holds a commit, between the two, that isn't a fix: run the step it names. It folds each `fixup!` commit into the commit it fixes and leaves every other commit separate, then lists the commits since the base and the test files changed since the refactor base, and warns of any commit that only refines an earlier one of the landing: run `build-state fold` for those before landing, since wt-ship runs the full check on each. It runs no check: a commit a fold rewrote gets its check from wt-ship. When it fails because a ref or config key moved, or reports a config key or remote-tracking ref that changed, find what changed and tell the user before anything else.
- Read `git diff <refactor base>..HEAD` for each listed test file. Every test change must trace to a finding; a loosened assertion or deleted test that keeps things green is a failure to undo.
- The commit list shows logically isolated commits.
- Push now, if the repo pushes: the change is reviewed and refactored.

## 7. Report

Tell the user: the commits, the findings you skipped and why, and any new entries in `uncertainties.md`.

A change the user then asks for to these commits before they land is a `fixup!` of the commit it changes, made in a build of its own.
