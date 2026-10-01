---
name: upskill
description: End-of-session retrospective. Mines the session for mistakes, waste and repetition, then pitches automation, skills and doc fixes.
disable-model-invocation: true
---

# Upskill

This session is about to end. Run a retrospective on it: find what cost time or tokens, and pitch changes that make the next session cheaper.

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

## 3. Pitch

Make no changes yet. Present a numbered list, biggest payoff first. Each item: the finding with its evidence from the transcript, the proposed fix, and its size. Ask questions wherever you're unsure. Then stop: the user picks which ones to follow through on now.

Close by suggesting `/retro`, Matt Pocock's skill, for a second pass over the same session. It looks for guardrails rather than waste: automated checks, rules for the reviewer agent, and steering files to trim.
