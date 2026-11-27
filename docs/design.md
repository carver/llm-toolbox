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
`/home/carver/code/claudedev/keep/heavy-lane-exp/` on the host disk, outside git.

### Every job goes on a record; its command is cut, not redacted (2026-11-26)

On 2026-11-24 a filedrawer landing's tests ran ten times slower than usual, beside a 12-minute pytest that the sandbox's processes.log showed by its PID alone; by then it had exited, and nothing said what it ran, for whom, or how it got in (#9, #8).
history.json, keyed by a hash, couldn't say either.
So every job a heavy runs in a slot or outside the queue writes a JSON line to `HEAVY_DIR/jobs.jsonl` as it starts and one as it ends, and `heavy history --overlapping PID|TIME|START..END` answers "what ran beside my landing?" in one command.

- The start line is written before CMD runs, so a job still running, or one whose heavy was killed, shows (as `running` or `no end recorded`).
- Overlaps are found when the record is read, from each job's start and end, rather than stored with a job: one still running when another ends is found the same way as one that ended first.
- Each job's end line holds the CPU-seconds other work used on its cores while it ran, as `--alone`'s report measures it, so load from work outside the queue shows too, not only heavy's jobs.
- A heavy inside a job adds no record: its work is the job's, and a commit check inside wt-ship's job would list each landing twice.
- Lines are appended in one write without the queue's lock, and the file is cut to its newest 2000 lines past 2 MiB, by a rename. A line appended during that rename can be lost; taking the lock at each line would add a wait to every job's start and end for a record that only explains.
- Nothing in it fails a job: any error is a one-line note, and heavy's stop signals wait while a line is written, since heavy stops CMD by raising from its signal handler.
- It lives with the queue in /tmp, so a reboot clears it. A crash's post-mortem then has only the sandbox's processes.log.
- A wt-ship check that fails in its heavy job prints, before the failure, one line saying how many jobs overlapped it, how many at full priority, and the CPU other work used on its cores, then the three that used the most CPU, so the agent reads load before it reads a flaky test (#8). It stays short because `bgrun --wait` shows only a log's last lines; `heavy history --overlapping PID` lists the rest. Its job is matched by PID and a start no earlier than the check's, so a lost start line can't name a days-old job of a recycled PID. A record it can't read is a note, never a skipped cleanup. A check that fails outside the queue, or passes, prints none.

A command line can hold a secret (`--token X`, `TOKEN=X`, `-pX`, a URL's credentials), and the record outlives the job, so it keeps the command's first words only, up to the first that may be a value (a word holding `=` or `@`, one after an option, or an option other than a bare `-X` or `--word`, which may hold its value glued on) and at most 120 characters, with `cut` set when it dropped any.
It doesn't keep every secret out: a bare positional token, as in `./deploy prod TOKEN`, looks like a subcommand and is kept.
The other option the issue named, redacting what looks like a secret and keeping the rest, fails open: every shape it doesn't know (a header after `-H`, a positional token, a new tool's flag) is kept in full. Cutting fails closed: what it drops can't leak, and the first words still name the tool, its subcommand and its folder, enough to say which job overlapped.
The cost is detail: `sh -c 'pytest -n 8'` shows as `sh -c`, and `pytest -k name` as `pytest -k`. The record's `key`, history.json's hash of the repo and the whole command, still tells two runs of one command from runs of another.

### `heavy waits --until-quiet` waits for empty lanes and a low load average (2026-11-26)

The night's end-of-run timings waited for `heavy status` to show both lanes empty, as the protocol said, and started at a 1-minute load average of 4.4 to 6.5: the agents' own load (Claude Code sessions, their hooks, git and grep outside heavy, about 3 cores at 11:55 by #25's sample) shows in no lane.
`--until-quiet` waits for both: no job running in either lane (holders, sharers, and --few-seconds jobs running outside the queue, which take no slot and are found in the record by a live PID), none waiting, and the load average under `--load`, default 2.
The load average, not heavy's busy share: it counts runnable tasks over a minute, so it settles past a burst of hooks, and it is the number the night's runs were judged by. The default of 2 is the owner's ("about 2"), unmeasured; `--load` changes it.
It is the queue's state once, not a promise: a job can start the moment it exits, so a timing still runs under `--alone`, whose closing line reports outside work.

### heavy-bench: a benchmark is many short --alone jobs, interleaved (2026-11-25)

In filedrawer #137's benchmark (2026-11-25), `heavy --alone -- hyperfine` over three variants of a 2-minute suite held heavy's queue for about 25 minutes, so every session's commit checks waited behind it; the owner asked for it split.
A job per run lets other jobs in between, and interleaving the variants round by round spreads a drift in the machine over all of them, where hyperfine's runs of one variant in a row would put it on the last.
The cost is heavy's quiet-cores wait before each run, seconds on a quiet machine.
heavy-bench reads each run's wall and outside CPU from heavy's `HEAVY_ALONE_REPORT`, so time in the queue doesn't count, and reruns a run taken under outside load once, keeping both rows.

### heavy-bench waits for a quiet machine before a rerun, and stops after two loaded runs (2026-11-26)

On 2026-11-26 a `heavy-bench --warmup --rounds 3` over two variants of filedrawer's suite (about 9.6 min a run) had about 550 CPU-s of outside work in each run, and reran each flagged run at once, under the same load, holding the normal lane for over half an hour while `--few-seconds` jobs waited (#22).
So before a rerun it runs `heavy waits --until-quiet --max-wait S` (`--quiet-wait`, default 900): no job running or waiting in either lane, so jobs that queued meanwhile go first, and the load average under 2. Still busy at S, it stops before the rerun; a rerun flagged too also stops it, since two loaded runs in a row say the load will last. Either stop prints the table so far and exits 3.
The signal is the queue and the load average together, the ones `--until-quiet` already reads for the night's end-of-run timings; the loaded run's own outside CPU says only that it was loaded, not when the load ends.
A stopped bench doesn't resume from its `--out`: it starts again with a new one. Resuming would mix runs from two spells of the machine in one table, and the stop is meant to be rare now that timings run last, alone; the bench that prompted this probably ran during that day's 10:11-12:50 host throttle.
The 900 s is unmeasured, a guess at long enough for a commit's checks and short enough not to strand a bench for hours. A wait that fails for another reason is said and the rerun goes ahead, as before. `--no-rerun` waits for nothing, so a bench run on purpose beside another job, which keeps the machine from being quiet, takes it.

### An --alone job reports the other work its cores did (2026-11-25)

`--alone` keeps other heavy jobs off a timing's cores, but not work outside the queue: agent-commit-gate's `checks-needed` runs before it asks for a place, and an agent can run a build bare.
In filedrawer #137's benchmark (2026-11-25), one of nine `bin/tests` runs took 179 s where the same setup's others took 102 and 114, and nothing recorded what else ran then; the owner had to point out an agent working outside heavy.
So each `--alone` job ends with a line giving the CPU-seconds other work used on its cores, from /proc/stat less the job's own CPU time, and a warning past `HEAVY_ALONE_OUTSIDE` cores on average (0.5).
A process CMD started and left running counts as other work, since getrusage sees only reaped children.
sccache's server is the exception made: it runs a cargo job's compiles without being its child, so its CPU time during the job counts as the job's, and the line says how much; the server is the VM's, so another session's compiles then hide inside that figure, the lesser error for a timing of a build.
`HEAVY_ALONE_REPORT` writes the numbers to a file, for a tool that times many runs.

### heavy-gate sends `mutate-check --diff` to the long lane (2026-11-26)

On 2026-11-26 a `heavy -- mutate-check --diff` held the normal lane for 18 minutes,
with 451 s of CPU in that time, while another session's wt-ship checks waited behind
it (`heavy history`: 18m01s, normal lane, stopped by signal 15). A `--diff` run
makes its own mutants and runs its command once for each, so it takes minutes, and
the normal lane is where every commit's and landing's checks queue. heavy-gate now
refuses heavy without `--long` around `mutate-check --diff`, background call or not,
and names the whole form, `bgrun --wait -o LOG -- heavy --long -- mutate-check --diff
...` as a background call (a subagent's refusal names plain bgrun and then waitone),
so an agent that follows it isn't refused again for a foreground wait.

The first version refused every mutate-check under heavy without `--long`. The owner
narrowed it the same day to `--diff`: a reviewer's targeted run, edits from
`--edits FILE` or stdin, runs its command once, and five
`heavy --few-seconds -- mutate-check --edits` runs that day took 1.3 to 2.0 s each.
Such a run goes under heavy like any other command, with the foreground and
`--few-seconds` timeout rules.

- `--diff` is read among mutate-check's own options, the words before its `--`, as an
  exact word: its parser takes no abbreviation and no `--diff=`. An expansion there
  could be `--diff`, so it counts as one.
- The script of a shell's `-c` under heavy (`heavy -- bash -c '...'`) is searched too.
- `heavy -- mutate-check -h` passes this rule, since it returns at once.

### heavy-gate refuses an unbounded timing outside `--alone` (#20, 2026-11-26)

On 2026-11-26 an overnight agent compared two variants of filedrawer's suite, three
runs a side, with `bench.sh`, a script it wrote with a heredoc and ran later, whose
loop ran `heavy $flag -- /usr/bin/time -o FILE bin/pytest bin/tests`: under plain
`heavy` the numbers carry whatever shares the lane, and each run held the queue.
heavy-bench exists for that. heavy-gate now refuses, background call or not, a timing
with no bound: `time` (keyword or /usr/bin/time), hyperfine or `perf stat` under heavy
without `--alone` (in a shell's `-c` script and `$(...)` too), and hyperfine outside
heavy. The refusal names `bgrun -o LOG -- heavy-bench ...` bare, and `heavy --alone`
with a bound for a single short timing. A bounded timing outside `--alone` passes,
with a note beside the call (the hook's `additionalContext`) that timings go under
`heavy --alone` and comparisons of more than a minute or two to heavy-bench.

- Bounds: a `timeout` around the timing or inside it (`time timeout 300 make`),
  hyperfine's `--runs`/`-r` or `--max-runs`/`-M` (not `--min-runs`), and heavy's
  `--few-seconds`, which heavy stops at 60 s. Any `timeout` counts, however long: the
  gate errs toward letting a call through, since it runs in every session.
- `time` outside heavy passes without a note. session-grep found about a hundred bare
  `time` calls from 2026-11-09 to 11-26 (`time gh api ...`, `time rg ...`, a tool's
  `-h`), one-off measurements with no queue behind them; refusing them would be wrong
  refusals. hyperfine outside heavy is a benchmark by nature, so it is judged.
- A heavy whose options hold an expansion (`heavy $flag -- ...`) isn't judged: the
  expansion could be `--alone`. The incident's own call was of that shape, inside a
  script written to a file and then run, which the gate never sees; only a check in
  heavy, on the argv it runs, would catch it (left as a follow-up on #20, with
  heavyhistory's estimate of runs times variants times a run's usual length).
- Replayed over the 69,681 Bash calls in the session logs at landing, it refused 3,
  each right: an unbounded `/usr/bin/time cargo nextest run` under plain heavy, and
  two hyperfine runs outside heavy with no `--runs`. It noted 24, about half of them
  hyperfine or `perf stat` under `heavy --few-seconds` (or `--quick`), most of the rest
  hyperfine with `--runs` outside heavy.

### A --few-seconds job is stopped at 60 s (2026-11-26)

On 2026-11-26 agents ran jobs of 61 to 215 s under `--few-seconds` (heavy's record: whole test files such as `./t test_build_state.py`, 202 and 215 s; `./t test_gh_ci_wait.py`, 131 s), each jumping the queue's front and running beside the lane's holder, and heavy only warned as each ended (`HEAVY_QUICK_MAX`).
The owner: "It should require a time limit of 60 s or less. That should only cover overloaded situations; they should usually be closer to 10 s or less."

- A --few-seconds job still running `HEAVY_QUICK_MAX` seconds after CMD started (60 by default) is stopped as a stopped heavy stops one: SIGTERM to CMD's process group, SIGKILL after `HEAVY_GRACE`. heavy exits 124, timeout(1)'s status, and the record keeps 124. The line names `bgrun -o LOG -- heavy -- CMD`. The warning it replaces is gone: no job runs past the limit to hear it.
- Only running time counts, not the wait at the queue's front, which a --few-seconds job doesn't choose.
- `HEAVY_QUICK_MAX` may lower the limit, never raise it: a setting over 60, or one that isn't a number of seconds over 0, is a usage error (exit 2) for heavy. The tests lower it to fractions of a second.
- A heavy inside a job has no limit of its own: the outer job's holds, and a check that calls heavy inside a landing's job must not be cut at 60 s.
- Not from heavy's history: the owner doubts its fidelity, and a limit stops the run that overran whatever predicted it.
- agent-commit-gate's seconds checks (`CHECK_SECONDS_EXIT`, filedrawer #155, since f54c3ac and filedrawer's 84af74b) run as --few-seconds jobs through heavyqueue, not bin/heavy, so the gate applies the same limit itself: the check runs in a process group of its own (with SIGTERM should the gate die, since a signal to the gate's group no longer reaches it; a gate stopped by SIGINT, SIGTERM or SIGHUP stops the check's group first, as heavy does, since PR_SET_PDEATHSIG reaches only the check's first process and a probe's `sleep` ran on outside the queue), and past the limit it is stopped the same way and runs again as an ordinary queued check, after a line saying the check's seconds rule needs a look. So a commit never fails for overrunning; it costs the time it ran plus the ordinary run. A bad `HEAVY_QUICK_MAX` there is a note and 60 s, not a refused commit. A check inside a job, as in wt-ship's landing, has no limit, as a heavy inside one has none. Both runners stop a group with heavyqueue's stop_group, and a stopped job's record says `"stopped": "limit"`, since its 124 alone is also what a command's own timeout(1) exits. filedrawer's record held no seconds run yet at landing; fd#148 measured the checks its rule calls short at under 40 s, so at the host's usual speed they fit, and under a throttle like that day's (about 0.15x) they would overrun and run again in the queue.

### heavy's `--quick` is named `--few-seconds` (2026-11-24)

Agents ran `heavy --quick` for runs of minutes: in one session, a whole test file of
306 s and two mutate-checks of about 2 minutes each, each time hearing at the end that
`--quick` is for seconds. The warning comes after the cost; the name is read as the
command is typed. The user proposed `--few-seconds` as the only documented name, and
the agent agreed: the limit is in the word. `--quick` stays as an alias that `-h`
doesn't list, so frozen repos, older docs and habit keep working, and heavy-gate takes
both. The `HEAVY_QUICK_*` settings keep their names.

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
  warning only. Since 2026-11-26 it is stopped at 60 s (the entry above).
- Since 2026-11-24, a run outside the queue is niced to 10, as a sharer is, even on a
  free lane, since a holder may take the lane while it runs and only root can undo a
  nice. That day a ten-worker pytest, most likely a `--quick` run admitted while a
  filedrawer landing sat between phases, ran beside the landing's nextest at full
  priority for 12 minutes, and a test with nextest's 180 s limit ran past it (#8).
  The owner picked this over running landings `--alone`, which would hold every
  session's `--quick` runs behind each landing.
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

### A queue entry whose first line isn't whole is cleared (2026-11-23)

After a VM crash on 2026-11-23, `/tmp/heavy/normal/` held a waiter's entry of 0 bytes
from the boot before. `heavy status` showed it as `waiting for ?`, and every job, in
two sessions, waited behind it for 28 minutes with the cores idle, until the file was
removed by hand. `fifo.join` writes an entry whole, by a rename, so a reader never
finds one half written; but a crash can leave the renamed file with its text never
flushed. `fifo.ahead` cleared only an entry whose process had certainly ended, and a
holder with no PID "may be alive", so this one stayed for good.

An entry whose first line isn't whole, as `join` writes it (a host, a PID, a start
time and the newline that ends it), is now removed like an ended one, in both queues
built on `lib/fifo.py`; so is one that isn't text. Only `join` writes entries, and it
always writes that line, so a cut anywhere in it, in the middle of the PID included,
is a crash's. Slots keep the old rule outside heavy, since a flag such as wt-ship's
may be written by other means; heavy's own slots already counted a holder with no PID
as dead.

Not done: an fsync in `write_whole`. It would slow every join, many a second across
the VM, for a case the clearing now covers.

### Rejected: a second job on the holder's cores at nice 10 (53e60a8, 6a76738, 2026-11-08)

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

## perf-promises

### A promised running time is measured on every run through heavy (2026-11-26)

An /upskill pitch put `mutate-check --diff` back into every /build (030df63), saying each mutant "reruns tests that take seconds".
Measured the next day, its runs took a median 8.2 minutes (p90 27.5), held heavy's long lane for about 63% of the VM's uptime, and made other jobs wait up to 54 minutes (mutate-check's entry, /build's --diff run is dropped again).
The owner: "If performance is promised, it should be measured ongoing and warn if the performance gets significantly worse (2x or more?)".
The owner's decisions:

- A repo declares its promises in `perf-promises.toml` at its top. llm-toolbox's own file also covers jobs in every repo, since its tools and skills run everywhere; runs matching one are logged under llm-toolbox, which is found as the repo holding the running lib/perfpromises.py, not by a workspace path. In llm-toolbox itself the tree's own copy stands, so a worktree's new promise works there.
- Only running time counts, from a job's start to its end, never its wait in the queue. The 030df63 runs took 317 to 3425 s against a promise of seconds, so running time alone catches that case.
- A run of twice its promise or more gets the agent that ran it one line. A promise is broken when the median of its last 5 counted runs is twice its seconds or more, and a SessionStart hook then shows it to every new session, as `systemMessage` for the owner and `additionalContext` for the agent. One slow run gives a note, never an alarm.
- It measures only work that runs through heavy: builds, test suites, mutate-check and commit checks. Work outside heavy is out of scope for now, and so is a commit check's first pass outside the queue (agent-commit-gate's `run_outside_first`), which makes no heavy job.

The builder's and the brief review's choices, which the owner hasn't overruled:

- The hook is where heavyqueue writes a job's end line (`recorded`), the one path that `heavy -- CMD`, the gate's checks and wt-ship's job all share. A job is matched on the command words heavy records, which for a gate check are its name (`agent-pre-commit`, `wt-ship's checks`), with the first word cut to its last path part. A heavy inside a job records nothing, and adds nothing here.
- The time is the job's Tally's monotonic seconds, so a suspended machine doesn't count.
- A run counts when its exit status is in the promise's `counts` (`[0]` by default; `[0, 1]` for mutate-check, whose 1 is a surviving mutant) or a signal stopped it, at least its elapsed time. A run that shared a lane (`--share`), or beside other work that used over half its cores (Tally's `other` over its seconds times its cores), is logged but left out, and its note says so: below half, load alone can't double a run's time, so a 2x run is the job's own.
- Only runs of the last 14 days count, and only log lines that carry the promise's current seconds and a hash of its command, so a worktree's edited promise doesn't skew main's. Below 5 counted runs a promise isn't judged.
- The log is per repo in its git common dir (`perf-promises/NAME.tsv`), shared by its worktrees, as check timings chose over keep/ and /tmp (Each check run reports and records its parts' timings). Lines are appended in one write; past 400 a lock is taken to cut it to 200, and a line appended during the cut can be lost, as in heavy's record.
- Dodges: a promise over 3 times its `measured_seconds`, or one that matched jobs once but none in 7 days (its pattern likely broke), is flagged by `status` and at session start. One that never matched isn't flagged, only shown as `never matched` in `status`, since a tripwire such as the `mutate-check --diff` seed is meant never to match. /build's review prompt asks for an entry here when a diff removes a promise or raises its seconds. A promise names a whole run's time, never a per-item one: the pitch's "seconds" was per mutant.
- The gate hides a passing check's output, and subagents read only its pass line, so a check's note goes in that line.
- Nothing in it fails a job: a file or entry it can't read, or a failure of its own, is the job's one note.
  A pathological `command` regex could still hang heavy while it holds a slot, since Python's `re` has no timeout; that is accepted, since promise files are written by the owner or through review.

llm-toolbox's seed is a tripwire for `mutate-check --diff`, at 120 s, the whole run a /build use would need (40 mutants at about 3 s each), against the median of 861 s that heavy's record measured that day. Its own landing check, which the README says takes about half a minute and heavy's record shows at a median of 262 s over 7 runs that day, has no promise: llm-toolbox's file covers every repo's jobs, and that job's words, `wt-ship's checks`, are the same in each.
Both numbers, the 262 s and the seed's 861 s, were taken on 2026-11-26 while the host throttled the CPU (the next entry), and neither has been measured again since.

### A run counts at the machine's usual speed (2026-11-26)

The afternoon perf-promises landed, the host throttled the VM's CPU, and the owner said: "The CPU was throttled really low, so I wouldn't trust that 262 s number right now."
The sandbox can't see that throttling: cgroup `cpu.stat` showed `nr_throttled 0`, `/proc/cpuinfo` a fixed 2496 MHz, and there is no cpufreq.
So a throttled run looked like an honestly slow one, and the load filter, which reads only the guest's own cores, didn't catch it.
The owner: "yes build it, and note the throttling in measured".
The owner's decisions:

- heavy times a fixed CPU loop after every job, so the usual speed builds from everyday jobs, and at the start and end of an `--alone` one. A machine's speed factor is the loop's usual time over its time now.
- Adjust, don't drop: a run counts at its seconds times min(factor, 1), and is left out only when that is under twice its promise while its seconds are twice it or more, since the slowness alone explains the overrun. The median in `status` and the alarm are over those times. This VM is power-bound (heavy's entry, three times the CPU-seconds for the same work while other vCPUs are busy), so a rule that dropped every run below 0.5x would leave out the long runs the tracking exists for.
- A run's note gives both times: `heavy: NAME ran 3425 s (1370 s at the machine's 0.40x speed, measured 2 min ago), 11.4x its promised 120 s`. An unknown factor counts a run at its seconds.
- `heavy --alone`'s closing line gives the factor at the start and the end, and says the timing isn't one to trust when both are under 0.8.
- The seed's `measured` says its 861 s was taken while the host throttled the CPU; `measured_seconds` stays 861.
- Load buckets, decided after the first build: at 13:18 that day, with a load average of 1.5, the probe took about 16 ms against about 8 ms on a quiet machine. On this power-bound VM ordinary load halves the probe's speed (the 2026-11-08 entry above: 16 vCPUs, an i7-1260P's 4 P-cores with 8 threads and 8 E-cores at about half speed on one 28 to 64 W budget, three times the CPU-seconds for the same work while the other half of the vCPUs was busy, 16 vCPUs no faster than 8), so one usual time would read load as throttling and discount runs on every busy machine. The owner: "add the load buckets to this change. I'm not sure 'number of cores used' is the right measure. Also note that there are virtual cores, so the real 'max' should be closer to 6 cores, otherwise performance slows".
  The owner chose capped busy cores: the load is the machine's busy vCPUs over the second before the probe, from /proc/stat, in buckets of under 1, 1 to 3, 3 to 6, and 6 or more, since past about 6 the power budget is spent. The probe runs pinned to one vCPU of the job's cores, which its line records, and the usual time is per machine, vCPU and bucket, so P-cores and E-cores each get their own; the guest can't see a busy hyperthread sibling, so probes within a bucket stay somewhat noisy. With under 20 probes in the cell, the factor is unknown and the run counts at its seconds. `heavy speed` prints each cell's usual time and probe count.
- Probe after heavy exits, decided after the load buckets: the probe as the buckets first built it added about 1.07 s to every job's end while the job held its slot, 1 s of sampling /proc/stat for the load (heavy takes no samples while a job runs) and about 66 ms of probing, which at 300 to 700 jobs a day is 5 to 12 minutes of lane time. The owner chose to judge a job by the newest probe on this machine from the last 5 minutes whose factor is known, any vCPU's, at the factor its line logs, since throttling lasts hours and minutes of lag don't matter; with none, the factor is unknown and the run counts at its seconds. Each job's own probe runs in a child heavy starts once the slot is free and doesn't wait for: a session of its own that ignores SIGHUP, holds no slot, writes only the speed file and ends within seconds, one at a time by a lock beside the speed file, so a burst of short jobs starts no crowd. An `--alone` job keeps its start and end probes inline, since its timing already waits for quiet cores and its closing line needs them. The after-exit child's second of load comes just as the next job starts, before it ramps up, and the loop then runs under that job's full load, so a probe takes the busier of that second and the loop's own run (its own CPU left out) for its bucket: else it would land in too low a bucket and read as a slow machine.
  Measured once built (2026-11-26, 15 runs of each, interleaved, inside one `heavy --alone`, whose line warned of 0.97 cores of other work), from a `heavy -- sh -c 'date ...'` command's end to heavy's exit: a median of 30 ms with the child against 30 ms before any probe (f238067) and 1096 ms with the probe inline.

The builder's and the brief review's choices:

- The factor is from process time, not wall time. This guest's kernel has CONFIG_PARAVIRT_TIME_ACCOUNTING unset, so stolen time is charged to the running task (kernel/sched/core.c, update_rq_clock_task), and both kinds of host throttling, a lower clock and a descheduled vCPU, slow `time.process_time()`; wall time also counts in-guest preemption, which is load, not speed. The wall time is logged only.
- The probe is a function using locals (module-level code ran 32 to 49 ms, a function about 8 ms), run once to warm up and then 3 times, the median counting: about 30 ms a job at the usual speed.
- The usual time is the fastest tenth (10th percentile, nearest rank) of the last 3 days' probes, from at least 20, of one machine (hostname, cores and MemTotal to the nearest GiB, in the file's name, so a sandbox recreated on other hardware starts again), one vCPU and one load bucket. At 300 to 700 probes a day, the file's trim past 4000 lines to 2000 keeps the window; with 16 vCPUs and 4 buckets, a cell fills only where the probes land often.
- The probe takes the least busy vCPU of the job's cores over that second, the lowest of a tie, so it competes with no task on its vCPU and the probes gather on fewer cells. The load leaves out the host's steal, which is the throttling the factor is there to show, and heavy's own CPU time over the second. For an `--alone` job the second is outside its seconds but holds its slot; its start reuses the last sample of its wait, which covers the second before it, so only the end pays.
- The speed file's lines carry each probe's factor, as a ninth column, so a later job reads it rather than judge the probe again. The child holds its lock from heavy: heavy takes it and passes the descriptor on, so a job ending while a child runs starts none, and the tests wait for a child by waiting for the lock. The tests turn the child off (`HEAVY_SPEED_AFTER_EXIT=0`, read only while `PERF_PROMISES_DIR` is set) except where they wait for it.
- The probes live in llm-toolbox's git common dir, `perf-promises/speed-<machine>.tsv`, beside the logs and shared with the owner's host through the mount; a probe or file that fails is at most the job's one note.
- Steal on the job's cores is split out of /proc/stat's counters and named in the `--alone` line, as a fact beside the outside load rather than a part of it: a task's own stolen time is charged to it as well, so steal isn't all in `other`. No second filter on steal: the factor covers it.
- Dodges: `HEAVY_SPEED_PROBE`, the tests' stand-in for the loop's time, is read only while `PERF_PROMISES_DIR` is set, and its lines are marked `fake`, which count toward the usual time only while it is read. `status` and the session-start hook flag a promise most of whose last 10 runs, 6 or more (or most of the runs, when it has fewer), were left out for speed or load.
- heavy-bench keeps the factors at each run's start and end in two columns of its results, and neither flags nor reruns a slow run: a throttled spell can outlast a bench, and would flag every run.

A CFS quota makes short probes bimodal, which the steal over a whole job catches better; that is unverified for host kernels that throttle per task.

## machine-speed

### The owner hears when the machine runs extra slow (2026-11-26)

The owner, after the afternoon the host throttled the VM: "Also, of course, I would want to be notified when the machine is running extra slow, even if it's not a broken promise of the code. I want to be able to address it on the host machine."
heavy's record put that throttling at roughly 0.1 to 0.2x from about 10:20 to 12:40 PST: wt-ship's checks used 5,200 to 6,350 CPU-seconds against 850 to 1,800 before and after, and `./t test_heavy.py -k` 35 to 48 against 1 to 9.
It was a lower clock or a power cap, not steal: steal since boot came to about 3,900 CPU-seconds, about one core-hour, where descheduling to 0.1 to 0.2x on 16 vCPUs for 2 hours would be about 25. So a timed probe is the only signal.
The owner's decisions:

- Two channels: an email, which reaches the owner away from any session, and a Claude Code status line, live at a session. No session-start message (since reversed for one line: the next entry).
- A cron job probes every 5 minutes, beside heavy's job-end probes, through the Purelymail account the email skill uses.
- The thresholds, split after measuring that day: an email when three probes in a row, within about 20 minutes, read under 0.25x; a recovery email when three read 0.5x or above, only after a slow email went out. The status line says `slow` under 0.5x.
- Load buckets, not quiet probes: "How would it even measure quiet machine probes? There are often long stretches where the machine is loaded without a break". Every cron probe counts, busy or not, at its load-adjusted factor (heavy's speed probe: against probes on the same vCPU under the same load), pinned to vCPU 0 so its cells fill fast. A cell with under 20 probes gives no judgment, so a machine loaded in a way it never was before is silent for about 100 minutes.
- Caps: a slow email at most once every 3 hours, at most 4 emails a day per machine, the hostname in the subject.

The builder's and the brief review's choices:

- `machine-speed probe` writes heavy's speed file, so cron's probes give heavy a recent speed for a long job on an idle machine too, which 04f5573 left counting at its raw time. The alert judges cron's own probes only, from its state beside the speed file, so the streak's spacing is cron's.
- A new slow spell within 3 hours of the last slow email waits the cap out while it lasts, then mails; a spell that ends first mails nothing, nor its recovery. So the worst case is a slow and a recovery email every 3 hours, which the daily cap holds to 4.
- The email goes through the email skill's `send_email.py`, called by path with its output captured, since it prints the address; a failure is logged as its error's class, tried again at most hourly, and shown on the status line as `(mail failed)`. The address is `git config --global llm-toolbox.newsEmail`, as skills-news's; the credentials come from the login shell's environment (`bash -lc`). llm-toolbox is public, so neither is in it.
- The status line shows the newest probe of the last 15 minutes whose speed is known, any probe's, rounded down to a tenth, so `0.4x` is never shown beside a factor of 0.5 or more. It always prints one line and exits 0, since Claude Code blanks the bar on a non-zero exit or no output, and refreshes every 60 s (`refreshInterval`), since its events stop while a session is idle.
- `install.py` adds the cron line and the status line only in the sandbox (SANDBOX_VM_ID or /etc/sandbox-secrets.sh), where the credentials and cron live, and sets the status line only when none is set.
- Limits, said in `-h`: throttling that lasts past the speed file's 3-day window becomes the usual speed; new hardware is a new machine, silent until its cells fill; a sandbox recreate loses the crontab and settings until install.py runs again.

### A slow spell is one line in `heavy status`, `heavy waits` and the session start (2026-11-26)

On 2026-11-26 the host held the VM's cores at 400 MHz (PROCHOT) from about 10:11 to 12:50, about 7 to 10 times slower, and agents read `heavy status` and `heavy waits` all that time without a word of it (#25's comment).
In the night's retrospective the owner asked for one loud line in both and in the session-start hook, with a threshold above 3x, since 3x under load is plausible on this power-bound host, or judged within the probe's load bucket.
That reverses the entry above's "no session-start message" for this one line: the owner's later word.

- The line reuses machine-speed's judgment rather than a second one: it shows while the alert state holds a slow spell open, three cron probes in a row under 0.25x (4x slower), each against probes on its vCPU under its own load, until three at 0.5x or above. So it is both above 3x and judged within the load bucket, and it says what the mail says.
- It shows only while cron's newest probe of known speed is at most 20 minutes old (`SPELL_FRESH`, the streak's span): a crontab lost to a sandbox recreate, or a probe that keeps failing, would otherwise leave a spell open for good. Where cron isn't installed there is no line, as there is no mail.
- It shows only while that probe reads under 1/3x (`SLOW_SHOWN`, the owner's 3x). A spell closes only after three probes at 0.5x or above, so through its recovery tail the line said `running 1x slower` or `2x slower`, which is no warning (second review, 2026-11-26).
- The factor is that probe's, shown as `Nx slower` (1 over it, rounded) and as the factor.
- `heavy status` prints it as its first line. `heavy waits` prints it on stderr, since its stdout is one line, or JSON, that scripts and the overnight gate read; `--until-open` the same, once. The session start puts it first among perf-promises' lines, for the owner and the agent.
- Reading it must never fail what prints it: a missing or broken state is no line.

## pipefail-lint

### tree-sitter-bash, repaired where it runs lines together (2026-11-09)

A finding names its line, and `lib/shellparse.py`, which the gates share, keeps no
positions: it joins continued lines and drops heredoc bodies. bashlex failed on 71 of
the workspace's 144 shell scripts (case patterns, arithmetic, heredocs). tree-sitter-bash
(0.23.3, the same in 0.25.1) parses all 144 without an error node, but misreads two
shapes without a word, which hid findings:

- After a pipeline of three stages or more, it runs the next line into the pipeline's
  last command when that line holds `&&` or `||`, or a heredoc follows. A newline
  between two words of a simple command, not after a backslash, can't be one bash
  reads that way, so the lint puts a `;` after the first word and parses again.
- It reads `a | b | c || d` as `a | (b | c || d)`. The lint takes a list it finds as
  a pipeline's stage for that list's first command, and reads the `||` as the whole
  pipeline's.

tree-sitter 0.25.2 has wheels for Python 3.14; 0.24.0 doesn't, and would need a C build
where uv picks the system Python.

### Only files that name pipefail reach it in the commit hook (2026-11-09)

The lint runs in `uv run --script`, which resolves its environment before the script
can skip a file. The hook passes only staged files whose text holds `pipefail`, so a
commit that stages none never starts it. This repo's README and docs name it, so a
commit to them does start it: 0.05 s with uv's cache warm, PyPI with it cold.

## wt-ship and the commit checks

### A commit that repeats its parent's message: warned at commit, refused at landing (2026-11-25)

A session ran `git commit -q -C HEAD@{0} --reuse-message=HEAD 2>/dev/null`, meant as a retry, and committed its change under the base commit's subject; nothing said so, and it took an amend and three calls to notice and fix.
Only a commit-msg hook could refuse such a commit as it's made, and that would need every repo's hooks reinstalled, and a commit in repos that track them.
So the post-commit line every repo already has warns at once, and wt-ship refuses the landing, after its autosquash rebase so two fixups of one target, which repeat a message by design, are gone by then.
`SAME_MESSAGE_OK=1` lets either through, for a repeat that's meant (the owner's choice of hatch, over a trailer in the message).

The owner's rule until 2026-11-24 was that every commit that reaches remote main
passes the full check. Since then, only each landing's tip does (below, issue #7).
In sessions from 2026-11-04 to 11-08 on filedrawer, agents spent about 35 to 40 of
104 hours blocked on checks. heavy's queue usually held one to five jobs ahead, nearly
all `agent-pre-commit`, and a landing ran the full check at each commit, again in
`build-state finish` on each commit a fold rewrote, and again in wt-ship after its
rebase. wt-ship's recheck failed none of 34 logged landings, and most refusals at a
commit came from tests of the code it changed.

### A landing of several commits lands as a merge, checked once at its tip (#7, 2026-11-24)

In filedrawer, a check of a commit that touches a crate takes about 260 s, so a
landing of 6 commits on 2026-11-23 spent 26 minutes checking, under the main tree's
claim, and the next session's landing waited behind it. Now a landing of one commit
lands as before. A landing of two or more, counted after the last rebase, lands as a
merge commit: first parent the branch's tip, second the rebased tip, tree the tip's,
made with `git commit-tree`, which runs no hook. Only the tip is checked, and only when
its tree has no pass on record, so the tip's pass, keyed by tree, is the merge's. The
owner's choices: the default in every repo, with no setting and no opt-out; and the
subject `Merge TOPIC (N commits)`, with the commits' subjects in the body, TOPIC from
the worktree folder `REPO-wt-TOPIC`, else its branch, else the tip's short ID.

What it gives up: the commits inside a merge carry only the checks they had when they
were made (in filedrawer clippy and the rule tests, in llm-toolbox the seconds-long
tests a path names), and on trees the rebase and the folds may since have changed,
which nothing checks. So `git bisect --first-parent` walks only checked commits, and a
plain `git bisect` can stop on an unchecked one. A fix folded into a commit before the
code it needs leaves that commit broken unseen, where it used to fail at landing.

- After landing, the worktree's HEAD, and its branch, moves to the merge, same tree,
  so the worktree's next landing finds its fork point in the branch's reflog. Left at
  the tip, which the reflog never holds, `--fork-point` finds nothing (a review of the
  plan showed it live).
- A pushed merge lost to another push's race is made again on the new tip, after the
  rebase and the check.
- The tip is checked in place, so nothing checks out an earlier commit any more, and
  the marker that guarded one is gone, with agent-commit-gate's refusal of commits
  while it stood (the owner's call). wt-ship still refuses on a marker an older
  wt-ship left, killed before it put the tip back, since that worktree may hold an
  earlier commit and would land a shortened range (the owner's call, after a review
  of the plan raised it). In its place, the tip is noted before the check, and a
  commit made in the worktree during the check stops the landing: what was checked
  would no longer be what lands. A second run from the same worktree waits for the
  claim, then finds nothing left to land.
- The landing notice lists the commits' subjects, not the merge's. The reminder to
  push counts the merge among the commits ahead.

Before: wt-ship checked every commit it landed, each with no pass on record, oldest
first, each checked out in turn (03bc4c8, 2026-11-08), since checking only HEAD left
the commits before it checked only on the base they were made on. Measured cost: see
above.

### Each check run reports and records its parts' timings (2026-11-24)

The pass line gave a check's total alone (`passed (258s, …)`), and wt-ship's logs
live in session scratchpads, so finding what made filedrawer's landings take four
minutes a commit took an hour of benchmarking: its bin/tests was 41% of the check,
and a fresh copy of the index rebuilt the crates every time. A check now writes its
parts' seconds to the file CHECK_TIMINGS names, the gate puts them on the pass line
or before the refusal, and every run goes on a record in the repo's common git dir.
The owner chose there, per repo and shared by its worktrees, over the workspace's
keep/ (a workspace path in a tool for any project) and heavy's folder (/tmp, wiped
by a reboot), and chose every run, commit checks included, over landings alone.
A landing's check runs inside a heavy job wt-ship waited for, where the gate starts
at once, so wt-ship passes that wait on as AGENT_CHECK_WAITED for the record
(2026-11-25): until then every landing showed 0 s waited (filedrawer #148).

### wt-ship --local checks only while it holds the main tree (2026-11-08)

It rebases before it waits, so a conflict shows without holding anyone up, but checks
nothing until it holds the claim, then checks once (each commit then, the tip alone
since #7). Checking first threw
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
the queue does. Since #7 a landing has one check, the tip's, so the job holds one.

### A commit check whose queued work takes seconds runs as a --few-seconds job (filedrawer #155, 2026-11-26)

In filedrawer's record (#148), commit checks of seconds waited minutes: since its
check slot (#146), the 29 queued clippy checks without the routes test waited 8,130 s
to run 770 s, up to 1,034 s for one 21 s check. They joined the back of the queue,
behind the holder and every landing and `--few-seconds` job that came after them.

The check itself now says so: its first run, outside the queue, gets
CHECK_SECONDS_EXIT beside CHECK_QUEUE_EXIT, and exits with it when what it would
queue takes seconds; the gate runs it again with `heavyqueue.turn(quick=True)`. The
check already decides there what it needs and why, and filedrawer's "seconds" rule
reads the check slot, which only its check knows. A check that never uses the new
exit, and a gate that doesn't set it, keep the old behaviour, so nothing breaks
either way.

Passed over:

- The issue's plan, a third answer from `commit-needs-queue` (exit 2: queues, but
  seconds). It changes an old answer's meaning (anything but 0 meant "won't queue";
  dash exits 2 on a syntax error, which would then hold a foreground commit), the
  gate would run the predictor a second time inside the hook, and the predictor would
  repeat the check's reading of the slot. The foreground refusal needs no third answer:
  a --few-seconds check on busy cores still waits for the running job, minutes, so the
  predictor says "queues" for it as before.
- Every commit check at the queue's front, beside landings. It needs no rule, and ends
  the jumps (3,455 s of #148's first record), but not the wait behind the holder, and
  long checks would jump ordinary jobs too.

Unmeasured at landing: what such a check costs a holder it runs beside at nice 10
(#148's second item). It runs as any `--few-seconds` job does, whose design (above)
took that cost as small for seconds of work.

### build-state finish only folds (8f13e99, 2026-11-08)

wt-ship checked a fold's new trees when it landed them, so finish's own run of the
full check on each rewritten commit was a second one. Since #7 wt-ship checks only a
landing's tip, so a fold's earlier trees get no full check at all, an accepted gap
(above).

### Commits that only refine an earlier one of the landing get folded (2026-11-09)

The owner's rule then: every commit gets the full suite before it lands, and none
after, so the give is in commit isolation. Since #7 only a landing's tip gets the
suite, so folding no longer saves checks. The owner kept the warnings, reworded: folding
still keeps each change in one commit, with no fixups landing after it, so that, for one,
a change can be reverted whole. The mutate-check landing of 2026-11-09 checked 8
commits, 96 to 160 seconds each, of which 5 only refined the `--diff` code an earlier
one added: review fixes, the owner's asks after a first run, two refactors. /build
now makes its refactor and those asks `fixup!`s, and `lib/foldable.py` finds the rest.

A commit is foldable when 75% or more of the non-blank lines its hunks replace, or,
for an insertion, sit either side of, were last changed (git blame, from the merge
base) by an earlier commit of the landing. The owner chose hunk blame, then the
threshold over "every line" on data: every line flagged 1 of the 5, since refactors
touch a few of main's lines and new helpers sit beside main's code. The shares were
0%, 9%, 17% for the three commits that stand alone and 88% to 100% for the five;
over 50 earlier commits taken as one batch, harsher than any landing, one scored
over 50%, a session-scan feature built on the commit before it. A refinement that
also fixes a line of main's folds with it, which the owner accepted: it lands
checked either way.

Each non-blank line a commit adds in a file it creates counts too, as a judged line
from outside the landing (2026-11-18). Hunk blame weighs only old lines, so
filedrawer's c93e791, which added 518 lines in three new files and reworded one
line of an earlier commit's, scored 100% and needed a `Fold-check: keep` by hand.
Over filedrawer's last 60 commits taken as one batch, the old rule flagged 14 and
this one 11; the three it drops each added a tool and its tests, 465 to 1271 lines.
llm-toolbox's last 60 flagged the same 15 either way. A rule that any new file
makes a commit stand alone was passed over: a review fix that adds a small fixture
still only refines.

A fold moves a commit back past those between it and its target. A conflict there
shows in the rebase, which `build-state fold` aborts; a use of a name a commit in
between defines doesn't, and would leave the folded commit broken, which only its own
full check would find, and since #7 none runs there. So a
commit whose added lines use a name a line added since its target defines (def,
class, const, `NAME =` and the like), which the target's tree lacks, is left in
place, and wt-ship lands it with a warning (the owner's call, after a side agent
raised it). Tried first: any word the parent's tree has and the target's lacks,
which flagged `Interrupted` in b37f43f, a word that only prose and a test string
named.

wt-ship and finish warn; neither refuses. The owner first chose a refusal, then
weighed it against real landings: of the last 149 landings to llm-toolbox's main, 44
had several commits, 248 in all, and 18 were foldable; of filedrawer's last 199, 49
had several, 291 in all, and 16 were foldable, 12 of them /build's "tidy after review"
refactors, which its fixup rule above already folds. About 6% of commits, a step
too small for a stop every other session meets. A `Fold-check: keep` trailer on the
commit silences the warning for it, rather than a flag for the whole landing (the
owner's choice): it lasts through rebases and says which split is on purpose.

### A repo may run a lighter check at each commit (fe1aad9, 2026-11-08)

A repo's `agent-commit-check` runs at each commit in the full check's place, with
passes on a record of their own, so wt-ship runs the full check when it lands such a
commit at the tip (since #7, the tip alone). The hook's line marks a commit's run with AGENT_CHECK_AT_COMMIT=1
rather than the gate finding `git commit` among its parents, which a check that runs
the gate's own tests under an agent's commit would fool.

filedrawer opted in with clippy (its pre-commit hook has no Rust lint); llm-toolbox
and sandbox-setup, whose pre-commit hooks run ruff, with a commit check that runs no
test (2026-11-08). llm-toolbox's has since run the seconds-long tests a staged path
names, outside the queue (2026-11-16): a tool's -h gained a variable its README entry
lacked, which only the landing's suite would have refused.

### Checks run without secret-named variables (2026-11-09)

agent-commit-gate leaves out of every check's environment each variable with a
`_`-separated part of its name, in any case, that is KEY, TOKEN, SECRET, PASSWORD or
PASS: the commit check, the full check and the quick check, at a commit or in
wt-ship's recheck, in every repo. `/etc/sandbox-secrets.sh` exports 11 such variables
into every shell, and a filedrawer test's failure message printed the environment a
subprocess received, so a secret's value reached the session's output. The owner
chose "Checks only": a `heavy -- CMD` job keeps the environment, so one that needs a
secret, such as filedrawer's `ops/smoke-staging eval`, still runs.

BASH_ENV goes too: it names `/etc/sandbox-persistent.sh`, which sources the secrets
file, so every bash a check started had them back (a review of the plan found it).
The other way, a variable the persistent file's secrets block would honour, came
later, below; BASH_ENV stays stripped as well.

The secrets file also exports EBAY_CERT_ID, eBay's client secret, whose name no part
marks. So the gate also leaves out every name that the file `$AGENT_SECRETS_FILE`
names exports; sandbox-setup's secrets block sets the variable to that file. The
owner's terms (2026-11-09): llm-toolbox names no sandbox path, a missing or
unreadable file is no error, and names are read, never values. The gate reads the
file's `export` commands with `lib/shellparse.py` rather than sourcing it in a bash
and listing what it exported: that would run the owner's file at every check, with
whatever command substitutions it holds, and pass every value through another
process. The variable is named for the environment it describes, not after the gate,
so another tool may read it.

It is the marker too (the owner's call, 2026-11-09): a check that starts `bash
--login` or `bash -i` (dot-files' tests do) reads the secrets block again through
`/etc/profile.d` or `/etc/bash.bashrc`, whatever BASH_ENV says, and the block sourced
the file every time. Now each check gets AGENT_SECRETS_FILE set to empty, after the
gate reads the names, and sandbox-setup's block keeps a value it inherits and sources
nothing while it is empty. So the variable means two things: the file that holds the
secrets, and, set and empty, "source none here". A reader must never default it with
`${AGENT_SECRETS_FILE:-...}` or Python's `or`, which read empty as unset; `-` keeps
it. A bash started with a cleared environment (`env -i`, a test's own clean one) has
no marker and still sources the file. dot-files' tests start theirs so, from
`clean_env()`, which passes AGENT_SECRETS_FILE through as it finds it, empty too
(2026-11-09), so their shells under a check get no secrets. BASH_ENV stays stripped:
it holds without the other repo's half, as on a sandbox whose persistent file
predates the marker.

### build-state finish stops only for what a fold, or the repo's safety, depends on (2026-11-09)

The fingerprint came from an anki-cards retro (132c422): a subagent's `rebase --exec`
rewrote the shared config from a worktree unnoticed. In a filedrawer session on
2026-11-09, finish stopped three times on changes it had no reason to: twice
`refs/heads/main`, as other sessions landed, and once `core.hooksPath`, which another
session's install.py set. The build's base was a commit of the owner's in the
worktree, so main was neither behind nor ahead of it and wasn't the target. Now:

- In a detached build, the main tree's branch is the target whenever HEAD shares
  history with it (`git merge-base`), so its moves only bring the note to rebase.
- `core.hooksPath` (the owner's example of a key a fold can't be affected by) and the
  settings of any branch but the build's own are reported, not a stop; the build
  skill says to pass the report on to the user.
- A remote-tracking ref made, moved or deleted is reported too, unless it's the
  target's (named as moved) or the upstream (left out of the fingerprint): the
  owner's call (2026-11-09), after a topic branch pushed to get CI's results, and
  deleted after, stopped finish. Nothing a fold or a landing does reads one: the fold
  is given its base, `rewritten_base` reads the upstream and the targets, and wt-ship
  lands on `REMOTE/TARGET`. Worktrees share refs, so one fetch anywhere can move
  dozens; the report names a ref only when it pointed at a commit the fold may
  rewrite, at start or now (the CI push of the build's own work), or is a symbolic
  ref repointed, and counts the rest.
- On a build on a named branch, that branch's own `branch.NAME.remote` and `.merge`
  are reported too, each when it wasn't set at start: the owner's call (2026-11-09),
  as `git push -u origin NAME`, the workspace's PR flow, sets them. So that a change
  finish only reports changes no fold, `rewritten_base` reads the upstream the branch
  had at start (`work_refs`), not `@{upstream}` now, which after such a push names the
  CI push of the work itself. While the branch's keys are as at start, the two name
  the same ref. Its other keys, and those two when they were set at start, still stop
  finish: `.remote`, `.pushRemote` and `.merge` decide where a bare `git push` or
  `git pull` goes, the reason `remote.*` stops (below).
  The two are reported only while the branch's upstream is what such a push leaves:
  the ref it maps to (`git for-each-ref --format='%(upstream)'`) is a remote-tracking
  ref of the branch's remote, `refs/remotes/REMOTE/...`, that exists and wasn't there
  at start (the owner's call, 2026-11-09, closing a hole the plan's review found). git
  takes `branch.NAME.remote=/tmp/x` with no `remote.*` entry, as `git push -u /tmp/x
  NAME` in a leaked test sets it, and maps no remote-tracking ref for it, so such a
  pair stops; so does a local upstream (`.remote=.`), a remote whose fetch refspec
  writes another remote's namespace, and a remote branch deleted since the push. The
  stop line says which.
- The rest of the config still stops finish. `remote.*` can't change a fold either, but
  a test leaking into the real repo can point `remote.origin.url` at a temporary repo,
  which is the damage the fingerprint is for, and wt-ship would push there next.

Hooks do run in a fold (pre-rebase, post-checkout, prepare-commit-msg, post-commit,
post-rewrite), and a prepare-commit-msg hook can change a folded commit's message, but
hook files were never in the fingerprint, so a hook added under the old path ran
unguarded too.

Left to the owner (reviews of the plans found them): a push over another session's
remote branch is only reported, or not at all when another worktree has that branch
checked out. A `push -u` whose remote branch is deleted again before finish (`git
push origin --delete NAME`, `gh pr close --delete-branch`) stops, since the ref its
upstream maps to no longer exists; letting that pass when the ref wasn't there at
start would still stop a path, URL or local upstream. So does one over a remote
branch there at start, pushed before the build without `-u`. A branch that tracked
something at start (`git checkout -b work origin/main`) still stops on `push -u`,
which changes its `.merge`; so does one under a global `branch.autoSetupRebase`, for
which `push -u` also writes `.rebase`. And a later build on the branch starts with its
own CI push, `origin/NAME`, as its upstream: when its base leaves HEAD's history (a
rebase onto a fetched main, then a force-push), `rewritten_base` folds from that push
and fails on the fixes before it; skipping such an upstream would lose a
collaborator's real rewrite of a shared branch.

### build-state finish refuses a build that skipped its review or refactor (2026-11-09)

The one stop beside a fold's and the repo's safety (above). In a filedrawer session
on 2026-11-09 the agent wrote its second commit's work before the first commit's
review, and never ran `build-state refactor`, so finish's list of test files changed
since the refactor base compared nothing. `review-prompt` now records the commit it
covers, and finish refuses, keeping the state, when no review was recorded, when no
refactor base was recorded after it, and when a commit between the two isn't a fix
(`fixup!`, `squash!`, `amend!`): work no review saw, or when the reviewed commit was
rewritten since into a new tree, as by a rebase, which takes a new review. A reword
keeps every tree, so finish finds the reviewed commit and the refactor base by their
trees after one, as after the reword a conflicting fold's hint gives. Commits after the
refactor base are the refactor's own (since 2026-11-09 /build makes them `fixup!`s,
which fold away), so a new commit made there still finishes
unreviewed: an accepted gap (the owner's call, 2026-11-09), since a rule on the
files those commits touch would also refuse honest refactors, such as a helper
moved to a new file. Rejected: a warning at finish, which comes after the next
commit's work is already written, the very mistake (the owner's call, 2026-11-09: "We
shouldn't change the essence of a feature to avoid breaking changes"). `start` marks
the builds it opens (`checks_steps`), and finish checks the steps only of a marked
build, so a build another session opened before this landed, whose review an older
`review-prompt` never recorded, finishes as before (the owner's call, 2026-11-09).

### build-state finish --dropped takes the review after a reviewed commit is dropped (2026-11-25)

In an llm-toolbox build on 2026-11-25 the review covered three commits, the owner
dropped the third, and finish refused: the reviewed commit and its tree were gone,
though the two commits left were unchanged and reviewed. The agent landed without
finish rather than record a review that didn't happen. `finish --dropped SHA...`
(the owner's call, 2026-11-25, from four options) takes the review for what's left
once it has checked it: each SHA is one of the review's commits and none is still in
HEAD by patch-id, and HEAD's commits since the base begin with the reviewed ones
without them, by patch-id (an empty commit by subject) and in order. The last of
those stands in for the reviewed commit, so the other step checks run as before: a
commit added after the drop still needs a review. Dropping from the middle works too.
What a drop can hide is a fix the dropped commit made to one that stays, which the
review read with the fix beside it; /build's Triage has the agent ask the user that
and review again when it did. Rejected: accepting any HEAD that is an ancestor of the
reviewed commit, which can't tell a drop from an accidental reset and misses middle
drops; a `build-state drop` command, more code for the same check; a review of the
drop's interactions every time, a subagent for what is usually nothing.

### build-state review-prompt tries the fold before the review (#10, 2026-11-26)

In a seven-commit build on 2026-11-23 a late fix was folded into an early commit of
the series, twice, and each fold conflicted at later commits (filedrawer's
`skills/versions.txt`, rewritten in every commit that touches a skill). Nothing said
so until a reviewer tried `rebase --autosquash` in a throwaway clone. Now
`review-prompt`, when the build holds fix commits, runs finish's own fold (the same
`fold_base`, the same autosquash rebase) in a temporary worktree, and warns on stderr
of each conflicting file with the commit the fold stopped at, then writes the prompt
as before: a conflict is for fixing before landing, not a reason to hold the review
(the issue's decision). finish prints the same lines when its own fold conflicts.

- A worktree rather than a clone: it shares the object store, so it copies nothing
  but the checkout, and `worktree remove` drops it. It runs with no hooks, no rerere,
  no signing, no auto gc and `--no-update-refs`, so a `rebase.updateRefs` setting
  can't move a branch and nothing lands in the shared repo but loose objects.
  SIGTERM, SIGHUP and SIGINT exit through its cleanup, so a stopped review-prompt
  leaves no worktree registered; when `worktree remove` fails it deletes only the
  trial's own entry under the git dir, not a `worktree prune` of every stale one.
  A clean fold that changes the tree, which finish refuses, is warned of too.
- Only the first stop shows, as git stops there: resolving it by guess to go on
  would make up conflicts the real fold may not have.
- It sees what finish's fold sees, and wt-ship's while the branch it lands on hasn't
  moved: wt-ship rebases from the fork point, an ancestor of `fold_base`'s start,
  and the commits between are replayed unchanged. Checked on a ten-commit series
  with a fix of commit 2 conflicting in a file every commit rewrites: the trial and
  `rebase -i --autosquash --fork-point main` stopped at the same fix, in the same
  file. A main that moved can add conflicts of its own, which neither finish nor the
  trial looks for.
- Cost: about half a second. On a ten-commit series with two fixups, in a clone of
  this repo (224 files), review-prompt took 0.60 s against 0.14 s without the trial
  (hyperfine, 15 runs each, twice, both under heavy's outside-load warning, about
  one core). A bigger checkout costs more, as the worktree checks out every file. A
  build with no fix commits skips it.

### build-state finish takes a hand fold that left the reviewed tree as it was (#11, 2026-11-26)

After a review in a build on 2026-11-23, the session folded its fixups by hand,
resolving conflicts, and left the tree byte for byte the reviewed one plus the fixes
the review asked for. finish refused, since the reviewed commit and its tree were
gone from HEAD's history, and the session went on with `start --force`, which drops
the record of the review. Now, when the step checks fail, finish looks for the
build's tip before the rewrite: the newest commit in HEAD's reflog that holds the
refactor base. When that tip passes the step checks and its tree is HEAD's, the
review stands, and finish says so; when the trees differ, it refuses, naming the
files and the `git diff TREE HEAD --stat` that shows the change.

- The reflog, not a tree recorded in the state (the issue left the choice): both
  live in the git dir and outlast a context reset. No build-state command runs
  between the refactor's last fixup and a fold by hand, so the state would need a
  commit hook to record each tip, and that hook runs only where it is installed and
  enabled for agents; the reflog also sees a commit made from the host. With
  `core.logAllRefUpdates` off there is no reflog, and finish refuses as before.
- What else reads the reviewed commit doesn't need it in HEAD's history: the fold
  (`fold_base`, the autosquash, `foldable` and `Fold-check: keep`) reads only the
  base, HEAD and the branch it lands on, and the test-file report diffs the refactor
  base's tree. `--dropped` matches HEAD's commits to the review's by patch-id, which
  a fold by hand changes, so a drop and a hand fold together still need a new
  review.

### wt-ship --remove keeps a worktree that processes still run in (2026-11-25)

In a filedrawer session the agent stopped `bin/live-preview`, which it had started in
the worktree, by hand before `wt-ship --local --remove`; a session that forgot would
leave a server running from a deleted folder. git removes a worktree whatever runs in
it. The owner's call: `--remove` keeps the worktree when a process of the user's has
its current folder or executable inside it, lists each one with the command that
removes the worktree once they're stopped, kills nothing, and exits 0, since the
landing worked. A nonzero exit would make that landing look failed and invite a rerun;
stopping the processes would kill what a session or the user still wants.

- The caller's ancestors and its pipeline siblings (in its process group, with an
  ancestor for parent: `wt-ship --remove | tee log`) don't count: they end with it.
- Another user's processes can't be read and don't count; a count of them would print
  on every run, root's being always unreadable.
- Open files and arguments aren't read, so an editor with a file open there doesn't
  keep it, and a program run through a symlink in the tree (a `.venv` python) whose
  folder is elsewhere is missed.
- A failed look keeps the worktree and says so, still exiting 0.

### wt-ship refuses a message naming an unlanded commit by its ID (2026-11-09)

In a filedrawer session, commit messages cited `cc417d7`, a commit of the same
unlanded range; rebases rewrote it, and the messages were reworded by hand. The
owner's rule: before any check, a word of a commit message that resolves (`git
rev-parse --verify --quiet WORD^{commit}`) to a commit of this repo that the branch
being landed onto doesn't hold, once the rebase is done, refuses the landing, one line
per commit and word, naming `build-state reword SHA`. A hex word that resolves to no
commit here is ignored.

- Words of 7 to 64 lowercase hex digits count. 7 is git's default abbreviation, and
  `cc417d7` is 7 long; a word that isn't an ID would have to match the start of one of
  the repo's commits by chance (about N in 268 million, for N commits) and that commit
  be off the branch. Shorter words are mostly English (added, decade).
- A commit of the same landing is refused too, though its ID would hold if nothing
  rebased again: a lost push race, or the branch moving under the claim, rebases it.
- It runs after every rebase, so with --local before the wait for the main tree.
- The line `git cherry-pick -x` adds, `(cherry picked from commit ID)`, whose source
  is meant to stay off the branch, doesn't count when a ref that lasts holds that
  source: the owner's calls (2026-11-09), "exempt IDs on that line only", then "only
  when some ref (branch, tag, remote-tracking ref) holds the commit", since a pick from
  an unlanded worktree, or a hand-written line in git's form, let through an ID a
  rebase would change. The line must be whole, as git writes it; the same ID anywhere
  else in the message still counts.
- The refs that don't last, for this: a branch a linked worktree has checked out (the
  recommendation the owner took) and its twin on a remote, such as a `push -u` for
  CI: that is a session's work, which its landing rebases. The main tree's branch
  counts, as build-state's `other_worktree_branches` leaves it in: landings
  fast-forward it. A commit of this landing's own never counts as held, whatever ref
  holds it, since its rebase rewrites it: one HEAD holds, or one with the patch (`git
  patch-id --stable`) of another commit of the landing, as the copy from before a
  rebase has, which a detached worktree's push for CI or a backup tag keeps (a code
  review found the copy passing). When no ref at all holds a pick's source, as for a
  pick from another repo's `FETCH_HEAD`, the refusal says how to give it one (a tag,
  or a fetch into refs/remotes/) if it should last.
- This guards against mistakes, as the rest does: a ref made on purpose (`git tag t
  ID`) passes any ID. Wider citations of a held commit stay refused, as the owner
  said not to widen it: "Backport of 1a2b3c4" above the -x line, and `git merge
  --squash`'s default message, which lists each squashed commit as `commit ID`.

Left to the owner (a review of the plan found them): worktrees share one object store,
so another session's unlanded commit or a PR topic commit resolves here and is refused
though its ID will last; and `build-state reword` refuses a commit a remote-tracking
branch holds, as after a push for CI. A remote-tracking ref holding a pick's source
exempts it, though a topic pushed for CI from another session's detached worktree is
one, and that landing rebases the commit; and a stale one, a branch deleted on the
remote but kept here until `fetch --prune`, exempts an ID the remote has lost. A
named-branch worktree mid-rebase shows as detached, so its branch isn't left out
while its landing rewrites it.

### A fix commit that changes a file its target didn't gets a warning (2026-11-09)

In a /build, `git commit -a --fixup=A` swept in test edits meant for commit B;
`build-state finish`'s fold then conflicted in the test file, and the agent reset and
split both fixups again. The owner's rules: warn, never refuse, since a fix may add a
file its target forgot; find the target as `git rebase --autosquash` does; say nothing
when there's none. Since /build's refactor became `fixup!`s (2026-11-09), a refactor
that moves a helper into a new file warns too: expected, and fine to go on.

- It runs from a post-commit line that `--install` writes. The pre-commit gate already
  reads the running `git commit`'s arguments, so it could have warned with no
  re-install, but a review of that plan found its advice could amend the wrong
  commit: a second commit chained in the same call, or a commit-msg hook or git
  itself refusing the commit after pre-commit passed, leaves HEAD elsewhere. It also
  missed `-F -`, the editor, `-C` and `--amend --no-edit`. Post-commit runs only for a
  commit that landed, reads its own subject and files, and the advice names its ID
  ("while HEAD is ..."). Each repo installed before needs `--install` again.
- The range is what a rebase onto another branch, a tag or a remote-tracking branch
  would replay, and a match goes to the oldest, as autosquash takes it (checked
  against a real `rebase -i --autosquash`). A target outside the range has landed and
  no fold reaches it, so nothing is said.
- The target's files include those of its earlier fix commits, which the fold gives
  it, so a fixup adding a test file the target forgot warns once, not at each later
  fixup of that test.
- Only under `git commit`, and with no rebase, cherry-pick or revert under way (its
  folder in the git dir): git's rebase runs post-commit for each commit it makes, some
  through a `git commit` of its own (a reword, a `--continue`), and in a detached
  worktree a replayed fixup's target is in range. A single cherry-pick or revert keeps
  no folder, but runs as no `git commit`.
- The advice names each path from the top of the work tree (`:/`), so it works from a
  subfolder, and says to unstage first what else is staged, which its amend would
  take in.
- One line per file, as the owner asked, with no cap.

Left to the owner: build-state's `fixed_name` and `names_commit` find a fix's target
by a prefix of the subject or an ID, newest first, which can differ from autosquash
and from this warning when two subjects match.

### build-state sweep searches for stale text and records it (2026-11-10)

/build's step 2 gave the builder a raw rg to run over the workspace for text a
behaviour change made stale. In a three-commit build (retrospective, 2026-11-10) it
was skipped for the first two commits, and a skill kept saying "Nothing records a
search" after a search log landed; nothing recorded whether the sweep ran. The owner's
design: `build-state sweep TERM...` runs that rg from the workspace root, prints the
hits, capped, and records the terms and HEAD in the build's state; finish warns, never
refuses, of each commit since the base after the last sweep, or of a build with none,
saying a build that changes no described behaviour may ignore it.

A review of the plan changed the rg and the warning (choices made in the build, not
the owner's):

- The root's own dot-folders are skipped (`-g '!/.*'`): the workspace root is no git
  repo, so no .gitignore hides `.sofa/credentials.json` and the API keys beside it,
  and a sweep for `token` would print them.
- Terms are fixed strings, since most are phrases and option names (`f(`, `--local`),
  and `--no-config` keeps a user's rg config out.
- In a linked worktree, the repo's own hits come from the worktree, and its main tree
  is skipped: the worktree glob skips the build's own edits too, so a fixed hit kept
  showing from main and a stale line the change wrote never did. Its hits come first,
  at most 5 lines a file and 60 in all, so another repo's long file can't push them
  out.
- review-prompt warns too, on stderr, before the review the sweep exists to spare; a
  warning at finish alone comes after it.
- A sweep made with uncommitted changes covers the next commit, and one whose commit
  was amended is found by its subject, since builders sweep before committing and
  amend the fixes in.
- rg exiting 2 for an unreadable file still records the sweep, saying so; a root that
  is $HOME or / is refused, as a clone straight in the home folder would walk it all.
- Other repos' hits print as lines only while they come to 20 or fewer, at 5 a file;
  past that, as a count a file for 20 files (2026-11-19). A sweep for `Strict` from
  filedrawer printed 60 lines of 300 characters, about 5.7k tokens, nearly all from
  another repo's paper texts and a generated .jsonl, and cut 175 more; the counts
  still name the files a stale copy may sit in, for the cost of a line each.
- Fix commits after the sweep don't count, though a spec-gap fix may change described
  behaviour: counting them would make nearly every build warn, through its review's
  fixes and refactor.
- A term too common to read is set aside (2026-11-24). /build has the sweep search
  the tool's name beside its old phrases, and on 2026-11-24 three names
  (`checks-needed`, `heavy`, `agent-commit-gate`) each filled the cap of 60 lines and
  hid the phrases' hits, which the agent then fetched again with greps. Once the
  lines to list pass the cap, a term with more than 60 hits of its own is dropped
  from the listing with a line giving its count, except where another term matches
  the same line, which rg's `--only-matching` tells even past the 300-character cut;
  the last term always stays, capped. Other repos' hits count toward the cap only
  while they still print as lines. Session transcripts (`*.jsonl`) are skipped as
  files nobody wrote by hand: one sweep listed another repo's.

The workspace root is the folder that holds the repo's main tree, not path-refs'
hard-coded default, so it holds on any machine with the workspace's layout.

### Rejected: `wt-ship --check CMD` (removed in 03bc4c8)

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
against haiku's 114), a third of its false rate, and more real findings. A run's
input tokens are the same whatever the model, so sonnet costs its higher price
per token; the owner's rule above accepts that. A run takes a few minutes. Two
sonnet runs each found a finding the other missed, so one run is not exhaustive.

/upskill's agent still reads `--errors` itself and accounts for every failure:
the scan adds to that read, it doesn't replace it. Of the baseline, item 7 (a
test failure) came from the errors, and no run of the scan with cut text found
it.

### session-scan reads whole messages and output tails (2026-11-09)

The digest cut each assistant message to 400 characters and printed no
successful output, so a wrong assumption deep in a long report, or an output
the agent misread, was out of the reader's sight. In a8a346b2, 82 of 361
messages ran past 400 characters (154k characters more, whole), and successful
outputs held 4.2M characters, too many to read. session-scan now digests with
`--whole-text` and `--tails` (each output's last three lines, at most 300
characters). Measured with sonnet on a8a346b2, scored like the runs above:

| digest | chunks | input tokens | candidates | real | false | distinct real findings | baseline found |
|---|---|---|---|---|---|---|---|
| text cut, no tails | 12 | 201k | 82 | 28 | 3 (4%) | 12 | 1-5 |
| whole text | 16 | 271k | 87 | 35 | 2 (2%) | 15 | 1-6 |
| whole text and tails | 25 | 407k | 128 | 44 | 7 (5%) | 17 | 1-7 |

Each step lost none of the findings before it. Whole text found a reviewer's
admission, at the end of a 6k-character report, that its unqueued test runs
skewed another agent's benchmarks, and an environment variable that BASH_ENV
exports again after a test unsets it. Tails found the one baseline item no
earlier run had, the timing-test margin, in a FAILED summary line, and a
`waitone` timeout past the Bash call's limit. 11 real candidates rest on whole
text and 9 on tails. The cost: twice the input tokens, more trivial candidates
(77 against 51), and three false ones from reading a tail as the whole result,
such as a mutant caught by mutate-check taken for a failing test; the reader's
prompt now names that case.

That run took tails as they came; on its digest, 115 of 1994 tails held only
Claude Code's notices ("Shell cwd was reset to …"), and those of Read, Write,
Edit and Agent results carried a file's last lines or a confirmation. Tails now
leave both out, and a line that doesn't fit whole keeps its end.

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

### session-digest finds the running session from any folder (2026-11-13)

With no session argument and no `--cwd`, `$CLAUDE_CODE_SESSION_ID`'s log is read
from whichever project holds it, when exactly one does. Before, it was read only
from the project of the current folder: a session started in
`ai-career-plan/fellows-assessment-prep` ran `session-digest --errors` from
`ai-career-plan`, which has a project of its own, and got that project's newest
log, another session's; session-scan scanned it too. An explicit `--cwd DIR`
still searches only DIR's project, since it asks for that project, and
session-scan passes `--cwd` on only when it was given one. When two projects hold
the id, neither is picked, and the current folder's newest is read as before. Then,
and when no project holds it, a line on stderr names the log that was read, since it
may be another session's; session-scan passes the line on.

### session-grep prints commands only, and masks secret-shaped values (#13, 2026-11-26)

An agent looking for the session that ran `git reset --hard HEAD~1` in a main
tree grepped the raw JSONL about eight times. JSON escaping hid matches, and each
try printed 1 to 2k characters of log. session-grep decodes each assistant
record's tool calls and matches the command as typed. Every tool_use record checked
(about 3,900 in the 30 newest main logs, 1,100 in the 30 newest subagent logs,
2026-11-26) carries `timestamp`, `cwd` and `sessionId`. A subagent's log has
`agentId` and sits in its session's folder, and no main log held a sidechain record
with a call. So the session and agent come from the log's path, and the time and cwd
from the record.

It reads only calls' inputs. Outputs and message text are never searched, since
an output can hold anything a command printed. A command can hold a secret too,
typed inline (`GH_TOKEN=... gh api`, a curl Authorization header), so values of
the common shapes print as `***`. The pattern matches before masking, so searching
for a token finds the call without printing the token. There is no flag to turn
the mask off. A regex mask misses shapes it doesn't know, so `-h` calls it best
effort and says to treat the output as private. Names that end in a secret word
are masked (`GH_TOKEN`, `--client-secret`) and names that only contain one are
not (`--token-file PATH`). `$references` are left as typed, since they name the
secret rather than hold it. A value after a space is masked only after an
option's dashes (`--password x`). The first version also masked a bare word's
next word, and on 7 days of real logs 473 of its 538 masked commands were that
form, mostly ordinary words such as `grep -rn password src/` and `gh secret set
FOO`. YAML-style `password: x` stays unmasked for the same reason: code holds it
far more often than a secret does.

A call is logged before it runs, so every search found its own call and exit 1
never happened inside a session. A call that runs session-grep is held back until
its result appears in the log, and left out when none does. That skips the
running search wherever it runs (a subagent's log, a parallel batch) without
guessing which call is newest. A session-grep call that was cut off before its
result is left out too.

The default line is short: the session's and a subagent's first 8 id characters,
the cwd with `~` for home, and the project folder only when the log has no cwd.
The first version printed the full ids and the project, a median of 194
characters before the command. `--full` keeps that long form, since
session-digest takes the whole session id.

The log listing and record time are in `lib/sessionlogs.py`, which session-digest
now uses too. session-digest reads each log whole. session-grep streams it and
parses only lines that contain `"tool_use"`, and skips a log last written before
`--since`. A search over all 1.8G of logs took 8.7 s, unmeasured under load.

### session-grep --unfinished-at lists the calls running at a time (2026-11-26)

The sandbox VM died on 2026-11-26 because of a command a session ran.
sandbox-setup's postmortem listed the commands of the last ten minutes and the
jobs `heavy` ran, which misses a command started earlier and outside heavy. A
command running when the VM died is a call with no result logged by then.
session-grep already reads every agent's calls and masks what it prints, so the
lookup is its option, and the postmortem calls it.

`--unfinished-at TIME` keeps a call that started at or before TIME and whose
result was logged after TIME or never. A session resumed after a crash may log a
result for the call that was cut off, with a later time, so "never" alone would
miss it. A result's time is the `timestamp` of the `user` record whose
`tool_result` block has the call's id. Over the logs of four days (36 main, 272
subagent, about 12,500 calls) every result sat in a `user` record with a
timestamp, after its call in the same log, except 8 results in main logs with no
call before them. `api-request-blob` records repeat results and have no
timestamp. They are not `user` records, so they aren't read.

A background Bash call gets its result at once. That result's record has
`toolUseResult.backgroundTaskId`: on all 496 launches that worked, where it
equalled the id in the result's text, and on none of the 5 that failed. The same
field was on 108 results of calls without `run_in_background`, which went on in
the background after they started, as a call that outlasts its time limit does.
Those are the long commands a postmortem wants, so the field marks a background
call, not the input. A `run_in_background` call with no result at all is marked
too. The field is the one thing read from a result besides its time.

The task's end is a notification, text that starts `<task-notification>` and
then the task's `<task-id>`. The main log holds it as a `queue-operation` record
and then as a `user` record with that text as its content (378 tasks) or an
`attachment` record with it as `attachment.prompt` (76). A subagent's task was
enqueued in its session's main log and delivered as an attachment in the
subagent's own log, within a second, for all 143 that ended. So each log tells
its own tasks' ends, and the earliest of the three forms is the end. Only the id
that opens the text counts: one notification quoted a second task's id further
in. Two notifications were logged before their launch's result, so the order
within a log doesn't matter. 7 tasks had no notification. One whose agent ended
first never gets one, and is listed at any later TIME.

`--since` bounds the start, as before, and a log is skipped only when it was
last written before `--since`: a session that died at TIME last wrote its log
before TIME. `--until` is refused, since TIME bounds the start. TIME takes the
forms `--since` takes, so a date alone means the start of that day. The rule that
holds back a session-grep call has no use here: the call either was running at
TIME or wasn't. A record without a timestamp can't be placed, so a call in one
is left out and a result in one counts as not logged.

A job started detached and returned from (`bgrun -o LOG -- CMD` without
`--wait`, `nohup ... &`) is no call's to wait for. It shows only as the call that
waited on it, such as `waitone --pid`. A subagent launched in the background
(the Agent tool) counts as finished at its launch: its result carries no
background task id, so nothing ties a later notification to the call. The calls
that subagent makes are still listed, under its own agent. The `[background] `
prefix goes on any tool's call that went to the background, not only Bash's: the
rule reads `run_in_background` and `backgroundTaskId` whatever the tool.

On the crash's logs, `--unfinished-at '2026-11-26 08:42:30' --since '2026-11-24
00:00' .` listed 9 calls in 1.7 s, 4 of them background, unmeasured under load.

### Rejected: a rebuild only after 40 idle minutes (2026-11-09)

The first rule. It caught all four of the main session's rebuilds, but only one
of the 17 in subagents, whose cache lasts five minutes; the 16 it missed held
about 3.2M tokens.

### session-scan checks its own candidates, with opus (2026-11-09)

/upskill's agent had to check 80 to 130 candidates itself, one call each, at the
end of a session that may already hold 500k tokens of context: every check resends
it, some 64M tokens for 128 checks, against the scan's 400k. Now verifiers check
the candidates (claude-fresh, no tools: the digest has whole messages and output
tails, the raw log is too big to read through tools, and no tools means text in
the logs can't make a verifier read files), and one merge call groups the real
ones into findings. The agent reads the findings, the candidates the verifier
couldn't settle, and the dismissed list with a reason each, and judges those with
its own memory of the session; that read costs it a few thousand tokens.

Measured on a8a346b2's whole-text-and-tails run, against its scored candidates
(44 real, 17 themes):

| verifier | real kept | its real that were real | findings | themes with none kept |
|---|---|---|---|---|
| sonnet | 34 | 34 of 51 | 12 | 2 |
| opus | 37 | 37 of 48 | 17 | 3 |
| opus, after the two prompt fixes below | 40 | 40 of 62 | 23 | 1 (fixed in that session) |

Each run read about 275k input tokens. Opus kept more, so it is the default. Both
had dismissed subagent cache rebuilds because the prompt called ending a turn to
wait "not a finding"; the prompt now says the rebuild after such a wait is still
its cost, and to say real when torn between real and trivial, since the agent
judges every real one again. One merge answer grouped 15 of 62 real candidates
and left the rest as findings of one; a merge that leaves over half ungrouped is
asked again (about 15k tokens), and the better answer kept.

Held out: session 4e895eaf (llm-toolbox), whose old /upskill, reading the whole
digest itself, pitched 9 findings; the owner acted on 6. The scan (sonnet
reading, opus checking; 245k and 160k input tokens) gave 14 findings. Of the 9:
5 found (one in part), 1 dismissed, 3 with no candidate at all. The verifier
dismissed 33 candidates, and only one of them was worth pitching: an old finding
the reader had put down to the wrong cause (polling, where the cost was a review
rerunning a suite that had passed), so the prompt now tells the verifier to judge
the event behind a candidate, not how it is put. The three no reader raised
fell outside the categories as written: a change to a shared tool that slowed
other sessions, a problem the agent noted and left for later, and a design
premise the user had to correct. The categories now name the first two kinds of
cost; rereading those chunks, the reader then raised the first, not the second.
So the scan also lists, by pattern, every line where an agent set something
aside ("left for later", "out of scope", "TODO"): 5 lines on that session, the
missed one among them. A premise the user corrected sits in the user's turns,
which /upskill's agent reads itself. The scan found 5 real findings the old pitch
lacked.

The scored runs, the held-out check and the script that compares a verifier's
verdicts with the scored ones are kept outside the repo, in
/home/carver/code/claudedev/keep/session-scan/; its README.md says what each
part is and how to check a change to the verifier against them.

## claude-fresh

### Auto memory off, and the call's empty folders removed (2026-11-09)

Claude Code makes `<config>/projects/<name>/memory` at the start of every call,
named for the call's working directory, and claude-fresh gives each call a new
temporary one. On 2026-11-09 the owner's VM held 4,312 such folders, all empty
(2,618 from `claude-json`, 964 from `claude-fresh`, two months of cron and script
calls), and sandbox-setup's `link-claude-memory` spent about 30% of its run time,
every minute, checking them.

So the child gets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (the owner's choice,
2026-11-09): a throwaway call on untrusted text gets no memory instructions, and
Claude Code 2.1.321 then made no `projects/` folder at all, run offline as below.
A Claude Code that ignores the variable still makes the folders, so when the call
ends claude-fresh removes `memory/` and then the project folder, each with
`rmdir`: it refuses a folder with anything in it (a transcript keeps the project
folder), a link and a missing path, with no gap between a check and the removal.
The project folder is opened with `O_NOFOLLOW` for the first `rmdir`, so one that
is a link is never gone through.

What Claude Code 2.1.321 does was read from its binary and checked by running it
offline (`unshare -n`, a scratch `HOME` and `CLAUDE_CONFIG_DIR`, so no API call):
`<config>` is `$CLAUDE_CONFIG_DIR`, else `~/.claude`; the folder appears even when
the call fails; `<name>` is the physical path, a link's target, with each character
but a letter or digit made `-`, and past 200 characters, the first 200, `-` and the
base-36 absolute value of the path's Java `String.hashCode`. JavaScript counts
UTF-16 code units, so a path that isn't ASCII is left alone. With
`--no-session-persistence` the call writes no transcript, so the project folder
holds only the memory folder.

The path is worked out before the call, as Claude Code works it out at its start,
so a link that moves during the call doesn't change it. The name holds the call's
random directory name, so another project shares it only by chance (the naming is
many-to-one: `/tmp/claude-fresh-ab_c1234` and `/tmp/claude/fresh/ab/c1234` share
one), and even then only empty folders go, which Claude Code makes again when it
next starts. Where Claude Code puts the memory elsewhere, the computed folder is
missing and nothing goes: a working directory inside a git repository (checked:
the memory folder is the repository's), `CLAUDE_CODE_PROJECT_DIR_NAME`,
`autoMemoryDirectory` or `CLAUDE_CODE_REMOTE_MEMORY_DIR`.

## drive-agent

### Each run is a system user in bwrap, with a filtering proxy (2026-11-10)

drive-agent runs the harness under test as a new system user inside `bwrap`: `/` read-only, its own `/tmp`, `/var/tmp` and `/dev/shm`, a tmpfs over `/home` with only its home bound in, its own PID namespace, and `/etc/sandbox-persistent.sh` shown empty.
Its network goes through a proxy drive-agent runs, which refuses github.com, api.github.com and loopback or private addresses; a per-UID iptables rule rejects every other connection.
The box has its own network namespace, so its loopback is its own: a tool's own server there, such as the MCP server Codex's TUI starts in a git project, is reached directly (`NO_PROXY`), and the box reaches the proxy through a unix socket at `/run/user/<uid>/bus`, which the in-box launcher relays from the proxy's port.
Reason: round 1 of filedrawer's usability runs (filedrawer#68) ran agents as the driver's own user, so they could have read or written the workspace, and through the sandbox proxy, which adds a GitHub token, pushed to a repo whose main deploys staging.
A separate user alone still lets the agent read world-readable workspace files, such as the project's design docs, which spoils a usability test; the shared `/proc` showed the driver's command line, and bwrap's own command line, as PID 1, showed how the box was made, so bwrap reads its options from a file.
The agent's environment is a mode-600 file the box reads, never an argv, since every user can read `/proc/*/cmdline`.
Claude Code gets only the driver's access token; Codex a copy of `auth.json` without its refresh token, which `codex exec` was checked to run on.
Cleanup removes the user, home, state folder and rule, and no files by UID: useradd hands the same UID out again, and Docker's image layers hold files owned by it.

### Claude Code's question dialog ends a turn, read from the transcript (2026-11-13)

When the agent calls AskUserQuestion, Claude Code draws a dialog and fires no Stop hook.
drive-agent takes the dialog as the end of the agent's turn only while the transcript holds the call with no tool_result and the dialog's footer is on screen, then types the next turn into its free-text option and waits for the tool_result before waiting for Stop.
Reason: in filedrawer's usability runs of 2026-11-13 the screen alone misled it.
An answered dialog stays drawn a moment after Enter, so the turn "ended" while the agent worked and a restart cut its install short (jason86); a dialog of two questions moved to its second tab, which also looked like a turn end, so nothing was submitted (jason82, jason85).
A dialog of several questions, or a multi-select one, is left through "Chat about this", and the turn is sent as an ordinary message once the agent's reply ends: the script holds one answer and tabs need one each, and a person answering in their own words does the same.
Checked against Claude Code 2.1.325: the option's number takes effect at once, the call gets a tool_result rejecting it ("The user wants to clarify these questions"), the agent asks what to clarify and ends its turn, and the prompt then takes text.
A multi-select dialog of one question leaves the same way, checked in one real run against Claude Code 2.1.332 (probe5m, 2026-11-26): the agent asked what to clarify, then took the typed "Cheese and olives." as the choice.
That dialog draws a tab line of its one question and Submit, as a dialog of several questions does, and its options, "Type something" included, as checkboxes ("4. [ ] Type something"), so a multi-select dialog read from the screen is told by its checkboxes, and its free-text option would take no typed answer.
The agent sees a declined dialog, not answers, which a usability finding should allow for.
A session reads only its own transcript: before its first prompt the newest one is the last session's, and the simulated human answered that session's dialog again after a restart (jason86).
Codex has no such dialog in default mode: `request_user_input` is for Plan mode, and `request_user_input_async` returns at once and shows its question as an agent message (next entry).

### Codex's request_user_input_async question ends a turn, answered as a message (2026-11-26)

When Codex calls `request_user_input_async` in a turn that still runs, drive-agent takes that as the end of the agent's turn: the simulated human reads what the agent said since the human's last message, the question included, and the next turn is typed as an ordinary message.
The turn after it waits for a turn end that follows the answer in the transcript, so a message that reaches the running turn and one that starts a new turn, when the agent ended its turn first, both wait for the turn that holds the answer.
Reason: in jason97 (filedrawer's usability runs of 2026-11-13) Codex asked whether to attach code, got `{"accepted":true}` at once, worked on, slept 60 s, and posted without an answer ("If you have no preference, I'll submit it without code"). Codex's notify comes only when the whole turn ends, so drive-agent never saw the question, and the notify's message, the turn's last text, may not hold it.
Checked in one real run against codex-cli 0.164.0 (probe5c, 2026-11-26): a message typed while the agent slept in the same turn reached that turn, as a user message in the rollout before its `task_complete`, and cut the sleep short; the agent wrote the answer and ended the turn.
A question in a turn that has ended is no turn end of its own: the turn ended already, and the human reads the question with the agent's last message.
The question goes last in what the human reads, so the prompt's cut to the message's last 6,000 characters keeps it.
When the next scripted turn is skipped, a conditional one that doesn't hold, drive-agent waits for the turn that asked to end before the turn after it: sent at once, that turn would go into the running turn as the answer. jason97's scenario has that shape, a go-ahead only if asked for, then a turn of its own.
A user message counts as the human's unless it starts with a tag Codex adds as context (`<environment_context>`, the only one seen in 0.164's rollouts), since a human's answer may itself start with "<".

### A person's step mid-run is a file the run waits for; a token outlives the run only through --keep-file (2026-11-26)

A scenario turn `{"wait_for_file": "NAME", "say": "..."}` prints one line naming `<--out>/NAME`, waits for that file past no turn limit (`DRIVE_AGENT_WAIT_LIMIT`, a day by default), then sends `say` as an ordinary turn; turns.md logs the wait.
`--keep-file REL:DEST` copies `~/REL` to DEST, mode 600 and unredacted, in the run's cleanup, before the home goes, on every exit, and prints only that it kept the file or why not; `files/` keeps its redacted copy.
Reason: filedrawer#141's no-account scenario (2026-11-24) had the agent make an account and ask for a claim, which the owner confirmed in a browser mid-run, and then meant to check the account with the agent's token.
`run` types every turn in one loop with no hook for its caller between turns, so the pause has to be a turn of its own; and the home goes on every exit, so the token, which the runner never had, could leave only through drive-agent.
A file in the run folder works from any shell or another agent, and holds the run with no terminal attached.
DEST must not exist and is opened with `O_EXCL | O_NOFOLLOW`, so a run never overwrites a file or follows a link someone planted there.
`~/REL` is read as the box's user but outside bwrap, where a link the agent planted would reach a file the box hides from it, such as `/etc/sandbox-persistent.sh`, and the caller would then send that file on (filedrawer sends the kept header to its site). So the read opens one folder at a time with `O_NOFOLLOW` and takes only a regular file; a link anywhere on the way keeps nothing and says so.
A wait turn's NAME is a bare file name, none drive-agent writes in the run folder (`turns.md` would end the wait at once) and no earlier wait turn's (the file is already there); print mode, which sends one turn, refuses one. turns.md logs the wait as it starts, so a run stopped mid-wait shows what it waited for.

### A stuck tmux server is killed by its own tree, and only while it is one (#16, 2026-11-26)

When `tmux kill-server` doesn't return, `kill_tmux_server` SIGKILLs the server's pid and every process under it, read from `/proc/PID/task/*/children` one level at a time, the whole tree before the first signal.
Reason: the first version scanned every `/proc/*/stat` for the server's pid as parent, so a single negated comparison made every process the user owns a victim: the mutant that killed six daily mutants runs and, applied to the main tree's live file, the VM (see mutate-check's PID namespace entries). Read from the server's own tree, no inverted or dropped test can reach outside it. It also takes a pane's grandchildren, which the parent-pid scan left running.
It signals nothing unless `/proc/PID/comm` still reads `tmux: server`, so a pid the ended server left to another process is spared.

### Rejected: a separate user without bwrap (2026-11-10)

It blocks writes but not reads of the workspace, which is world-readable, and leaves `/proc` and `/var/tmp` shared between runs.

## skill-change-hint

### A loaded skill that changed on disk gets one note, never a refusal (2026-11-10)

An agent follows the copy of a skill it loaded. On 2026-11-10 the build skill's
SKILL.md changed at 01:04, mid-session, dropping a step; the session kept to its
loaded copy, ran two long mutate-check jobs the skill no longer asked for, and the
owner had to point it out. The owner settled the hook's shape: before the agent's
next Bash or Agent call it adds context naming the skill, when it changed and its
path, once per change, and it never blocks. Loading the skill again takes the new
copy as read.

What the hook sees, from Claude Code 2.1.322 run with hooks that saved their input
(`claude -p` in a scratch project): PostToolUse on Skill gives `tool_input.skill`
and `tool_response: {"success": true, "commandName": NAME}`, no path, so the hook
finds SKILL.md itself, user skills before project skills, as Claude Code ranks
them. A slash command reaches UserPromptSubmit as the raw prompt (`/name args`),
not in the `<command-name>` tags the transcript holds; the hook reads both. A
subagent's events carry its parent's `session_id` and an `agent_id` of their own,
so loads are kept per agent, or a subagent's load would send its parent a note
about a skill the parent never read. PreToolUse `additionalContext` reached the
model on both Bash and Agent calls.

## The gates' refusals

### A refused command is saved, with its heredocs, in the session's scratchpad (2026-11-23)

A refused Bash call runs none of its parts, so an agent types it all again. In a
filedrawer session on 2026-11-23, edit-script-gate refused an inline script of about
6,000 characters of edits, which the agent rewrote as apply-edits blocks, and
pipe-status-gate refused a call that began with an apply-edits heredoc of about 8,000
characters, which the agent sent again unchanged; the global rule to write large
content in a call of its own held neither time. So every gate's refusal ends with
`lib/refused.py`'s ending: NOTHING_RAN, then the files it saved the command in
(`<h>.txt`) and each heredoc body (`<h>-<n>-<delimiter>.txt`), to copy from, or
`apply-edits < FILE`. The files are text, not scripts: a saved script would let `bash
FILE` run what a gate refused, since no gate reads inside it.

They go in the session's scratchpad, the folder named like the transcript's, else the
only one holding the session's id, under Claude Code's temp root (`CLAUDE_CODE_TMPDIR`,
else `TMPDIR`, `TMP`, `TEMP`, `/tmp`). That layout is Claude Code's and undocumented,
which ruled it out as web-shot's default (above); here a save that finds no folder
saves nothing and the refusal reads as before, so nothing depends on it. A command may
hold a secret typed inline, as its transcript does; the files are 600 in 700 folders.
Claude Code never cleans these folders but sweeps transcripts after 30 days, so each
save removes saved files older than 30 days from every session's `refused/` folder
(the owner's choice over pruning only the session's own), and only the names it
writes, never through a link.

## The main tree only receives landings

### A commit in a main tree is refused; an edit there gets a note (#4, 2026-11-26)

The owner's rule is that agents edit and commit in a linked worktree, and a repo's
main tree only receives landings. Measured over the transcripts from 2026-11-01 to
11-10 (`keep/llm-toolbox/main-tree-writes/`), 71 sessions ran `git commit` in a main
tree 333 times in 21 sessions, Edit or Write into one 525 times in 13, and shell
writes 1,916 times; of 10 sampled commits, 7 were plain mistakes. The owner's
decisions: refuse the commit in agent-commit-gate's PreToolUse mode, before anything
runs, in repos with an agent check (scratch repos stay free); let an edit through,
since it has happened, with a PostToolUse note (bin/main-tree-hint). A refusal in
the git pre-commit line was dropped: it would refuse only after the hook's formatters
had written in the main tree, and need every repo's hooks reinstalled.

The gate refuses every agent session in every such repo, so it lets a commit through
whenever it isn't sure. lib/maintree.py's walk, on lib/shellparse.py, follows `cd`,
`pushd`, subshells, `git -C` and bgrun's `-C`, and gives no directory for a word with
an expansion, `cd -`, a sourced file or eval, `env -C`, GIT_DIR or `--git-dir`; git
that can't answer, as for a worktree `git worktree add` makes earlier in the same
command, passes too. The old commits() read `(cd ../r-wt-x && git commit)`, pushd, `cd
-` and `--git-dir` as the session's cwd (12 of the 333 commands). A cd inside a
piped or backgrounded group, as in `echo m | (cd ../r-wt-x && git commit -F -)`,
makes the directory uncertain, since a scope can't tell the group's commands from
those after the pipeline. A list sent to the background, as in `cd WT && sleep 900 &
by=$!; heavy -- bin/mutate-check ...`, moves only its own commands: that command once
left mutate-check running in a main tree, and its mutants killed every process on
the VM (2026-11-26). The gate's older checks (--no-verify, the queue
predictor) read each step's `guess` instead, which follows the plain cds alone, so
a sourced file, a function or GIT_DIR changes nothing for them. The refusal names a
repo's own `bin/new-worktree` when it has one (filedrawer's AGENTS.md). Exempt: an unborn
HEAD, a submodule, a bare repo, and a merge, cherry-pick, revert, rebase or sequencer
in progress, whose commit finishes it. The opt-out is a one-shot `AGENT_MAIN_TREE_OK=1`
prefix, visible in the command; the refusal says to ask the user first and doesn't
name it. Claude Code blocks a call only on exit 2, so the refusal shares that status
with the gate's other two and its first words tell them apart.

A dry run of the gate over 2,441 Bash calls holding a `git commit` (2026-11-02 to
11-26, against the filesystem of 11-26) refused 223, each in one of six main trees
(175 in filedrawer, 27 in llm-toolbox, 15 in sandbox-setup), all main-tree work from
11-03 to 11-07 but a message amend on a landed commit (11-18) and a hook probe
(11-09). None of the 18 whose output showed a commit on a detached HEAD, a
worktree's, was refused. The first replay refused three review-scratch runs that
sourced a script which made and entered a repo (`. $S/mk.sh`), so a sourced file now
makes the directory uncertain. A replay can't see the state a commit met then (an
unborn HEAD, a rebase), so the tests cover the exemptions.

The gate takes the hook's `cwd` as where the command starts and keeps no state
between calls. Claude Code documents cwd as the directory when the hook runs; in the
transcripts a call's cwd follows the previous call's `cd DIR &&` under the session's
start folder 145 times of 192, and the rest match the shell's rules (a cd outside the
working folders, a background call or a failed command doesn't carry).

main-tree-hint covers Bash writes as well as the file tools, since those were most
of the measured writes: `sed -i`, `perl -i`, `>`/`>>` redirections, `tee`, and
apply-edits' `--- PATH` lines, each found from its step's directory; ignored files
and `.git/` get no note. It runs on every such call, with no `if`: `if` takes one
rule, and whether `Edit(...)` there matches Write and NotebookEdit, as it does in
permission rules, is undocumented.

lib/paperkit/git.py keeps its own copy of the main-tree test, since paperkit is
vendored into projects without lib/; test_maintree.py holds the two to one answer.
Issue #12 (destructive git after a bare `cd X;`) reuses the walk: each step names the
earlier cds in its scope whose failure wouldn't stop it, with the folder each would
leave the shell in.

### Destructive git after a loose cd, or in a main tree, is refused (#12, 2026-11-26)

On 2026-11-23 a review agent's `cd filedrawer; ...; d=<scratch>/r; cd $d; ...; git
reset -q --hard HEAD~1` met a folder that didn't exist: the cd failed, the shell stayed
in filedrawer's main tree, and the reset dropped main's newest commit, unnoticed for
two hours. On 2026-11-26, `cd WT && sleep 900 & by=$!; heavy -- bin/mutate-check ...`
sent the cd to the background with the sleep, mutate-check put a mutant into
llm-toolbox's main tree, whose bin/ every session runs, and it killed every process
on the VM. bin/wrong-tree-gate refuses both shapes for git before anything runs: a
destructive git command (`reset --hard`, `checkout -f`, `switch -f`, `clean -f`,
`restore` of the work tree, `stash drop`/`clear`, a `rebase` that starts one) after a
cd whose failure wouldn't stop it, and one in the main tree of a repo with an agent
check, judged by lib/maintree.py's walk as #4's commit refusal is. mutate-check,
which the first draft gated too, refuses a main tree itself since 07771c1.

- Scope of the main-tree rule: repos with an agent check, as #4's, rather than every
  repo under the workspace root the issue named. That leaves out what has no rule to
  break: the shallow clones in `cache/`, review scratch repos, and the workspace
  repos with no agent check (blog, research, dot-files and others), which #4's
  commit refusal leaves free too.
- A loose cd is one whose failure doesn't stop the command: it ends with `;` or a
  newline, or it starts an `&&` list that does, as in `cd X && git fetch; git reset
  --hard`, where a failed cd skips only the fetch. `cd X || exit` was never loose;
  `cd X || echo` isn't caught.
- It is judged by the folder the shell would stay in if it failed, and refused only
  when that folder isn't certain or is a main tree the rule below refuses. A scratch
  experiment that cds about under /tmp, or `cd ..; git reset --hard` from there,
  has no main tree to hit; the incident's shell stayed in filedrawer's. With several
  loose cds, each one's folder counts, back to the latest that names an absolute
  folder that is there when the hook runs.
- `set -e` makes a later cd safe only where bash heeds it. Bash ignores it in an if,
  while or until condition, in a non-final part of an `&&` or `||` list, and in every
  command inside a group, subshell or compound command sitting in one of those places
  (`set -e; { cd $d; git reset --hard; } || echo failed` resets wherever the shell
  was), so shellparse marks those commands `unchecked`. Nor does it count for a cd in
  a function's body, which may be called as `f || true`, or anywhere once a function
  sets or unsets it. The first version voided it everywhere once the command
  defined any function, and its refusal still said to put `set -e` first (second
  review, #12); where `set -e` wouldn't help, the refusal now says why instead.
  It is tracked per subshell, `set +e` undoing it.
- A cd that is the whole condition of an if, elif, while or until guards what runs
  only when it succeeded: `if cd $d; then ...`, and what follows `if ! cd $d; then
  exit 1; fi` or `until cd $d; do ...; done` (unless the loop breaks). It stays
  loose in the branch that runs on its failure and after an if whose failure branch
  doesn't exit. A cd inside a longer condition (`if cd $d && x; then`) is still
  judged as an `&&` list ending with `;`.
- A relative cd counts as a cd to a folder that is there when the folder, taken from
  where the shell stood, is there: the workspace's `cd ../<repo>-wt-<topic>` from a
  main tree. Unlike an absolute one, it doesn't settle where the shell is: if a cd
  before it failed, it is taken from elsewhere, so those earlier cds are still
  judged.
- When the step is chained to a cd that comes after the loose one, as in `cd "$S" &&
  git rebase main; cd "$T" && git rebase main`, the second rebase waits for `cd
  "$T"`; the risk is only that "$T" is relative and taken from wherever a failed
  `cd "$S"` left the shell. The refusal names `cd "$T"` and asks for an absolute
  folder, rather than naming `cd "$S"` and asking for the `&&` already there.
- A loose cd before `bash -c '...'` or `sh -c '...'` counts for the script, which
  starts where the shell is; one before a $(...) substitution counts for its body,
  which runs in the folder the shell is in there. The main-tree rule judges a
  substitution's body by that folder too, so `x=$(git reset --hard 2>&1)` in a main
  tree is refused. agent-commit-gate and main-tree-hint share the walk, so they now
  judge a substitution's body by that folder too: the commit gate refuses `echo
  $(git commit ...)` in a main tree.
- `git checkout -- PATH` and `git checkout .` overwrite the work tree as `git restore
  PATH` does, so they count as it does, passing mid-operation. Left out, as a
  question for the owner on #12: `checkout -B`, which moves a branch, and `reset
  HEAD~1`, `--keep` and `--merge`, which move one and keep or refuse local changes.
- The honest case, an agent resolving its own rebase in the main tree of a repo with
  no worktrees. Finishing one under way (`--continue`, `--abort`, `--skip`, `--quit`,
  `--edit-todo`) passes anywhere. The owner's call (2026-11-26, on #12): in a main tree
  mid-merge, mid-rebase, mid-cherry-pick or mid-revert, only that and a path's fix
  pass, `restore` and `checkout -f` naming paths (after `--`, or with `--ours`,
  `--theirs`, `-m` or `--conflict`); `reset --hard`, `clean -f`, `stash drop`/`clear`
  and a branch's `checkout -f` stay refused, with the opt-out prefix for what the
  user asked for. An unborn HEAD, a submodule and a bare repo still let all of it
  through, as #4's commit does.
- Cost: the gate runs on every Bash call. It exits before importing the parser when
  the command names none of the subcommands it judges: about 45 ms a call against
  90 ms (hyperfine, 20 runs each, 2026-11-26), and 28 against 45 ms on another run;
  the rest is the interpreter starting.

A dry run over the transcripts (2026-10-30 to 11-26, against the filesystem of 11-26;
keep/llm-toolbox/wrong-tree-gate/replay2.json) found 241 Bash calls with destructive
git, and the gate refused 101. 8 ran in a main tree, all from before agents moved to
worktrees (11-10) or undated. 93 followed a loose cd, 57 of them through the `&&`-list
rule. 53 of the 93 name an absolute worktree folder since removed: when they ran it
was there (only 4 results in all 93 show a cd failing, the 11-23 incident's among
them), so the gate would have passed them. The other 40 cd to a variable (`cd $S;
...`) or a relative folder in review scratch experiments started from a main-tree cwd,
the incident's shape: 21 of them since 11-10, under one a day. The first draft's run
(replay.json) counted mutate-check too, 1,245 calls and 237 refused. The one-shot
`AGENT_MAIN_TREE_OK=1` prefix is #4's, unnamed in the refusal.

After the second review (replay3.json, the same transcripts and 61 more calls that
`checkout` paths): of the 241, 99 are refused, the two fewer being a `set -e`
command that defines a function; 4 of the loose-cd refusals now name the chained
cd. Of the 61 checkouts of paths, 13 are refused, 5 of them in a main tree: agents
putting back a main-tree file they had copied to a worktree, and once, after the
11-26 crash, restoring the mutated bin/drive-agent there, which now takes the
user's word or the opt-out prefix.

### skill-patch save -C writes the patch into a worktree (#23, 2026-11-26)

Once #4 refused an agent's commit in a main tree, `skill-patch save` still wrote
`skill-patches/NAME.patch` into llm-toolbox's main tree, so every skill patch needed
the owner. The owner's answer: `save -C WORKTREE`, committed there and landed with
wt-ship. Without -C, save writes into the main tree as before, for the owner.

- -C refuses a folder that isn't a work tree of the tool's own repo (the same git
  common dir as `lib/thirdparty.py`'s REPO), since a patch anywhere else is never
  landed or applied. Any folder inside the worktree names it; the main tree passes
  too, as the default does.
- It refuses a worktree whose base is behind a landed change to that patch. The new
  patch layers its reason on the reasons the worktree's copy holds, which would drop
  the landed layer's, and the rebase at landing would then conflict. `git rebase
  BRANCH` there settles it before save, where nothing is lost. A worktree with no
  merge-base on the main tree's HEAD is refused too, rather than compared with
  nothing.
- An unlanded change to the patch in another worktree, or uncommitted in the main
  tree, gets a note naming each, and save goes on. The diff runs against the installed
  copy every session shares, so it takes in the other session's edit, if made there,
  without its reason. The worktrees can't be ordered from here: the second to land
  meets a rebase conflict and merges the reasons, and a main-tree change stops
  wt-ship on its own.
- `apply` takes no -C. It writes the installed copy every session shares, which should
  hold the landed patches; install.py runs it from the main tree after a landing, and
  a worktree's patch is already on the copy it was saved from.
- THIRD_PARTY_SKILLS is still read from the tool's own install.py, the list the
  installed skills came from.

## subagent-wait-gate

### A subagent blocks on its job; a SubagentStop hook refuses a stop to wait (2026-11-13)

The guidelines told every agent to end its turn while a background job ran and let the
notification resume it. Right for the main session, whose prompt cache lasts an hour.
In session 27f72ef3 subagents did the same: six cache rebuilds of 137k to 355k tokens
after waits of 6 to 22 minutes (a subagent's cache lasts five minutes), and about 25
turns of the parent's spent on the notifications of subagents that had stopped to
wait. The owner asked for the fix "both as guidance and enforced".

What the hook sees, from Claude Code 2.1.325 (the hooks reference, and a `claude -p`
run whose subagent ran `sleep 25` in the background and ended its turn):
SubagentStop carries `agent_id`, `agent_transcript_path` and `background_tasks`, the
running tasks of the whole session, each with `id`, `type` (`shell`), `status`
(`running`) and `command`; the main session's Stop has no `agent_id`. The subagent's
transcript holds each task it started in the Bash result's text ("Command running in
background with ID: ..."), so running jobs come from the event and ownership from the
transcript. An interactive session's transcript also records the id as
`toolUseResult.backgroundTaskId`, but the `claude -p` run's had no `toolUseResult`, so
the hook reads the text. A subagent's foreground Bash call is moved to the background
after 180 s (`subagentCapApplied`), so the wait it is given is `waitone -t 170`, not
the four minutes first proposed.

It can't trap a subagent: two refusals in a row with no tool call between them let
the stop through (a server left running on purpose), as does a job an hour old or
any error inside the hook. `stop_hook_active` can't be the cap, since it is set after
any hook-caused continuation, and Claude Code's own cap of eight blocks resets on each
tool call.

### Rejected: refusing `bgrun --wait` as a background call in a subagent (2026-11-13)

The background call isn't the waste; ending the turn is, and an agent may work while
the job runs. Nor can bgrun tell that a subagent started it: no documented variable
says so. heavy-gate, which sees `agent_id`, gives a subagent the blocking form in its
refusals instead.

## ci-status

### Failing CI on the default branch reaches the agent at session start (2026-11-10)

The owner pushes main and CI runs on GitHub, so no agent learned when it failed, and
sessions built and landed on a red main. The owner settled the shape: a SessionStart
hook adds a line per failing workflow, naming its failed jobs, the commit, the age
and the run's URL; silent when all pass, off GitHub, or on any error; a deadline of
a few seconds and a per-repo cache of minutes so sessions starting together don't
each call GitHub. No issue filing and no email. wt-ship prints the same lines before
it lands (a warning, never a refusal), through the same tool.

A review of the design, before code, settled the details: GitHub lists runs by
creation, and a re-run moves only `updated_at`, so the newest run per workflow is
the first listed (filedrawer's mutants run 44f037e, re-run at 14:23, listed below a
13:37 run); runs of a deleted or disabled workflow would stay red, so only active
workflows count; a run cancelled by a newer push is no verdict; job names are
written by whoever writes the workflow, so they are cut to plain characters before
they reach an agent's context; the default branch comes from the API, not the local
`origin/HEAD`, which can be stale, and is cached with the rest. The `gh` calls stay
in the tool's process group, so wt-ship's timeout stops them with it. Measured on
filedrawer-dev/filedrawer from the sandbox: the runs call takes about 1.8 s, a jobs
call 0.4 s; `gh auth status` says not logged in there, yet the calls work through
the proxy, so the tool tries the call rather than checking auth.

### A repo may ignore workflows by file name (2026-11-10)

Scheduled runs count, so a scheduled job that fails for reasons outside the code (a
nightly link check, a long mutation run) would read as "CI is failing on main". The
owner chose a per-repo opt-out, `git config --add ci-status.ignore FILE.yml`, matched
against the workflow's file name. It applies after the cache, so a change counts at
once, and `-v` names the failing workflows it left out.

## gh-ci-wait

### Wait for the runs to appear, then report what failed (2026-11-26)

A filedrawer session ran `gh pr checks --watch` right after `gh pr create`, as
GLOBAL_CLAUDE.md said to, and it exited 1, "no checks reported on the
'apt-config-isolate' branch", because GitHub had not queued the runs yet; it cost a
`gh run list` and a second watch. In the same week PR filedrawer-dev/filedrawer#172
failed CI on a test the branch didn't touch, a bug in its base, and nobody acted;
main broke on a push 13 h later. The owner chose one tool for both: wait for the
runs to appear (3 minutes, then exit 3), watch them with no overall deadline, since
CI can take hours and the caller runs it in the background, and print "verdicts and
failures": a line per run, each failed run's failed jobs and steps and its failed
tests' lines from the log, within about 20 lines, plus a line for each failing pytest
file the branch didn't change. Exit 0 all green, 1 a run failed, 3 no run in time.

A review of the brief settled the rest:

- The runs come from one `gh api repos/R/actions/runs?head_sha=SHA&per_page=100`
  a poll, not `gh run list`: that takes two calls, lists 20 runs by default, and
  gives an empty `workflowName` for a ruleset's workflow. GitHub matches only a
  full SHA (a short one returned `[]`), so a short one is resolved by git first.
  Digits alone are a PR number, unless git has a commit they abbreviate: about 1 in
  27 short SHAs is all digits.
- A PR counts only its pull_request runs and a commit only its push runs, each
  workflow's newest, and the workflows `ci-status.ignore` names stay out, as in
  ci-status, which shares that list's reader. Newest is per workflow path, since a
  ruleset's `org/.github/.github/workflows/ci.yml@…` and the repo's own `ci.yml`
  are two workflows; the report and the ignore list still name the file. A PR's
  head is read each poll, and the wait starts over when it moves.
- A workflow that starts late, as a path-filtered one can, joins: the wait ends
  only when every run is done and none has been new for 45 s.
- A run waiting for approval, or concluded action_required, would hang the wait,
  so it ends it with exit 5. Cancelled and stale fail, since nothing passed.
- gh failing is retried with backoff for 10 minutes before exit 4, and an answer of
  a shape it can't read counts as gh failing, so a blip in an hours-long wait
  doesn't lose it. Exit 2 is for usage alone, and for a PR number GitHub says it
  doesn't have on the first lookup, which no retry would fix. The jobs and log calls
  are best effort: a startup_failure has no log, and the run list alone sets the
  status.
- The job lines list failed jobs before cancelled ones: a fail-fast matrix cancels
  the failed job's siblings, which would otherwise fill the 5 lines. When the 20
  lines run out, test lines go before the unchanged-file notes, which are the
  report's reason for #172.
- Log text is written by whoever writes the PR, a fork's included, so after the
  matching it is cut to printable characters (colour codes, control, bidi and
  separator characters out), 200 a line; names go through ci-status's `plain`.
  GitHub's logs carry a BOM and colour codes written as a literal `^[`, both
  stripped before matching. nextest prints each FAIL line twice, so lines are
  kept once.
- The owner revised the note to `<path>: unchanged by this branch`, claiming
  nothing about the base: in #172's case the base held local commits origin's CI
  never ran, so GLOBAL_CLAUDE.md has the agent run the test on the base before
  saying the base fails it. No note when the commit is its own merge base, as on
  main, or git can't answer (no `origin/HEAD` in an older clone).
- `github_repo` and `plain` moved to `lib/github.py`, shared with ci-status.
  wt-ship's and issue-todo's copies of `github_repo` stay, out of this change.

A gap stays open: a re-run, or a reopened PR, whose older completed run is listed
before GitHub creates the new one can end the wait on the old verdict. Taking each
workflow's newest run and the settle window narrow it.

## path-refs

### rg runs in the workspace root (2026-11-09)

rg anchors a `--glob` that starts with `/`, such as `!/cache/`, at its own working
directory, not at the search path. With the workspace root as its working directory
and `.` as the path, the skips (`!/cache/`, `!/keep/`, `!*-wt-*/` and the rest) hold,
and rg walks about 10.8k files: a search takes about 1.3 s, under half a second warm.
Run from any other folder, with the absolute root as the path, the two anchored
globs match nothing, and rg walks the 1.17M files under `cache/` and `keep/`, past
60 s (measured by the agent that asked for the tool; the anchoring checked on a
two-folder tree). So the tool runs rg in the root, on `.`, and prints hits relative
to it. `test_skipped_folders_and_git_ignored_files_are_not_searched_from_any_cwd`
runs the tool from another folder, and fails when rg doesn't run in the root.

The symlink walk of `--links` can't use rg, which doesn't list links, and doesn't
skip git-ignored folders, where a link such as `data -> ../cache/X` is most likely to
live. It walks the rest of the workspace, git-ignored venvs included, in about 2 s
warm (13 s once, with the host mount's cache cold), as long as `find` takes over
the same folders.
## mutate-check

### The command writes no .pyc (2026-11-09)

Python takes a `.pyc` as current when the size and the mtime, in whole seconds,
that it recorded match its source's. A same-size mutation (`n * 2` to `n + 2`,
`==` to `!=`) that the command imported, put back within the second, left the
mutant's bytecode looking current: in 9 of 10 runs, a plain import afterwards
ran the mutant, until the next edit of that file. So the command runs with
`PYTHONDONTWRITEBYTECODE=1`, and no mutant's bytecode is written at all.

A Python started with `-E` or `-I` ignores the variable, and so does one the
user starts in the same tree while a mutant is in place. So the file put back
also gets an mtime at least a second past the mutant's: no `.pyc` written for
the mutant matches it, and cargo and make, which compare mtimes, still rebuild.
It can sit up to a second in the future. In `--diff` mode each Python mutant
also gets an mtime a second past the last one's, since two mutants of a file can
be the same size: with a command faster than a second, the file's mtime runs
about a second ahead of the clock per mutant.

Dropped: putting back the file's old mtime, which makes cargo and make take
their mutant build for current and skip the rebuild; and deleting the restored
file's `__pycache__` entries, which also removes the bytecode of the user's own
runs and misses a `PYTHONPYCACHEPREFIX`.

### --diff: operators picked from the reviews' survivors (2026-11-09)

`--diff` exists because, in one session, all three /build reviews found mutants of
the builder's diff that its tests let through, each costing a fix round. Its
operators are the fewest that flag those: deleting a statement (a `chmod` call, a
guard's `return`, an idempotence check's `raise`, a `finally` cleanup), negating a
condition, dropping one operand of an `and`/`or` (a reminder's condition), changing
a string that's a value (the `"keep"` of three default roots) and a shell default
(`${CACHE:-.../keep/...}`, which deleting the line doesn't test under `set -u`).
The rest, `==`/`!=` and `True`/`False`, are the usual cheap ones.

Dropping an operand replaced swapping `and` and `or`: the swap survives exactly
when both drops survive, so the drops flag every gap the swap does, and more.

Dropping a keyword argument was tried for the sixth survivor, `tar.extractall(x,
filter="data")` turned into `"fully_trusted"`, and dropped: on Python 3.14 the
default filter is `data`, so the dropped argument passed even the test that caught
`"fully_trusted"`, and `"XXdataXX"` raises. No generic operator flags that one.
Dropping `encoding="utf-8"` or a temp folder's `prefix=` only added survivors.

Strings with whitespace (messages), URLs, digests and the strings of a module's
data tables are left alone, and deleting a message call runs last. On sandbox-setup
3260129, the pinned tools table's digests, URLs and versions made 12 survivors no
useful test kills, and deleting `say(f"...")` calls took 5 of the first 55 runs.

The order puts each statement's first mutant before any statement's second. By
line, on that diff, the first 40 of 93 runs spent 15 on the pinned tools table and
never reached the guard, the idempotence check, the `finally` cleanup or the
reminder. By statement, with the data and messages rules, the diff has 78 mutants,
and the first 40 reach five of the six examples' lines; the reminder's dropped
operand is 62nd.

CMD runs once unmutated first: a command that fails would read every mutant as
caught. That, and a run that judges no mutant, exit 3, so neither passes for "none
survived". The owner then asked (2026-11-09) for the judge to read that run, not
just its status, and for a stop after the first 3 mutants when all 3 are
inconclusive: the first dogfood run, `--diff` over its own change with `pytest -x`
under this repo's `-n auto`, read 89 of 100 mutants as inconclusive in about 12
minutes of full-suite runs. Under pytest-xdist, `-x` ends the run by raising
`Interrupted`, which pytest reports as exit 2, its status for a collection error
too; so an exit of 2, 4 or 5 whose summary counts a failed test is now a catch, in
both modes. The rerun read 97 of 100 as caught. Each mutant's timeout is ten times the unmutated run, at least 30
seconds: the other mode's 600 would cost ten minutes per hanging mutant, such as a
deleted loop counter.

Left as the other mode has them, though a review asked: pytest exiting 2 after a
passing baseline (a mutant that breaks the import) stays inconclusive, not caught,
and a test pytest-timeout fails stays caught. Suggested, not built: one run per file
with an exit at its top, to name in one line a file the command never runs, instead
of a survivor for each of its mutants.

### /build no longer runs --diff; a daily CI run does (2026-11-10)

/build's step 2 had the builder run `mutate-check --diff` after every change to Python
or shell, under `heavy --long`, and leave the tree alone until it ended. The owner
dropped it: it blocks the builder for the run (8 minutes for 40 mutants in the
fold-check build), and its 4-core lane is half of this VM's 6 to 8 cores of real
compute, while the landing checks wait behind it. Each repo's `mutants.yml` workflow
now runs daily on the lines changed since the last run that judged mutants
(filedrawer keeps its full run started by hand too), on
GitHub's runners, and `/upskill` pitches triage of the runs no one has triaged
(`mutants-pending`). The cost, accepted by name: the --diff entry above exists because
reviews found survivors the builder hadn't, each a fix round, and those rounds can
come back, a day later. A builder or reviewer still runs `mutate-check` on one mutant
to show a test catches what it's meant to (/build step 2, /tdd).

### /build's --diff run, brought back on 2026-11-25, is dropped again (2026-11-26)

A session's /upskill pitched putting `mutate-check --diff` back in /build's step 2
(030df63), saying each mutant "reruns tests that take seconds". The owner approved
it. Nobody measured that claim, the pitch didn't name the 2026-11-10 decision above,
and this file wasn't updated. The day after, a study of the heavy record and the
session logs measured what it did:

- 32 runs in about 22 builds: median 8.2 minutes from launch to result, p90 27.5,
  max 39.6; one run judged 9 mutants in 57 minutes before it was stopped.
- Every job in the long lane that day was a mutate-check, holding the lane for
  about 63% of the VM's uptime on 0.4 to 1.0 of its 4 cores. Other jobs waited up to
  54 minutes behind them.
- 1663 mutants, 189 survivors; 39% of the distinct survivors were string values
  (help metavars, temp prefixes, defaults). About 7 builds got a useful test from a
  survivor; none found a production bug.
- Every gap that mattered, both of the rule's own examples included, came from a
  reviewer's targeted mutate-check.

The owner dropped it again: the 2026-11-10 entry above holds. A reviewer's targeted
run stays, and llm-toolbox's daily mutants run, which judged no mutant from
2026-11-14 to 2026-11-25, is to be fixed. The lesson the owner drew: a change sold on
how fast it is must say what was measured, and the speed must keep being measured,
with a warning once it gets much worse (2x or more), so a promise like "tests that
take seconds" can't turn into many-minute waits unseen.

### /build leaves the full suite to the commit hook or the landing (2026-11-26)

/build's step 2 said to run only the tests you touched "in a repo whose commit hook
runs the checks". sandbox-setup's hook runs no test at an agent's commit: its
`agent-commit-check` is `exit 0`, and wt-ship runs `agent-pre-commit`, the whole
suite, on the tip of each landing. A reviewer reported that hook's `passed (0s)` as
a suite that had not run. The orchestrating session took that for a gap and told
its subagents to run the whole suite by hand, seven times over five landings, each
of which ran it again.

The rule now names the repo by what it has, an `agent-pre-commit`, and says the
full checks run at the commit or at the landing. It also says a hook that passes
in 0 s is the rule at work, and that a subagent's brief carries the same rule.
Step 6 and step 2's "Done when" follow it.

### A daily run cut off at its time limit still moves the base (2026-11-10)

The brief had a run cover its range only when it judged every mutant. A review found
the base would then stall: with the whole suite per mutant (the owner's choice, kept
under a third of a private repo's free minutes) and mutate-check's fixed order, a
range bigger than one run's 15 minutes is cut off at the same mutants every day, and
never covered. So a run that judged any mutant covers its range, and its summary says
the mutants it left out are not retried; only a run that judged none (the suite fails
unmutated on the runner) keeps the base, and fails. Raising MUTANT_MINUTES, as a
public repo's free minutes would allow, leaves out fewer.

### The daily run gives each mutant's suite a PID namespace (2026-11-26)

Six daily runs, 11-19 to 11-24, all at `bf78929`, passed the unmutated check, then
died after 2h27m to 2h50m with "The hosted runner lost communication with the
server" and uploaded nothing. `bf78929` added drive-agent's `kill_tmux_server`, which
SIGKILLs a stuck tmux server and its children, found by their parent pid in /proc,
and `test_a_kill_server_that_never_returns_still_ends_the_server_and_says_so` calls
it for real. The mutant that negates the parent-pid check SIGKILLs every other
process the user owns, and on a hosted runner that user runs the runner's own agent.
In a PID namespace, with a `sleep` standing in for the agent, that mutant killed the
`sleep` and mutate-check itself, which left the mutant in the file. The first guess,
a mutant growing memory without bound, would have called for a ulimit, which
wouldn't have stopped it.

So mutants.yml runs each mutant's suite through a small wrapper, `own-pids`: `sudo
unshare --pid --mount-proc --kill-child`, then `setpriv` back to the runner's user
and groups. The drive-agent tests call `sudo`, which a user namespace would break.
`env -i` and the `export -p` taken before sudo give the suite the environment
mutate-check gave the wrapper, since `sudo --preserve-env` still drops
`PYTHONDONTWRITEBYTECODE` and the other `PYTHON*` and `LD_*` variables. bash is the
namespace's init, and reaps what is orphaned there: with `uv` as init, a zombie
outlived `test_a_timeout_stops_the_commands_children_too`'s wait. The init ignores a
SIGKILL sent from inside, so the killing mutant reads as caught, exit 137, and
`--kill-child` ends the namespace when mutate-check stops a run past its timeout.

mutate-check didn't do this itself, so a local `--diff` run over such a mutant would
have killed the user's own processes, Claude Code sessions included. The next entry
moved the wrapper into mutate-check.

### mutate-check runs every command through own-pids, and refuses without it (2026-11-26)

#15: /build's step 2 tells each build to run `mutate-check --diff`, and a local run
over `kill_tmux_server`'s line 249 SIGKILLs every process the user owns, every agent
session on the VM. So the wrapper became `bin/own-pids`, with tests, and mutate-check
puts it before every run of the command, in both modes, the unmutated run included.
mutate-check itself stays outside, so it can always put the file back. mutants.yml
lost its inline copy and gets the namespace from mutate-check.

Without a namespace it refuses, exit 2, unless `--unisolated`. A warning only when
the diff touches code that sends signals was dropped: no reading of a diff tells. The
mutant that kills everything negates `if fields[1:2] == [str(pid)]:`, which sends no
signal, and lib/procmatch.py's `matches` (#17) sends none at all; safekill, which
calls it, does. `own-pids --check` decides, before the command first runs: a probe in
a namespace has to see only itself and the init. A namespace that fails later, where
own-pids exits 125 with its `no PID namespace` line, stops the run with the file put
back, so no later run goes unisolated.

It runs a copy of own-pids made at its start: the daily run mutates own-pids too, and
a mutant of the wrapper would otherwise change the namespace of the runs judging it.
Inside, a command that won't start exits 127, which reads as a catch, so mutate-check
looks the command up first. A command with a `heavy` word is refused: lib/fifo.py
takes a holder whose pid isn't in /proc for ended, so heavy inside would drop other
sessions' queued jobs, and they would drop its; `heavy -- mutate-check` keeps it
outside. The word check misses a heavy run deeper, by `sh -c '...'`, `make test` or a
script, so own-pids sets OWN_PIDS=1 for the command and heavy refuses its shared
queue (HEAVY_DIR unset or /tmp/heavy, both resolved) when it sees it, exit 125 with a
line mutate-check stops on, exit 2. A heavy with a queue of its own, as
test_heavy.py's runs have, still works there, so the daily run can mutate heavy. The
word check stays, as the early and clearer error.

The first version stopped on the line wherever it appeared, whatever the status. A
caught mutant of heavy's refusal then read as stopped (second review of #15): pytest
prints a failed assertion's message, and test_heavy.py's asserts carry heavy's
stderr, so the refusal's text shows up in an ordinary failure's report. mutate-check
now stops only when the line starts a line of output and the status is 125 or no test
summary (pytest's, cargo's) says tests ran, which covers a refusal under `make`,
whose status make replaces; a caught mutant of the refusal still prints pytest's
`1 failed`. test_heavy.py's test of the refusal ran heavy on the live /tmp/heavy,
so a mutant that defeated it ran real heavy on the shared queue from inside a
namespace; HEAVY_SHARED_DIR now stands in for /tmp/heavy there, and heavy refuses
/tmp/heavy itself whatever the variable says.

It refuses a main tree, --unisolated or not. The namespace covers only the command:
other sessions run the main tree's files themselves, safekill from PATH importing its
lib/procmatch.py, drive-agent, the git hooks, so a mutant there of a kill-all check
reaches every session that runs it meanwhile. The crash of 2026-11-26 08:36 was
mutate-check applying the line-249 mutant to the main tree's live bin/drive-agent.
The test is lib/maintree.py's (#4): a folder whose git dir is the common one, less a
bare repo and a new one, whose HEAD is unborn and which has no refs at all: a `git
init` scratch repo has nothing for a session to run. An unborn HEAD alone doesn't
do, since an orphan branch (`git checkout --orphan`) in a repo with commits has one
too, and a submodule is a repo like any other, where a worktree can be added. A file
outside any repo is allowed, and mutants.yml runs in a worktree of its checkout.

Measured while building own-pids: unshare blocks SIGTERM and SIGINT while it waits,
and the init ignores them, so neither `--kill-child` nor sudo's relay acts on a stop
sent to own-pids alone. own-pids passes it to the command and, $OWN_PIDS_GRACE seconds
later, kills the init. A stop that comes before bash has started the command, in the
first 50 to 90 ms, found no one to pass it to, and the first version dropped it: a
`sleep 6` ran to its end (review of #15). It now looks again every 10 ms until the
command is there, and the grace starts once the signal reaches it. Started without a
terminal, the command shares own-pids' process group, so mutate-check's
group signal reaches it directly, and its SIGKILL kills the init; from a terminal,
sudo's use_pty gives it a session of its own. bash runs with
--posix, so it doesn't source the $BASH_ENV of the environment it restores. The
environment goes through a 0600 file the init's first process deletes, as JSON, not
`export -p`, since it is exec'd whole. Each run costs about 0.1 s.

### safekill's tests can signal only what they started (#17, 2026-11-26)

A mutant that drops `matches`' name test (lib/procmatch.py) matches every process but
the caller's ancestors, and safekill is the only tool on `matches` that signals
(safeps lists, waitone waits). Its tests then sent SIGTERM to everything in reach:
in a PID namespace, the `sleep` started beside them and pytest. A local run outside
one would reach every session on the machine.

So test_safekill.py runs safekill through `signal_guard_testing.py`, which replaces `os.kill`
and `os.killpg` with ones that refuse any target without the test's pid among its
parents, and exits 86 at the first refusal, before the signal is sent. safekill
signals in pid order, so a widened match stops at the first process outside the
test's tree, kernel threads and other sessions included, and the tests' status
checks fail. Checked with a stand-in mutant that adds one outside `sleep` to the
matches (the real one was never run): 15 of the 24 tests failed on exit 86, and the
`sleep` lived. Inside a namespace, where every process may be in the test's tree,
`test_no_match_signals_nothing` still fails on its exit status and bystander.

A fake /proc was the other way: procmatch would read it from a variable, which
production code then carries for the tests, and safekill would still signal the
real pids it lists.

## paper

### A web page's text comes through pandoc, with stand-ins for what it would lose (2026-11-13)

`paper` refused a paper published as a web page, such as
https://transformer-circuits.pub/2026/workspace/index.html, so an agent wrote an
HTML-to-text converter by hand. `text`, `outline` and `grep` now take any http(s)
URL that isn't arXiv's, doi.org's or a post's and doesn't end in `.pdf`: an HTML
answer goes through `pandoc --from=html --to=gfm-raw_html --wrap=none` and is cached
as Markdown in `web/`, so `outline` and `grep` work as they do for a post. The owner
chose a thin layer over pandoc, not a converter of our own.

Before pandoc runs, the text it would escape or drop is swapped for a numbered
stand-in in private-use characters, and put back afterwards: Distill's `<d-math>`
(pandoc would escape the TeX's backslashes and underscores), `<d-cite>`, the
brackets around a `<d-footnote>`'s text, an image (`[IMG PATH]`) and a heading's id
(`{#ID}`, which gfm has no form for). `raw_html` is off because with it pandoc
writes a figure, a div and a table with merged cells as HTML tags; without it,
their text. The page's own private-use characters are swapped for stand-ins first,
so one that looks like a stand-in is put back as it was, where it used to end the
run with an IndexError.

Before any of that, comments are removed, and then scripts, styles and inline
`<svg>`: in that order, since `<!-- <script> -->` once opened an element that ran to
the next `</script>` and took the text between with it. No base64 reaches the
cached text, which agents read whole: pandoc wrote an inline `<svg>` as a
`data:image/svg+xml;base64` image, so it goes, and an `<img>` whose `src` is a
`data:` URI is `[IMG inline]`. The bytes are decoded in the charset the
`Content-Type` header names, else the `<meta>` in the first 1024 bytes, else UTF-8
with U+FFFD for what doesn't decode; before, an ISO-8859-1 page lost every accented
letter.

A URL that names no PDF but serves one (`openreview.net/pdf?id=...`) is read as a
PDF, as before. A URL ending in `.pdf` that serves a page is still refused.

### A web page that may not be the paper is refused or shown, never reused silently (2026-11-13)

Any HTML answer was converted and cached, so `https://openreview.net/forum?id=...`
answering `<html>login</html>` was cached as a paper and every later read reused it
without a word. Nothing tells a login or error page from a paper for certain, so
there are three guards. A page whose Markdown has under 2,000 characters is refused
with exit 1 and nothing cached: a login page, an app shell or a landing page. A
`.json` beside the Markdown keeps the URL asked for, the URL redirects ended at,
the page's `<title>`, the fetch time and the character count, and the conversion
says on stderr when the final URL's host or path differs from the one asked for.
And every `text`, `outline` and `grep` of a page prints its title and fetch date on
stderr, so a wrong page reused from the cache shows each time. Markdown cached
without its `.json` is fetched again.

## web-shot

### --variants shoots four looks; --fragment was dropped as a duplicate (2026-11-13)

To preview an Artifact page, an agent pasted an HTML skeleton around its body
fragment five times and shot it by hand at each width and scheme. The request was
one option that wraps the file and shoots desktop and phone, light and dark. The
wrapping half already existed: since commit 57cbb15 (2026-11-07) web-shot loads a
local `.html` file with no doctype inside the skeleton the Artifact publisher adds.
So the planned `--fragment` was dropped as a duplicate of it, and only `--variants`
was added.

The phone run is 400 wide at `--size`'s height, so the two viewports differ only
in width. A run that fails ends the command, and the runs after it aren't made.

### view, --sizes, several pages and --montage compare first screens (2026-11-25)

To show an owner four versions of a page on two phone sizes, an agent ran web-shot
eight times and wrote three PIL scripts to crop each full-page shot to the
viewport, label it and paste the eight side by side; one failed on a font path.
A shot was always the full page, since that is what a reader of a report wants,
so nothing showed where a page's fold falls.

`view NAME` shoots what the viewport shows. It is an action of its own, not a flag
on `shot`, whose second word is already a selector that runs to the end of the
line. `--sizes` names its runs by size, where `--variants` keeps its fixed pair of
widths. Several pages get a run each, named for the page, because the comparison
that prompted this was between pages, not between sizes of one. `--montage` takes
no path: there is one montage for each shot name, saved beside the shots as every
other output is. Its labels use Pillow's built-in font, so it needs no font file.

### Each run removes default output folders idle for 72 hours (2026-11-21)

With no `--out`, web-shot writes to a new `web-shot-*` folder in the temp dir and
never removed one, since the agent reads the shots after the run. By 2026-11-21
/tmp held 434 of them, 658M, on a 20G sandbox disk that had reached 92%. Now each
run first removes the `web-shot-*` folders in the temp dir that nothing was
written to for 72 hours, counting the newest write to any file inside. The owner
chose 72 hours over 24, so a session can come back to its shots days later. A
box-wide /tmp reaper and a default inside the session's scratch folder were the
alternatives: the reaper would be a new gate that deletes files across tools, and
the scratch folder layout is Claude Code's, undocumented, and never cleaned either.
