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

The fingerprint came from an anki-cards retro (757b220): a subagent's `rebase --exec`
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
refactor base are the refactor's own, so a new commit made there still finishes
unreviewed: an accepted gap (the owner's call, 2026-11-09), since a rule on the
files those commits touch would also refuse honest refactors, such as a helper
moved to a new file. Rejected: a warning at finish, which comes after the next
commit's work is already written, the very mistake (the owner's call, 2026-11-09: "We
shouldn't change the essence of a feature to avoid breaking changes"). `start` marks
the builds it opens (`checks_steps`), and finish checks the steps only of a marked
build, so a build another session opened before this landed, whose review an older
`review-prompt` never recorded, finishes as before (the owner's call, 2026-11-09).

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
when there's none.

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
