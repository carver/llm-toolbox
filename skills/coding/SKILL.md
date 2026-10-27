---
name: coding
description: How to code: what to weigh and in what order, testing, commits, merge danger. Load before writing or changing code, tests or build config, in any project.
---

# Coding

## General principles

You code very fast. So when planning, weigh development cost ~0. Instead, prefer in rough order:

1. Quality and correctness
2. Long term maintainability
3. Security
4. Performance
5. Scalability

Make it work, then make it good. While implementing, the quality you own is the tests:

- Red / Green testing
- Great coverage: e2e and property testing when appropriate (like python hypothesis)
- Fast tests, so the whole suite runs on every commit:
  - Run them in parallel from the start (pytest: `pytest-xdist` with `-n auto --maxprocesses=8` in `addopts`; `-n0` for one file).
  - Avoid spawning an interpreter when another option works: each one pays its imports again, often most of a second. When a test needs one, have it import only the light module it uses.
  - Wait for a condition by polling it: a fixed sleep costs its full length on every run.
  - Build an expensive fixture (a git repo, a rendered figure) once per module, and copy it into each test.
  - To find what's slow, start with a sampling profiler (py-spy for Python), which disturbs what it measures far less than cProfile or strace; compare timings with `hyperfine`, not repeated runs by hand.

Review holds the code to [standards.md](standards.md), in the same order, and a refactor pass applies what it finds. `/build` runs that sequence.

## Good habits

Make logically isolated commits.
Before every commit or amend, check `git diff --cached --stat`. Unstage anything you didn't write: the user may have edits in progress in the same tree.
Read a commit's whole output, or check its exit status: when a hook fails, nothing is committed, and a `| tail` can hide that. The agent check prints one line when it passes and ends with `COMMIT REFUSED` when it fails, so there's no output worth filtering. Chain a series of commits with `&&`, never `;`: after a refused commit, the next one sweeps in what was staged for it.
Before an unpushed branch is merged, fold fixes into the commits they fix (`git commit --fixup=<sha>`, then `GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base>`; plain interactive rebase isn't available here). History shouldn't show detours.

Before you start a change, and again on the finished diff, judge its merge danger:

- Door: a revert walks back a two-way door. Data loss, destructive migrations, messages sent to people, published releases and rewritten shared history are one-way doors.
- Blast radius: what breaks if it's wrong. It's local when only my own use suffers, even across projects, as with my global guidelines and skills; wide when it reaches other people, published packages, production systems or shared data.

A two-way door with a local blast radius stays fast: commit on the current branch as usual. Anything else gets my review first. Say so before starting, work on a branch, open the PR with `/pr`, and stop there instead of merging. If the finished diff turns out riskier than you judged, move the unpushed commits onto a branch. A repo with no GitHub remote keeps the branch local, and the `/pr` body goes in chat.

Before putting code into a folder you didn't create, especially in another repo, read its README or listing to confirm the folder is meant for that kind of file. For example, a `spec/` holding a format and fixtures is no place for a library.

Maintain high standards: if you notice issues, fix them. Examples: lint, test flakiness.

Use /diagnosing-bugs to debug.

Before you read a dependency's source to learn how it behaves, or start debug prints for a surprise, run `sofa search` on the library and the symptom (e.g. `sofa search "egui ctrl+v image paste"`). Another agent may have dug it out already.

Use pre-commit hooks to lint and type-check the code.

Use CI tools that check lint and tests on main and new pull requests.

A pre-commit hook ends every refusal with a `COMMIT REFUSED: <why>` line, and for an agent (`$AI_AGENT` set) prints nothing on a pass, so its output never needs filtering: agents grep a noisy pass, and the grep hides a refused commit. A person committing still sees the checks run. For the pre-commit framework, `pre-commit-run --install REPO` adds a hook that does this, and also refuses first when `.pre-commit-config.yaml` has unstaged changes, which pre-commit won't run with. A pre-commit hook must finish in under 5 seconds all-in. Anything slower, like end-to-end tests or a from-scratch dev install, goes in CI; create a CI workflow if the repo has none.

Your commits get a slower gate too: when `$AI_AGENT` is set, the repo's pre-commit hook first runs `agent-pre-commit`, an executable beside it (same hooks directory), and fails the commit when it fails. Put the checks CI runs in it, the test suite first, so CI failures show up at your commit instead of after the push. Run independent checks (tests, type checker, linters) at once, each into a log of its own shown when it ends, and have the hook's exit trap kill the background ones, which ignore Ctrl-C and would outlive it. Add one to a repo that has CI and no `agent-pre-commit`, then run `agent-commit-gate --install REPO` to add the line that calls it to the pre-commit hook. Never skip it with `--no-verify`.

CI builds the dev environment from scratch the documented way (e.g. a fresh venv with `pip install -e '.[dev]'`) before running the checks. When you change packaging, dependencies or dev tooling, run that same from-scratch install yourself before committing. Code that only ever ran from the source tree can hide an uninstallable package.

When you do token-heavy, deterministic work, consider whether you will do it multiple times.
If so, find a tool to help for next time.
Research what's out there. If you find a valuable one, use it.
Perhaps write a thin wrapper if it would make for a tighter interface.
I want to be able to use these tools too, so make it's documented and that -h shows options.
If no tools meets the goal, consider writing your own from scratch.

## Addressing uncertainty

Usually, I want you to ask me if there is any ambiguity in a request.
But sometimes you won't realize the uncertainty until the middle of implementing.

Sometimes, I will tell you that I'm going offline and to get as far as you can.

In either case, keep an uncertainties.md file in the repository.
For each new uncertainty, include:
- the list of possible options that could be taken
- why you made the choice you made
- your best counter-argument for a different choice

The file holds only open choices, not a history. Once I confirm a choice, delete its entry.
