export const meta = {
  name: 'overnight',
  description: 'Work an overnight queue: implement, review, then land or draft each issue, 3 or 4 at a time',
  whenToUse: 'After /overnight setup has written queue.md and protocol.md, in place of the loop',
  phases: [
    { title: 'Gate', detail: 'one shared heavy waits --until-open before build work' },
    { title: 'Questions', detail: 'question and findings items, 3 to 5 to an agent' },
    { title: 'Implement', detail: 'implementer.md up to READY FOR REVIEW' },
    { title: 'Review', detail: 'a fresh reviewer per issue' },
    { title: 'Finish', detail: 'a fresh fixer, or a lander for a clean review' },
    { title: 'Redo', detail: 'an issue whose agent died, redone as question-only' },
  ],
}

// args: {
//   run_folder: '<keep/<project>/overnight-<date>>',  // protocol.md, implementer.md, reviewer.md, log.md
//   repo: '<main tree path>',
//   items: [{issue: 41, end_state_hint: 'landed' | 'draft' | 'question' | 'findings',
//            triage_note: '<the triage line>', files: ['path', ...],
//            goes_live: '<link from the main tree | install.py rerun and restart | push>'}, ...],   // in queue.md's order
//   max_in_flight: 3,      // optional, 1 to 4, default 3
// }
const RUN = args.run_folder
const REPO = args.repo
const ITEMS = args.items
const MAX = Math.min(4, Math.max(1, args.max_in_flight || 3))
const QUEUE_FREE = new Set(['question', 'findings'])
const GATE_ROUNDS = 8 // busy gate runs, up to 25 min each, before an item is deferred
const SPAWNS_PER_OPEN = 3 // spawns an open gate run admits, among the workers waiting on it
const DEADLINE_MIN = 90 // a stage agent stops and gives up past this

const GATE = {
  type: 'object',
  properties: {
    exit: { type: 'integer', description: "heavy's exit status, from the log's last line" },
    reading: { type: 'string', description: 'the last line heavy printed before that' },
  },
  required: ['exit'],
}

const IMPLEMENTED = {
  type: 'object',
  properties: {
    status: {
      type: 'string',
      enum: ['ready_for_review', 'landed', 'draft_question', 'question', 'findings', 'found_existing', 'gave_up', 'claim_refused'],
      description: 'ready_for_review when you stopped at step 4; found_existing when an earlier agent left a worktree for it; otherwise the end state you finished in',
    },
    intended_end_state: {
      type: 'string',
      enum: ['landed', 'draft_question', 'none'],
      description: 'for ready_for_review, the end state you mean it to reach; else none',
    },
    worktree: { type: 'string', description: 'absolute path, or empty' },
    base: { type: 'string', description: 'build-state base sha, or empty' },
    review_prompt: { type: 'string', description: 'the exact text build-state review-prompt printed, or empty' },
    built: { type: 'string' },
    chose: { type: 'string', description: 'the choices you made that the reviewer should judge' },
    probe: { type: 'string', description: 'what the reviewer should probe' },
    sha: { type: 'string', description: 'landed sha, or empty' },
    pr: { type: 'string', description: 'draft PR URL, or empty' },
    rerun: { type: 'string', description: 'any server rerun, timing (its exact command) or other step the owner must take, or empty' },
    note: { type: 'string', description: 'anything surprising, in at most 3 lines; for gave_up, where you stopped' },
  },
  required: ['status', 'intended_end_state', 'worktree', 'base', 'review_prompt', 'built', 'chose', 'probe', 'sha', 'pr', 'rerun', 'note'],
}

const REVIEWED = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          where: { type: 'string', description: 'file:line' },
          defect: { type: 'string' },
          scenario: { type: 'string', description: 'a concrete failure' },
          severity: { type: 'string', enum: ['important', 'nit'] },
        },
        required: ['where', 'defect', 'scenario', 'severity'],
      },
    },
    changes_promise: { type: 'boolean', description: 'a finding changes what the owner or users are promised' },
    deletes_data: { type: 'boolean', description: 'the change deletes or moves stored data' },
    contradicts_issue: { type: 'boolean', description: "the change contradicts the issue's text" },
    sensitive_area: { type: 'boolean', description: 'it touches an area protocol.md says ends as a draft' },
    shared_tool: { type: 'boolean', description: 'the diff changes a commit or git gate, a hook, or a tool every session runs from PATH' },
    recommend: { type: 'string', enum: ['land', 'draft_question'] },
    sound: { type: 'string', description: 'what you checked and found sound, briefly' },
    gave_up: { type: 'boolean', description: 'true when you stopped at the deadline' },
    stopped_at: { type: 'string', description: 'for gave_up, where you stopped' },
  },
  required: ['findings', 'changes_promise', 'deletes_data', 'contradicts_issue', 'sensitive_area', 'shared_tool', 'recommend', 'sound', 'gave_up'],
}

const END_STATES = ['landed', 'draft_question', 'question', 'findings', 'found_existing', 'gave_up', 'claim_refused']
const OUTCOME = {
  type: 'object',
  properties: {
    issue: { type: 'integer' },
    end_state: { type: 'string', enum: END_STATES },
    sha: { type: 'string' },
    pr: { type: 'string' },
    rerun: { type: 'string' },
    note: { type: 'string', description: 'at most 3 lines; for gave_up, where you stopped' },
  },
  required: ['issue', 'end_state', 'sha', 'pr', 'rerun', 'note'],
}
// A fixer told to stop for a second review returns ready_for_review with these.
const FIXED = {
  type: 'object',
  properties: {
    ...OUTCOME.properties,
    end_state: { type: 'string', enum: ['ready_for_review', ...END_STATES] },
    fix_base: { type: 'string', description: 'for ready_for_review, the head sha before your fixups; else empty' },
  },
  required: [...OUTCOME.required, 'fix_base'],
}
const OUTCOMES = {
  type: 'object',
  properties: { results: { type: 'array', items: OUTCOME } },
  required: ['results'],
}

// Every result the script returns is built here, so each has the same fields.
function result(issue, end_state, fields = {}) {
  return { issue, end_state, sha: fields.sha || '', pr: fields.pr || '', rerun: fields.rerun || '', note: fields.note || '', ...fields.extra }
}

const isBuild = (item) => !QUEUE_FREE.has(item.end_state_hint)

// Every agent() the script starts, so it returns only once none is in flight.
const started = new Set()
function spawn(prompt, opts) {
  const p = agent(prompt, opts)
  started.add(p)
  return p
}

// What an earlier agent may have done already: a redo, or a rerun of the whole
// workflow, must report it, not do it again.
function alreadyDone(issues, worktreeToo) {
  const n = issues.map((i) => `#${i}`).join(', ')
  return `Before any work on ${n}, check what an earlier agent already did, and report that instead of redoing it:
- a commit on the main tree's branch whose message names or closes the issue (\`git -C ${REPO} log --oneline -20 --grep '#<N>'\`): end state landed, with its sha;
- an open draft PR for it (\`gh pr list --state open --search '<N>'\`, reading each hit's body): draft_question, with its URL;
- a line for it in ${RUN}/log.md, or a question or findings comment tonight on the issue: the end state of the last line for the issue. A \`ready_for_review\` line is the implementer's ${worktreeToo ? 'and means found_existing, naming its worktree and rerun step' : 'handing it on to you: no reason to stop'}.${worktreeToo
    ? `\n- a worktree of ${REPO} for it (\`git -C ${REPO} worktree list\`, by topic, or by commits naming #<N>): found_existing, naming the path.`
    : ''}`
}

// Where an issue's background jobs keep their logs and PID files, as implementer.md
// says, so a redo can find what a dead agent left running.
const jobs = (issue) => `${RUN}/jobs/issue-${issue}`

// A stage agent's own clock: the script has none, and can't stop an agent.
function deadline(issues, stage) {
  const n = issues.map((i) => `#${i}`).join(', ')
  return `Background jobs: start each as \`mkdir -p ${jobs('<N>')} && bgrun -o ${jobs('<N>')}/<name>.log -- CMD... > ${jobs('<N>')}/<name>.pid && echo <program> >> ${jobs('<N>')}/<name>.pid\`, where <program> is CMD's program name (\`git\`, \`wt-ship\`, \`heavy\`); the file's first line is the PID to wait on. A later agent reads these files to settle any job you leave running.
Deadline: run \`date +%s\` as your first command and keep the number; run it again between steps. Once ${DEADLINE_MIN} minutes have passed, stop where you are, even mid-step. The one exception is a landing under way (wt-ship, or a job it waits on): stopping it midway leaves two outcomes, so wait for it to end and report what it did. Then, for each of ${n} not yet finished: release its claim (\`issue-todo claims\` names the claimant; \`issue-todo release <N> --as <claimant> --note "overnight ${stage} passed its ${DEADLINE_MIN}-minute deadline"\`), append \`#<N> | gave_up | | | ${stage} deadline: <where you stopped>\` to ${RUN}/log.md, and return gave_up, saying where you stopped and naming any worktree.`
}

// What a redo does before anything else: a dead agent's jobs may still be running.
// bgrun exits once it has started a job, so no live command line names the log: the
// PID files are the trail, and a PID counts only while its command line still names
// that program (an interpreter's script, as `uv run --script` or python3 runs it,
// included), since the kernel reuses PIDs.
function settleJobs(issues) {
  const files = issues.map((i) => `${jobs(i)}/*.pid`).join(' ')
  return `First, settle the jobs the lost agent left. List them:
\`for f in ${files}; do [ -e "$f" ] || continue; pid=$(sed -n 1p "$f"); prog=$(sed -n 2p "$f"); if tr '\\0' '\\n' 2>/dev/null < "/proc/$pid/cmdline" | sed 's#.*/##' | grep -xF -- "$prog" > /dev/null; then echo "live $pid $prog $f"; else echo "gone $pid $f"; fi; done\`
A \`gone\` job has ended, or its PID now belongs to another process: leave it alone. Wait for a live \`wt-ship\` with \`waitone --pid PID -t 170\`, again after each exit 4, and read its log (the .pid file's name with .log) for what it did; \`kill PID\` every other live one. Only then check what was already done.`
}

// The shared gate. Workers waiting at once await one gate agent. A run that ends open
// admits SPAWNS_PER_OPEN of the workers waiting on it, and its admissions end with
// that round: a later spawn, an item's review or finish included, reads the gate again.
// heavy exits 0 open and 3 held; any other status (a usage error, say, from a heavy
// without --until-open), or a gate agent lost twice in a row, is a broken gate, not a
// busy queue: it sets gateError, and no build work is admitted for the rest of the run.
// A gate agent lost once is retried: the next reading starts a new gate run.
let gateRun = null
let gateCount = 0
let lastBusy = false
let gateError = null
let gatesLost = 0 // gate agents lost in a row

function readGate() {
  if (!gateRun) {
    const k = ++gateCount
    const out = `${RUN}/gate/gate-${k}.log`
    const prompt = `You are the spawn gate of an overnight run. Do only this, then return.
1. \`mkdir -p ${RUN}/gate && bgrun -o ${out} -- sh -c 'heavy waits --until-open --lane normal --p90 180 --waiting 1 --max-wait 1500; echo "exit $?"'\` (\`heavy -h\`). heavy reads the shared queue every 30 s, prints its first and last reading, and exits 0 once the queue opens, or 3 after 25 minutes still held; the \`exit N\` line after it records that status.
2. \`waitone --pid PID -t 170\`, again after each exit 4, until it exits.
3. Read ${out}. Append each line heavy printed (one or two, each starting \`open:\` or \`held:\`) to ${RUN}/waits.md, prefixed \`gate ${k} | \`. If its last line is anything other than \`exit 0\` or \`exit 3\`, append instead \`gate ${k} | error: exit N: <heavy's last line before it>\`.
4. Return exit as the N of the \`exit N\` line, and the line heavy printed before it as reading. Do not retry or work around a failure: the script handles it.`
    gateRun = spawn(prompt, { label: `gate ${k}`, phase: 'Gate', schema: GATE, effort: 'low' }).then((g) => {
      gateRun = null
      const exit = g ? g.exit : null
      const reading = (g && g.reading) || ''
      gatesLost = g ? 0 : gatesLost + 1
      if (gatesLost === 1) log(`gate ${k}: gate agent lost; reading the gate again`)
      else if (exit !== 0 && exit !== 3 && !gateError) {
        gateError = g ? `gate ${k}: heavy exited ${exit}: ${reading || 'no output'}` : `gate ${k}: gate agent lost twice in a row`
        log(`gate broken, admitting no more build work this run: ${gateError}`)
      }
      lastBusy = exit !== 0
      return { exit, reading, round: { admits: exit === 0 ? SPAWNS_PER_OPEN : 0 } }
    })
  }
  return gateRun
}

// 'open' once a gate round admits this build spawn; 'busy' after GATE_ROUNDS busy
// rounds; 'error' once the gate is broken.
// A worker an open round has no admission left for waits on the next round.
async function admit(item, why) {
  for (let busy = 0; busy < GATE_ROUNDS;) {
    if (gateError) return 'error'
    const g = await readGate()
    if (gateError) return 'error'
    if (g.round.admits > 0) {
      g.round.admits--
      return 'open'
    }
    if (g.exit === 3) {
      busy++
      log(`#${item.issue} ${why}: queue busy (${g.reading}), ${busy}/${GATE_ROUNDS}`)
    }
  }
  log(`#${item.issue} deferred before ${why}: the queue stayed busy`)
  return 'busy'
}

// An issue whose agent died (agent() gave null), or that a batch left out of its results.
async function redoAsQuestion(items, where, tag) {
  const issues = items.map((it) => it.issue)
  log(`${issues.map((i) => `#${i}`).join(', ')}: agent lost during ${where}; redoing as question-only`)
  const notes = items.map((it) => `- #${it.issue}: ${it.triage_note}`).join('\n')
  const prompt = `Overnight agents working ${issues.map((i) => `#${i}`).join(', ')} in the repo whose main tree is ${REPO} died during ${where}. Trust nothing they fetched or concluded.
${settleJobs(issues)}
${alreadyDone(issues, true)}
For each issue not already done:
1. Release the lost agent's claim, if one is held: \`issue-todo claims\` names the claimant; \`issue-todo release <N> --as <claimant> --note "overnight agent lost; redone as a question"\`.
2. Append \`#<N> | lost | | | during ${where}\` to ${RUN}/log.md.
3. Follow ${RUN}/implementer.md for it as question-only: claim, post the question, wrap up. Leave any worktree the lost agent made in place and name it in your note.
${deadline(issues, 'redo')}
Triage notes:
${notes}
Return one result per issue.`
  const r = await spawn(prompt, { label: `redo ${tag}`, phase: 'Redo', schema: OUTCOMES })
  const got = new Map(((r && r.results) || []).map((o) => [o.issue, o]))
  return items.map((it) => {
    const o = got.get(it.issue)
    const extra = { redone: where }
    return o ? result(it.issue, o.end_state, { ...o, extra }) : result(it.issue, 'gave_up', { note: 'the question-only redo was lost too', extra })
  })
}

// Question and findings items, several to one agent. They queue no jobs, so no gate.
async function runBatch(items) {
  const issues = items.map((it) => it.issue)
  const notes = items.map((it) => `- #${it.issue} (${it.end_state_hint}): ${it.triage_note}`).join('\n')
  const prompt = `Follow ${RUN}/implementer.md for each of these issues in turn, in the repo whose main tree is ${REPO}. Each ends as a question or findings, as its triage note says; write no code.
${notes}
${alreadyDone(issues, false)}
${deadline(issues, 'questions')}
Return one result per issue.`
  const r = await spawn(prompt, { label: `questions ${issues.map((i) => `#${i}`).join(' ')}`, phase: 'Questions', schema: OUTCOMES })
  const got = new Map(((r && r.results) || []).map((o) => [o.issue, o]))
  const missing = items.filter((it) => !got.has(it.issue))
  const redone = missing.length ? await redoAsQuestion(missing, 'questions', `batch-${missing.map((it) => it.issue).join('-')}`) : []
  return [...items.filter((it) => got.has(it.issue)).map((it) => result(it.issue, got.get(it.issue).end_state, got.get(it.issue))), ...redone]
}

function implementPrompt(item, avoid) {
  return `Follow ${RUN}/implementer.md for issue #${item.issue}, in the repo whose main tree is ${REPO}.
Triage note: ${item.triage_note}
Triage's end state: ${item.end_state_hint}. Files it expects: ${item.files.join(', ') || 'unknown'}.
Issues in flight, whose files to stay out of: ${avoid || 'none'}.
How the change goes live: ${item.goes_live || "not given: read it from the repo's install docs"}.
${alreadyDone([item.issue], true)}
${deadline([item.issue], 'implement')}
For a change that needs review, stop at step 4: append \`#${item.issue} | ready_for_review | | <rerun step, or none> | <worktree>\` to ${RUN}/log.md, so the rerun step survives a lost run, and return status ready_for_review with the worktree, base and the exact review-prompt text. A trivial change finishes through step 7. Your structured result is your report.`
}

function reviewPrompt(item, impl) {
  return `Follow ${RUN}/reviewer.md for issue #${item.issue}.
Worktree: ${impl.worktree}. Base: ${impl.base}.
Review-prompt pointer: ${impl.review_prompt}
The implementer built: ${impl.built}
It chose: ${impl.chose}
Probe: ${impl.probe}
Also answer: should this land, or become a draft + question for the owner? It is meant to end as ${impl.intended_end_state}. Set the booleans from what you found, and sensitive_area from the areas ${RUN}/protocol.md lists. Your structured result is your report.
${deadline([item.issue], 'review')} For you, returning gave_up means gave_up true, with stopped_at.`
}

const findingLines = (findings) => findings.map((f) => `- [${f.severity}] ${f.where}: ${f.defect} (${f.scenario})`).join('\n')

function fixPrompt(item, impl, review, endState, second) {
  return `Follow ${RUN}/implementer.md as the fixer for issue #${item.issue}, in the repo whose main tree is ${REPO}.
Worktree: ${impl.worktree}. Base: ${impl.base}. End state: ${endState}.${second ? `
Second review: the change touches a commit or git gate, a hook, or a tool every session runs from PATH. Stop after your fixups and return end_state ready_for_review with fix_base; a lander finishes it after a fresh review.` : ''}
The implementer's report: built ${impl.built}; chose ${impl.chose}; rerun ${impl.rerun || 'none'}; note ${impl.note || 'none'}.
${alreadyDone([item.issue], false)}
${deadline([item.issue], 'fix')}
Findings to fix:
${findingLines(review.findings)}
Return the end state you reached as your structured result, with issue ${item.issue}.`
}

function secondReviewPrompt(item, impl, fix, review) {
  return `Follow ${RUN}/reviewer.md for issue #${item.issue}, as its second review.
Worktree: ${impl.worktree}. Base: ${fix.fix_base}, the head before the fixer's fixups; review ${fix.fix_base}..HEAD alone.
The fixups are meant to fix these findings:
${findingLines(review.findings.filter((f) => f.severity === 'important'))}
Set the booleans from what you found. Your structured result is your report.
${deadline([item.issue], 'second review')} For you, returning gave_up means gave_up true, with stopped_at.`
}

function landPrompt(item, impl, endState, reviewed = 'its review found nothing to fix') {
  return `Finish issue #${item.issue} of an overnight run; ${reviewed}. Repo main tree ${REPO}, worktree ${impl.worktree}, base ${impl.base}, end state ${endState}.
${alreadyDone([item.issue], false)}
${deadline([item.issue], 'land')}
Otherwise do only steps 5 and 6 of ${RUN}/implementer.md: ${endState === 'landed' ? 'land it with wt-ship' : 'open the draft PR and post the question'}, then tick, release the claim and append the log.md line. Return the end state you reached, with issue ${item.issue}, the sha or PR, and rerun: ${impl.rerun || 'none'}.`
}

// Decide per SKILL.md's loop step 4: a finding that changes a promise, deletes data or
// contradicts the issue, or a sensitive area, turns a landing into a draft + question.
function decide(impl, review) {
  if (impl.intended_end_state === 'draft_question') return 'draft_question'
  const flagged = review.changes_promise || review.deletes_data || review.contradicts_issue || review.sensitive_area
  return flagged || review.recommend === 'draft_question' ? 'draft_question' : 'landed'
}

// A stage whose agent died: the issue goes question-only.
const redoStage = (item, stage) => redoAsQuestion([item], stage, `${item.issue}-${stage}`)

async function runBuild(item, avoid) {
  const n = item.issue
  // An item the gate doesn't admit: deferred on a busy queue, gave_up on a broken gate.
  const refused = (gate, before, fields = {}) => gate === 'busy'
    ? [result(n, 'deferred', { ...fields, note: `queue busy before ${before}` })]
    : [result(n, 'gave_up', { ...fields, note: `gate broken before ${before}: ${gateError}` })]
  let gate = await admit(item, 'implement')
  if (gate !== 'open') return refused(gate, 'implement')
  const impl = await spawn(implementPrompt(item, avoid), { label: `implement #${n}`, phase: 'Implement', schema: IMPLEMENTED })
  if (!impl) return redoStage(item, 'implement')
  if (impl.status !== 'ready_for_review') {
    const where = impl.status === 'found_existing' || impl.status === 'gave_up'
    return [result(n, impl.status, { sha: impl.sha, pr: impl.pr, rerun: impl.rerun, note: where && impl.worktree ? `${impl.worktree}: ${impl.note}` : impl.note })]
  }
  // What a result keeps once the implementer has left a worktree.
  const midway = { rerun: impl.rerun, extra: { worktree: impl.worktree } }
  gate = await admit(item, 'review')
  if (gate !== 'open') return refused(gate, 'review', midway)
  const review = await spawn(reviewPrompt(item, impl), { label: `review #${n}`, phase: 'Review', schema: REVIEWED })
  if (!review) return redoStage(item, 'review')
  if (review.gave_up) return [result(n, 'gave_up', { ...midway, note: `review deadline: ${review.stopped_at || 'no detail'}` })]
  const endState = decide(impl, review)
  if (endState !== impl.intended_end_state) log(`#${n}: review turns ${impl.intended_end_state} into ${endState}`)
  const important = review.findings.filter((f) => f.severity === 'important')
  const nits = review.findings.filter((f) => f.severity === 'nit').map((f) => `${f.where}: ${f.defect}`)
  gate = await admit(item, 'finish')
  if (gate !== 'open') return refused(gate, 'finish', midway)
  // A clean review, or nits only, goes to a low-effort lander; important findings to a
  // fixer, which stops for a second review when the change is in a tool every session runs.
  const second = important.length > 0 && review.shared_tool
  const finisher = important.length === 0
    ? { prompt: landPrompt(item, impl, endState), label: `land #${n}`, effort: 'low' }
    : { prompt: fixPrompt(item, impl, review, endState, second), label: `fix #${n}` }
  let done = await spawn(finisher.prompt, { label: finisher.label, phase: 'Finish', schema: second ? FIXED : OUTCOME, ...(finisher.effort ? { effort: finisher.effort } : {}) })
  if (!done) return redoStage(item, 'finish')
  if (second && done.end_state === 'ready_for_review') {
    gate = await admit(item, 'second review')
    if (gate !== 'open') return refused(gate, 'second review', midway)
    const again = await spawn(secondReviewPrompt(item, impl, done, review), { label: `review fix #${n}`, phase: 'Review', schema: REVIEWED })
    if (!again) return redoStage(item, 'second review')
    const bounced = again.findings.filter((f) => f.severity === 'important')
    if (again.gave_up || bounced.length) {
      const why = again.gave_up ? `second review deadline: ${again.stopped_at || 'no detail'}` : `second review bounced: ${bounced.map((f) => `${f.where}: ${f.defect}`).join('; ')}`
      return [result(n, 'gave_up', { ...midway, note: why })]
    }
    gate = await admit(item, 'finish')
    if (gate !== 'open') return refused(gate, 'finish', midway)
    done = await spawn(landPrompt(item, impl, endState, "a fixer's fixups passed their second review"), { label: `land #${n}`, phase: 'Finish', schema: OUTCOME, effort: 'low' })
    if (!done) return redoStage(item, 'finish')
  }
  return [result(n, done.end_state, {
    sha: done.sha, pr: done.pr, rerun: done.rerun || impl.rerun, note: done.note,
    extra: { decided: endState, findings: review.findings.length, nits },
  })]
}

// Units of work: each build item alone, and the question and findings items in
// batches of 3 to 5 (fewer only when fewer remain), in queue order.
function batches(items) {
  const k = Math.ceil(items.length / 5)
  const out = []
  for (let b = 0, at = 0; b < k; b++) {
    const size = Math.ceil((items.length - at) / (k - b))
    out.push(items.slice(at, at + size))
    at += size
  }
  return out
}
const indexed = ITEMS.map((item, i) => ({ item, i }))
const pending = [
  ...indexed.filter((x) => isBuild(x.item)).map((x) => ({ kind: 'build', items: [x.item], i: x.i })),
  ...batches(indexed.filter((x) => !isBuild(x.item))).map((b) => ({ kind: 'batch', items: b.map((x) => x.item), i: b[0].i })),
].sort((a, b) => a.i - b.i)
const filesOf = (u) => u.items.flatMap((it) => it.files)

// A pool of MAX workers. Each takes the waiting unit whose files overlap least with
// the units in flight, earliest in the queue on a tie; while the last gate run ended
// busy, it takes a question batch first.
const inFlight = new Map()
const results = []

const overlap = (u) => {
  let n = 0
  for (const other of inFlight.values()) n += filesOf(u).filter((f) => filesOf(other).includes(f)).length
  return n
}

function take() {
  const batchFirst = lastBusy && pending.some((u) => u.kind === 'batch')
  let best = -1
  for (let k = 0; k < pending.length; k++) {
    if (batchFirst && pending[k].kind !== 'batch') continue
    if (best < 0 || overlap(pending[k]) < overlap(pending[best])) best = k
  }
  return best < 0 ? null : pending.splice(best, 1)[0]
}

async function worker(w) {
  for (let unit = take(); unit; unit = take()) {
    const avoid = [...inFlight.values()].map((u) => u.items.map((it) => `#${it.issue} (${it.files.join(', ')})`).join('; ')).join('; ')
    inFlight.set(unit.i, unit)
    log(`worker ${w}: ${unit.items.map((it) => `#${it.issue}`).join(' ')} (${unit.kind})`)
    try {
      results.push(...(unit.kind === 'batch' ? await runBatch(unit.items) : await runBuild(unit.items[0], avoid)))
    } catch (e) {
      results.push(...unit.items.map((it) => result(it.issue, 'gave_up', { note: `script error: ${e}` })))
    }
    inFlight.delete(unit.i)
  }
}

phase('Implement')
log(`${ITEMS.length} issues in ${pending.length} units, ${MAX} in flight`)
await Promise.all(Array.from({ length: MAX }, (_, w) => worker(w + 1)))
// Return only once every agent has settled, gate runs included.
await Promise.allSettled(started)

const order = new Map(ITEMS.map((it, i) => [it.issue, i]))
results.sort((a, b) => order.get(a.issue) - order.get(b.issue))
return results
