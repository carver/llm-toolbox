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

### heavy-bench: a benchmark is many short --alone jobs, interleaved (2026-11-25)

In filedrawer #137's benchmark (2026-11-25), `heavy --alone -- hyperfine` over three variants of a 2-minute suite held heavy's queue for about 25 minutes, so every session's commit checks waited behind it; the owner asked for it split.
A job per run lets other jobs in between, and interleaving the variants round by round spreads a drift in the machine over all of them, where hyperfine's runs of one variant in a row would put it on the last.
The cost is heavy's quiet-cores wait before each run, seconds on a quiet machine.
heavy-bench reads each run's wall and outside CPU from heavy's `HEAVY_ALONE_REPORT`, so time in the queue doesn't count, and reruns a run taken under outside load once, keeping both rows.

### An --alone job reports the other work its cores did (2026-11-25)

`--alone` keeps other heavy jobs off a timing's cores, but not work outside the queue: agent-commit-gate's `checks-needed` runs before it asks for a place, and an agent can run a build bare.
In filedrawer #137's benchmark (2026-11-25), one of nine `bin/tests` runs took 179 s where the same setup's others took 102 and 114, and nothing recorded what else ran then; the owner had to point out an agent working outside heavy.
So each `--alone` job ends with a line giving the CPU-seconds other work used on its cores, from /proc/stat less the job's own CPU time, and a warning past `HEAVY_ALONE_OUTSIDE` cores on average (0.5).
A process CMD started and left running counts as other work, since getrusage sees only reaped children.
sccache's server is the exception made: it runs a cargo job's compiles without being its child, so its CPU time during the job counts as the job's, and the line says how much; the server is the VM's, so another session's compiles then hide inside that figure, the lesser error for a timing of a build.
`HEAVY_ALONE_REPORT` writes the numbers to a file, for a tool that times many runs.

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
  warning only.
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
The agent sees a declined dialog, not answers, which a usability finding should allow for.
A session reads only its own transcript: before its first prompt the newest one is the last session's, and the simulated human answered that session's dialog again after a restart (jason86).
Codex has no such dialog in default mode: `request_user_input` is for Plan mode, and `request_user_input_async` returns at once and shows its question as an agent message.

### A person's step mid-run is a file the run waits for; a token outlives the run only through --keep-file (2026-11-26)

A scenario turn `{"wait_for_file": "NAME", "say": "..."}` prints one line naming `<--out>/NAME`, waits for that file past no turn limit (`DRIVE_AGENT_WAIT_LIMIT`, a day by default), then sends `say` as an ordinary turn; turns.md logs the wait.
`--keep-file REL:DEST` copies `~/REL` to DEST, mode 600 and unredacted, in the run's cleanup, before the home goes, on every exit, and prints only that it kept the file or why not; `files/` keeps its redacted copy.
Reason: filedrawer#141's no-account scenario (2026-11-24) had the agent make an account and ask for a claim, which the owner confirmed in a browser mid-run, and then meant to check the account with the agent's token.
`run` types every turn in one loop with no hook for its caller between turns, so the pause has to be a turn of its own; and the home goes on every exit, so the token, which the runner never had, could leave only through drive-agent.
A file in the run folder works from any shell or another agent, and holds the run with no terminal attached.
DEST must not exist and is opened with `O_EXCL | O_NOFOLLOW`, so a run never overwrites a file or follows a link someone planted there.
`~/REL` is read as the box's user but outside bwrap, where a link the agent planted would reach a file the box hides from it, such as `/etc/sandbox-persistent.sh`, and the caller would then send that file on (filedrawer sends the kept header to its site). So the read opens one folder at a time with `O_NOFOLLOW` and takes only a regular file; a link anywhere on the way keeps nothing and says so.
A wait turn's NAME is a bare file name, none drive-agent writes in the run folder (`turns.md` would end the wait at once) and no earlier wait turn's (the file is already there); print mode, which sends one turn, refuses one. turns.md logs the wait as it starts, so a run stopped mid-wait shows what it waited for.

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

mutate-check doesn't do this itself, so a local `--diff` run over such a mutant would
kill the user's own processes, Claude Code sessions included.

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
