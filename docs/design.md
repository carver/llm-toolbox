# Design decisions

Settled choices about how the tools behave, each with its reason and the evidence
for it, and the alternatives that were tried or measured and dropped. Read a tool's
entries before changing how it behaves. Reviving a rejected alternative needs the
owner's agreement, and new evidence.

## heavy

### One job at a time in a lane, on this VM (2026-11-08)

A second check running beside the first buys no throughput here: it only makes each
check slower, past the time limits test runners set. The VM shows 16 vCPUs, but a
full commit check runs no faster on 16 than on 8, and two checks side by side finish
no sooner than the same two one after the other.

The measurement, inside `heavy --alone`, with hyperfine. The guest is a KVM VM that
lscpu shows as 16 single-core sockets, with 15 GB of memory and no swap.
A is filedrawer's full check, `bin/agent-pre-commit` with no CHECK_BASE, after
touching `crates/server/src/lib.rs` so that it recompiles the server crate and
relinks its test binaries, as a commit does. B is llm-toolbox's whole suite,
`.githooks/agent-pre-commit` with no CHECK_BASE. Both at nice 0, each pinned with
taskset.

| Run                                 | vCPUs          | Wall time                    | User CPU |
| ----------------------------------- | -------------- | ---------------------------- | -------- |
| A alone                             | 0-15, 16 jobs  | 173 s (1 run)                | 602 s    |
| A alone                             | 0-7, 8 jobs    | 166 s (2 runs, ±0.3)         | 438 s    |
| B alone                             | 8-15           | 106 s (2 runs, 90 to 122)    | 333 s    |
| A, with B back to back on 8-15      | 0-7, 8 jobs    | 425 s (2 runs, 385 to 465)   | 1324 s   |
| B, beside those two runs of A       | 8-15           | 163 s (6 runs, 109 to 215)   |          |

- The work of the shared phase, 2 runs of A and 6 of B, takes about 970 s one after
  the other (2 × 166 + 6 × 106) and took about 977 s side by side.
- The same check took three times the CPU seconds while the other half of the vCPUs
  was busy (438 against 1324 s), and A on all 16 vCPUs took 37% more than on 8. The
  vCPUs share physical cores on the host, most likely as hyperthread pairs, which the
  guest can't see.
- It isn't memory or disk: in every phase free memory held at about 200 MB with the
  rest page cache, disk reads stayed at 46 to 188 blocks a second, iowait under 1%,
  and steal under 4%.

The host, as the owner reported it the same day: a laptop with an Intel Core i7-1260P,
a hybrid chip of 4 performance cores with two threads each and 8 efficiency cores with
one, 16 threads in all, which are the VM's 16 vCPUs, on one power budget of 28 W base
and 64 W at most. So the vCPUs are 4 fast cores' 8 threads and 8 cores at about half
their speed, and with more than a few threads busy the power limit lowers every
core's clock. That fits each number above: no gain from 8 vCPUs to 16, the same
throughput side by side as one after the other, and three times the CPU seconds for
the same work while the other vCPUs were busy. The machine is bound by its power
budget, not its core count, so only less work, or more machine (a bigger host, or
GitHub's CI), makes checks faster.

So heavy's "% busy", which counts vCPUs, overstates the room left: a lane that looks
half busy may be using most of what the host gives this VM. The levers for faster
landings are fewer full checks per landing and cheaper ones, not more at once.

The script and its raw results (hyperfine JSON, vmstat logs) are in
`/home/carver/code/claudedev/cache/heavy-lane-exp/` on the host disk, outside git.

### `heavy --quick`: seconds of work runs at once on quiet cores, else next (2026-11-09)

The user's design, in their words: "If load is low enough, run outside heavy,
otherwise jump to the front of the line." A targeted test run in filedrawer, a few
tests after a one-file edit, takes 4 to 6 s since filedrawer#35, but it waited behind
every session's full checks, sometimes tens of minutes.

- The agent marks the job (`--quick`), on trust, as the queue works: heavy's history
  can't tell targeted runs apart, since each has its own command line.
- When the normal lane's cores average under 25% busy over 2 s, it runs at once,
  outside the queue, whoever holds the lane (one waiting on the network, say), except
  an `--alone` one, which promises a timing nothing beside it: no slot, no entry. The
  one-job-at-a-time rule above is about checks that take minutes;
  a few seconds beside a holder that leaves its cores idle cost little. It also needs
  4 GiB of memory free and runs first in line for the OOM killer, since a holder's
  quiet moment may hold gigabytes (a review of the design found the case).
- Otherwise it joins the front, after earlier front entries such as a landing's
  checks, without stopping the running job, and keeps watching, so a holder that
  idles lets it through. Heading the lane, it starts at the first quiet sample: a 5 s
  job shouldn't wait 10 s of quiet.
- One that runs past 60 s hears, as it ends, to drop `--quick` for runs like it; a
  warning only.
- heavy-gate lets `heavy --quick -- CMD` run as a foreground Bash call only when the
  call's own timeout is 600000 ms, the tool's longest. Its wait for the running job
  can pass the tool's default limit of two minutes, and the call is then killed
  mid-wait. The gate first left the timeout unchecked, since the user asked that the
  hooks accept `heavy --quick -- CMD`; on 2026-11-09 the user decided otherwise:
  "reject a quick without a timeout." A `timeout` command around heavy doesn't count.
  Nor is 600000 ms a promise: a quick job behind several front entries can wait
  longer, so the refusal also offers the background call.

Left open for the owner (the design review's other suggestions, each of which changes
the design above or the trust the queue works on): a full HEAVY_QUIET_SECONDS window
beside a holder; one outside run at a time, under a marker `heavy status` could show;
quick jobs that yield to an ordinary head they've passed for a minute; a limit on the
wait that gives up the place; the HEAVY_QUICK_* knobs honoured only with a private
HEAVY_DIR; landings ahead of quick jobs that joined the front before them.

### Rejected: a second job on the holder's cores at nice 10 (71650a7, eadadb6, 2026-11-08)

heavy let one more job start beside a holder that left cores idle, on the same cores
at nice 10. Under contention a nice-10 process gets about a tenth of a core, so when
the holder burst onto every core, as cargo's compile and link steps do, the sharer
stalled. A commit check that shared beside a busy `cargo nextest run` took 7m47s
instead of about 1.5 minutes, and two of its tests failed on their time limits;
filedrawer's bin/tests took 826 s sharing against 70 s alone, and a wizard test timed
out. Only `heavy --share` work shares now, meant for work with no time limits, such
as a build.

### Rejected: two lanes on separate vCPUs at equal priority (2026-11-08)

Measured in the first entry: the same throughput as one lane, with each job 1.5 to
2.6 times slower.

## wt-ship and the commit checks

The owner's rule: every commit that reaches remote main passes the full check.
In sessions from 2026-11-04 to 11-08 on filedrawer, agents spent about 35 to 40 of
104 hours blocked on checks. heavy's queue usually held one to five jobs ahead, nearly
all `agent-pre-commit`, and a landing ran the full check at each commit, again in
`build-state finish` on each commit a fold rewrote, and again in wt-ship after its
rebase. wt-ship's recheck failed none of 34 logged landings, and most refusals at a
commit came from tests of the code it changed.

### wt-ship checks every commit it lands (9927dd7, 2026-11-08)

Each commit with no pass on agent-commit-gate's record, oldest first, each checked
out in turn in the worktree, then HEAD. Before, it checked only HEAD, so after a
rebase the commits before it reached main checked only on the base they were made on.

### wt-ship --local checks only while it holds the main tree (2026-11-08)

It rebases before it waits, so a conflict shows without holding anyone up, but checks
nothing until it holds the claim, then checks each commit once. Checking first threw
the round away whenever another session landed during the wait, which with several
sessions landing was often. Other landings wait longer for the claim, but they'd wait
the same for heavy's one lane, and a wait for the claim costs no CPU. The wait limit,
WT_SHIP_WAIT, starts again when the holder changes, so a queue that moves never times
out (the owner's choice over a longer fixed limit).

### A landing's checks run in one heavy job, at the queue's front (2026-11-08)

Under the claim, each check used to wait its own turn in heavy's queue, behind every
session's commit checks (20 to 43 minutes that day), while every other landing waited
for the claim; and a job from the queue could take the lane between two of a landing's
checks. Now each commit's check runs first outside the queue (`--no-queue`), so a
docs-only landing never waits for the lane, and the checks that need it run together
in one heavy job that joins the queue ahead of every ordinary waiter (the owner's
design). The running job isn't stopped. The front goes only to a landing under the
claim and, since 2026-11-09, to a `heavy --quick` job (above), and works on trust, as
the queue does.

### build-state finish only folds (1465ae5, 2026-11-08)

wt-ship checks a fold's new trees when it lands them, so finish's own run of the
full check on each rewritten commit was a second one.

### A repo may run a lighter check at each commit (2d50a0e, 2026-11-08)

A repo's `agent-commit-check` runs at each commit in the full check's place, with
passes on a record of their own, so wt-ship runs the full check on each such commit
when it lands. The hook's line marks a commit's run with AGENT_CHECK_AT_COMMIT=1
rather than the gate finding `git commit` among its parents, which a check that runs
the gate's own tests under an agent's commit would fool.

filedrawer opted in with clippy (its pre-commit hook has no Rust lint); llm-toolbox
and sandbox-setup, whose pre-commit hooks run ruff, with a commit check that runs no
test (2026-11-08).

### Rejected: `wt-ship --check CMD` (removed in 9927dd7)

Nothing used it. It came with wt-ship's first version, for a repo that had no
`agent-pre-commit` then and has one now.

## session-digest, session-scan and /upskill

### /upskill reads every agent's whole timeline, through session-scan (2026-11-09)

The owner's rule: a retrospective that misses an opportunity costs more than the
tokens it would take to find it. Before, /upskill read `session-digest --errors`
(the summary, the user's turns and what failed) and the full digest only to drill
into one agent, since the full digest of session a8a346b2, a main session and 22
subagents in filedrawer, ran to 540k characters. That read found the seven baseline
findings below; waste in calls that succeeded left no error to find.

Now step 1 reads `--errors` itself and runs `session-scan`, which cuts the full
digest into chunks of about 60k characters (12 for a8a346b2), has a model read
each at once through `claude-fresh` for /upskill's six categories, and prints
candidates with the digest lines they cite. The retrospective's agent verifies
each one. Measured on a8a346b2, each run's candidates scored by an agent that
checked every one against the digest and the raw log:

| run | candidates | real | false | distinct real findings | baseline found |
|---|---|---|---|---|---|
| haiku | 114 | 36 | 14 (12%) | 10 | 2, 4, 5, 6 (1 weakly) |
| sonnet | 62 | 25 | 3 (5%) | 12 | 2, 3, 4, 5, 6 |
| sonnet, with the user's turns and the longer list of non-findings | 82 | 28 | 3 (4%) | 12 | 1, 2, 3, 4, 5 |

Baseline (the errors-first retrospective): 1 the main session's length; 2
filedrawer's hooks ran the main tree's scripts; 3 stale text after behaviour
changes; 4 leaked test processes; 5 benchmarks holding heavy's queue; 6
session-digest's size; 7 a timing-test margin set too tight.

Every run found what errors-first missed: 214 foreground `waitone` polls, 165 of
them in two builder subagents, each resending 150 to 300k tokens of context; subagent
cache rebuilds; reviewers' mutation runs queued behind other sessions' commit
checks; full checks run before a rebase that then conflicted; `find /` and a
`grep -r` over the mount; a docs-only commit that ran the full suite because
`docs/` was missing from the hook's map. Most candidates that aren't real are
true but normal work (a subagent reading its brief, a hook refusal followed by a
correct retry), cheap to dismiss. The false ones mostly misread what the digest
leaves out: a Read's offset (now shown), an output spilled to a file, a command
cut mid-pipeline.

The default model is sonnet: fewer candidates for the agent to verify (62 and 82
against haiku's 114), a third of its false rate, and more real findings. Every
run reads about 200k input tokens, whatever the model, so sonnet costs its higher
price per token; the owner's rule above accepts that. A run takes a few minutes.
Two sonnet runs each found a finding the other missed, so one run is not
exhaustive.

### Rejected: Agent-tool subagents or a Workflow for the fan-out (2026-11-09)

A script gives the same chunking, prompt and output shape every run, and a cost
that can be measured; subagents' reports arrive as prose in the main context, and
a Workflow needs the owner's opt-in each time. The readers get no tools: a
chunk's text is all they need, and claude-fresh runs them locked down, which suits
logs that hold tool output and pasted text.

### session-digest times slow calls and marks cache rebuilds (2026-11-09)

A call of a minute or more shows its time; from 40 minutes it is marked LONG (the
owner: "it's common and okay for a tool call to last a long time. Calls over 40
minutes are definitely worth highlighting though, since that starts to risk
losing the prompt cache"). Calls sent in one batch run one after another, so each
is timed from the result before it: timed from its own start, the third of three
3-minute waits read 9 minutes, and such stacks filled the whole longest-calls
list. A call that started in the background isn't timed, since its result can be
logged when the agent next wakes (one in a8a346b2 read 1h48m). AskUserQuestion
and ExitPlanMode are tagged user, never LONG.

A tool call's time doesn't show the cache loss itself, which happens between
requests. The digest marks every request after an agent's first that wrote 100k
tokens or more to the cache and read fewer: on a8a346b2, 21 rebuilds and about
6M tokens, four in the main session (an hour's cache) after 68 to 167 idle
minutes, and 17 in subagents (a five-minute cache), most after a few `waitone`
calls of about three minutes in a row. A request that wrote that much but read
more, such as after a big tool output, kept its cache and isn't one.

### Rejected: a rebuild only after 40 idle minutes (2026-11-09)

The first rule. It caught all four of the main session's rebuilds, but only one
of the 17 in subagents, whose cache lasts five minutes; the 16 it missed held
about 3.2M tokens.
