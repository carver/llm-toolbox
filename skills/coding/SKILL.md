---
name: coding
description: Coding standards, what to weigh and in what order, testing, commits. Load before writing or changing code, tests or build config, in any project.
---

# Coding

## General principles

You code very fast. So when planning, weigh development cost ~0. Instead, prefer in rough order:

1. Quality and correctness
    - Red / Green testing
    - Great coverage: e2e and property testing when appropriate (like python hypothesis)
    - Architecture that favors testability
    - Robust to unexpected scenarios & inputs
    - Avoid code smells
    - Code should explain itself, making comments often unnecessary
2. Long term maintainability
    - Simplicity
    - Readability
    - Small updates require small changes
    - Structure for efficient agent understanding
3. Security
    - Limit attack/abuse surface area
    - Defense in depth
4. Performance
5. Scalability

## Good habits

Make logically isolated commits.
Before every commit or amend, check `git diff --cached --stat`. Unstage anything you didn't write: the user may have edits in progress in the same tree.
Before an unpushed branch is merged, fold fixes into the commits they fix (`git commit --fixup=<sha>`, then `GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base>`; plain interactive rebase isn't available here). History shouldn't show detours.

Before you start a change, and again on the finished diff, judge its merge danger:

- Door: a revert walks back a two-way door. Data loss, destructive migrations, messages sent to people, published releases and rewritten shared history are one-way doors.
- Blast radius: what breaks if it's wrong. It's local when only my own use of this repo suffers; wide when it reaches other people, published packages, production systems or shared data.

A two-way door with a local blast radius stays fast: commit on the current branch as usual. Anything else gets my review first. Say so before starting, work on a branch, open the PR with `/pr`, and stop there instead of merging. If the finished diff turns out riskier than you judged, move the unpushed commits onto a branch. A repo with no GitHub remote keeps the branch local, and the `/pr` body goes in chat.

Before putting code into a folder you didn't create, especially in another repo, read its README or listing to confirm the folder is meant for that kind of file. For example, a `spec/` holding a format and fixtures is no place for a library.

Maintain high standards: if you notice issues, fix them. Examples: lint, test flakiness.

Use /diagnosing-bugs to debug.

Use pre-commit hooks to lint and type-check the code.

Use CI tools that check lint and tests on main and new pull requests.

A pre-commit hook must finish in under 5 seconds all-in. Anything slower, like end-to-end tests or a from-scratch dev install, goes in CI; create a CI workflow if the repo has none.

Your commits get a slower gate too: `agent-commit-gate` runs the repo's `agent-pre-commit`, an executable beside the pre-commit hook (same hooks directory), before every `git commit` you run, and blocks the commit when it fails. Put the checks CI runs in it, the test suite first, so CI failures show up at your commit instead of after the push. Add one to a repo that has CI and no `agent-pre-commit`.

CI builds the dev environment from scratch the documented way (e.g. a fresh venv with `pip install -e '.[dev]'`) before running the checks. When you change packaging, dependencies or dev tooling, run that same from-scratch install yourself before committing. Code that only ever ran from the source tree can hide an uninstallable package.

When you do token-heavy, deterministic work, consider whether you will do it multiple times.
If so, find a tool to help for next time.
Research what's out there. If you find a valuable one, use it.
Perhaps write a thin wrapper if it would make for a tighter interface.
I want to be able to use these tools too, so make it's documented and that -h shows options.
If no tools meets the goal, consider writing your own from scratch.

## Code Quality

If small updates require a large change, and fixing it isn't straightforward, consider recommending that I run the architectural improvement skill.

Some ideas for how to think about keeping clean code, and avoiding code smells:

- Group functions into files based on related functionality
- No god functions/classes.
- Make sure the code is documented appropriately so that it is easy to pick up.
- Minimize the following gotchas:
  - Casts to get around type checking
  - Extra comments that a human wouldn't add.
  - Heavy nesting
  - If/Else ladders
  - A ton of helper methods

## Addressing uncertainty

Usually, I want you to ask me if there is any ambiguity in a request.
But sometimes you won't realize the uncertainty until the middle of implementing.

Sometimes, I will tell you that I'm going offline and to get as far as you can.

In either case, keep an uncertainties.md file in the repository.
For each new uncertainty, include:
- the list of possible options that could be taken
- why you made the choice you made
- your best counter-argument for a different choice
