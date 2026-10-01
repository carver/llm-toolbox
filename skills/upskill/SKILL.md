---
name: upskill
description: End-of-session retrospective. Mines the session for mistakes, waste and repetition, then pitches automation, skills and doc fixes.
disable-model-invocation: true
---

# Upskill

This session is about to end. Run a retrospective on it: find what cost time or tokens, and pitch changes that make the next session cheaper.

Also look for important conventions in the repositories that might be ill-formed.

## 1. Mine the history

Walk the whole transcript, first turn to last. Collect every instance of:

- **Mistakes**: wrong assumptions, retracted answers, reverted edits, user corrections.
- **Waste**: dead ends, oversized reads or outputs, searches that took several tries to land.
- **Repetition**: the same command sequence, lookup or explanation done more than once.
- **Missing context**: anything you learned by trial, by asking the user, or by digging through code, that a doc could have told you.

Done when every failed tool call and every user correction is accounted for, either as a finding or dismissed with a reason.

## 2. Match each finding to a fix

- **Deterministic task**: automate it. Research existing libraries and tools first. Prefer, in order: one that solves it out of the box; a thin layer around one; an extension of a binary or script the project already has; a new Python script in the project's `bin/`. If the tool would pay off across projects, say so: it could live in `llm-toolbox` instead.
- **Repeated task needing judgment**: a project-local skill (`.claude/skills/<name>/SKILL.md`), written with `writing-for-agents`.
- **Missing context**: docs. If the context was written but you missed it, sharpen the pointer to it (a `CLAUDE.md`/`AGENTS.md` line). If a doc was wrong, fix it. If nothing was written, add a doc.
- **Mistake from a habit**: a guideline, skill or memory edit that changes the habit.
- **Hard-won knowledge**: something another agent would also have to dig for may be worth a SOFA post.

Drop one-offs with no cheaper path next time.

## 3. Conventions

Look for candidates for improvement in these categories.

- **Automated checks**: are there automated checks that could catch errors the agent made? Linting, typing, tests, filesystem linters? Read the repo's own check command first (its `package.json`/build-tool `lint`/`check` scripts, its CI workflow), so a check that already exists but sits unwired or silently broken is the finding, not a reinvention. A repo with no **guardrail** (no pre-commit hook and no CI job running its lint/typecheck/test command) is itself a finding: an un-linted repo is a standing missed opportunity, not a neutral default. _Use when_ the agent made a mistake an automated check could have caught, or the repo has no guardrail at all.
- **Coding standards**: should the **reviewer agent** be given a new rule to enforce? Should an existing rule be removed or clarified? Classify the violation first: a **mechanical** one (a fixed syntactic pattern, a banned API, an import shape, a file-location rule) gets a deterministic check, full stop: a custom rule in the repo's own linter, a new pre-commit hook, or a new CI job, whichever the repo's language and existing guardrail make cheapest. Default to building the check over writing the rule. Reserve `CODING_STANDARDS.md` for genuine **judgement calls** (cross-file consistency, "matches the surrounding style," anything no guardrail could ever substitute for). _Use when_ the reviewer agent failed to catch a mistake.
- **Global AGENTS.md**: are there any steering instructions that should be moved to coding standards (or automated checks) instead? _Use when_ the AGENTS.md file is particularly large - in the repo OR the user's global scope.
- **No-ops**: look for instructions in steering files that don't modify the agent's behavior. _Use when_ the steering files are large and unwieldy.
- **Information access**: look for opportunities to increase the agent's access to information. Teeing dev server logs, readonly access to third-party services. _Use when_ a crucial piece of information was not available to the agent.


## 4. Pitch

Make no changes yet. Present a numbered list, biggest payoff first. Each item: the finding with its evidence from the transcript, the proposed fix, and its size. Ask questions wherever you're unsure. Then stop: the user picks which ones to follow through on now.

Close by suggesting `/retro`, Matt Pocock's skill, for a second pass over the same session. It looks for guardrails rather than waste: automated checks, rules for the reviewer agent, and steering files to trim.
