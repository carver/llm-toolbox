---
name: mutants-triage
description: Triage a run of llm-toolbox's .github/workflows/mutants.yml (mutate-check --diff) and pick the 1 to 3 surviving mutants most worth a test, if any. Use when asked to analyze, triage or report on a mutants run, or when /upskill lists runs not yet triaged.
---

# Mutants triage

filedrawer's `.claude/skills/mutants-triage` is the same flow for its cargo-mutants runs; the tools differ, so each repo keeps its own.

A daily run breaks the Python and shell lines landed since the last covered run and runs the whole suite against each mutant; a survivor is code no test checks.
Most survivors don't matter.
The job is to find the **few gaps** where a wrong line would let something real through, and turn each into an issue a later session can close with a test.
Zero is a valid answer.

## 1. Find the runs

`mutants-pending` lists the runs not yet triaged, newest first, and the open `Tests:` issues.
Triage every listed run, oldest first: each covers different lines.
A run that failed judged nothing (the suite failed unmutated on the runner): read its `mutate.log`, fix the suite, and mark it triaged with that fix.

Done when you hold each run's URL.

## 2. Fetch the results

Download onto the host disk, never the sandbox's:

```sh
D=/home/carver/code/claudedev/cache/mutate-check/run-RUN
gh run download RUN --repo carver/llm-toolbox -n mutants -D "$D"
```

`$D` holds `survived.txt` (one mutant a line, `FILE:LINE: what changed`), `mutate.log` and `range.txt` (the base and head commits).
The run's summary page says how many it judged and whether it was cut off at its time limit; mutants a cut-off run left out are not retried.

## 3. Pick the gaps

Read the code at each survivor's line at the run's head (`git show HEAD_SHA:FILE`), then rank by **consequence**: what breaks if that line were wrong, in this order.

1. A guard against losing work or touching the wrong thing: wt-ship's refusals, safekill's one-match rule, agent-commit-gate, the hooks, `build-state finish`'s stops.
2. A decision the owner settled in `docs/design.md`.
3. What a tool tells an agent to do next (a refusal's fix, a warning).
4. Messages and formatting.

Pass over:
- a mutant that changes no behaviour (an equivalent mutant), or only the wording of a message;
- code whose tests skip on the runner: `bin/web-shot` and `bin/gui-shot` (no browser or display), `bin/paper`'s pdftotext path, and `heavy`'s lane sharing (its tests need 4 cores);
- code an open worktree or branch is changing (`git worktree list`), which its own tests may cover soon.

Several survivors in one function are one gap.
Done when you hold at most 3 gaps, each with the survivors that show it and one sentence on what a wrong line would let through.

## 4. File each gap

One issue each on carver/llm-toolbox, titled `Tests: <the behaviour>`, holding:
- the run's link and head commit;
- the survivors, as `survived.txt` prints them;
- what a wrong line would let through;
- a `- [ ]` item per test, at the seam the tool's test file already uses.

The session that closes it shows each new test catches its survivor with `mutate-check` on that one mutant (`mutate-check -h`), which takes seconds.

Then comment on the tracking issue (#1, named in the workflow's header) with a line `Triaged run: <run URL>` for each run you covered, the counts from each run's summary, and the issues filed, or that none was worth one. `mutants-pending` reads those lines, from the owner and collaborators only.
Done when the comment names every run you triaged.
