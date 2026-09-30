---
name: todone
description: Carry out a coding to-do from the knowledge-dump vault, put its Why and source in the commit message, and record the result back on the to-do.
disable-model-invocation: true
---

# Todone

A to-do in the knowledge-dump vault is kept because its reason matters later. This skill does the work and leaves the reason in two places that point at each other. The commit carries the Why and the source. The vault's to-do records the commit and what you learned along the way.

## The vault

Clone it into your scratchpad: `git clone -q git://host.docker.internal/notes <scratchpad>/v`. The sandbox reaches it only this way, never through the mount. To change it, commit in the clone and run `git push --force origin HEAD:refs/heads/agent`. The host merges that branch and prints "merged into main". `knowledge-dump/docs/silverbullet.md` has the task and attribute syntax.

A to-do is an open task on a topic page, with its context on indented lines under it:

```markdown
- [ ] Install retro skill, run it regularly #queue [added: 2026-09-30]
  Why: Pocock reviews the process along with the code. A comment written twice means the skill files should change.
  Source: [Fixing the PR Bottleneck — Matt Pocock, AIHero - YouTube](https://m.youtube.com/watch?v=LlgiOCmFG_w&t=1238) at 20:38
```

The `Why:` line may be missing.

## 1. Pick the to-do

If the user named one, find its task line. Otherwise list the open tasks on every page except `Triage`, `Queue` and `Inbox/`, keep the ones that are coding work (a change to code, a skill, a config or a guideline file), and offer the 3 to 5 most actionable with AskUserQuestion, each labeled with its page and a short summary.

Done when one task line is chosen and you know which repository the work happens in. Ask if the task doesn't make that clear.

## 2. Settle the Why

The Why is the reason the user took this on, in one or two sentences. Take it from, in order:

1. The task's `Why:` line.
2. A `Why:` the user wrote in the task text or anywhere in its nested lines.
3. The source. For a YouTube link, run `yt-transcript '<url>'`, which prints the captions from 90 seconds before the timestamp to 60 seconds after (`-h` for the window options). For other links, read the page around the passage.
4. The user, if there is no source or it doesn't give a reason.

Draft it in the user's framing: what persuaded them, not a summary of the source. Show the draft and ask the user to confirm or correct it.

Done when the user has confirmed the Why.

## 3. Do the work

Work in the target repository by its own CLAUDE.md or AGENTS.md, with the `coding` skill. Note any **finding**: a surprise you'd want to know the next time you revisit this decision, such as the work going differently from what the to-do said, and why. Most to-dos have none. Skip small lessons from along the way.

Commit messages for the work end with the Why and the source:

```
Add the retro skill

Runs at the end of a session and pitches fixes to the skills and docs.

Why: a review comment written twice means the process is wrong, not just
the code, so review the skill files too.
Source: Fixing the PR Bottleneck, Matt Pocock
  https://m.youtube.com/watch?v=LlgiOCmFG_w&t=1238 (20:38)
```

If there were findings, the last commit's message states the ones that explain the code.

Done when the work is committed and the repository's checks pass.

## 4. Record it in the vault

Refresh the clone (`git fetch && git reset --hard origin/main`), then edit the task:

- Tick it and add a `[done: YYYY-MM-DD]` attribute.
- Add a `Why:` line if it had none.
- Add `Done in: <repo>@<short sha>`, or `<repo>@<first>..<last>` for several commits.
- If there were findings, show them to the user as proposed `Learned:` lines and add only the ones they approve.

```markdown
- [x] Install retro skill, run it regularly #queue [added: 2026-09-30] [done: 2026-10-02]
  Why: ...
  Source: ...
  Done in: llm-toolbox@4c1e2a9
  Learned:
    - Renamed it from /retro to /retro-session: Matt Pocock's skill already has that name.
```

If an approved finding is a lesson beyond this to-do ("when I face X, consider Y"), offer to file it as a Cue page under `Cues/` too.

Commit and push. Done when the push prints "merged into main".
