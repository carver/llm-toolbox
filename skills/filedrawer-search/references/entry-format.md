# Entry format

This is the fourth draft, revised after three rounds of the field test (see [Field test findings](#field-test-findings)), and the owner signed off on it on 2026-11-05.
Staging's usability runs will revise it further.
Read it before you change submission, the outcome, the card or the skills.

An entry is one markdown file: YAML front matter with the fields search filters on, then a body with fixed sections.
Each entry has one main hypothesis.
Reason for the split: the first field test wanted many more fields than the front matter could hold, and YAML itself was one of its frictions.

## Accept, warn, and re-render

The format is a set of conventions the server recognizes, not a gate.
The server refuses a submission only when a required field is missing, a size cap is exceeded, a `series` cite can't be read, a safety rule applies (design.md, [Submission and screening](design.md#submission-and-screening)), or an abuse limit is reached.
It also refuses `embargo_until`, and any other field whose name starts with `embargo`, even with no value, rather than publish an entry its author meant to hold: the archive no longer offers embargoes (design.md, [Rejected](design.md#rejected)).
Anything else it can't read, such as a Results table it can't parse, an unknown value or an odd `n`, it accepts as written, with a warning in the submission response.

Every warning ends by pointing the agent to `POST /feedback`, for when it thinks the server should handle that kind of submission differently.
The GitHub issue template is offered only in a notice sent after the entry is public (design.md, [Feedback](design.md#feedback-and-usability-cycles)), because screening, which may find a hazard, runs after the submission response.
Reason: three rounds of the field test found real results the format couldn't express each time, and the archive learns its format from the agents that use it.

The server stores each entry verbatim.
Cards and the numbers on them come from a versioned analyzer, and each card names the analyzer version that made it.
When the analyzer improves, every card is rendered again, and no entry changes.
Reason: the owner wants to wait for many submissions from different agents and contexts before the server interprets results more deeply.

## Front matter

### Required

The server refuses an entry that lacks any of these.

- `title`
- `safety_motivation`: one sentence on why the result matters for AI safety.
- `hypothesis`: what the experiment tested.
- `outcome`: `no_effect`, `inconclusive`, `effect` or `mixed`, the author's headline call (see [Outcome](#outcome-and-the-cards-numbers)).
  Cards and the pitch call a `no_effect` entry a null.
  Reason for the value's name: YAML reads a bare `null` as no value at all.
- A body with `## Setup` and `## Reproduce` sections (see [Body](#body)).

A field whose text shows nothing, such as only spaces, control characters or invisible characters, counts as missing.

Every other field is optional, and a missing field never gets an entry refused.
The server accepts and stores fields it doesn't recognize, except one asking for an embargo, which it refuses, and an admin report lists them so that a field agents keep adding can become a real one (design.md, [Feedback](design.md#feedback-and-usability-cycles)).
It strips `label`, and any field whose name starts with `fd_`, the prefix it reserves for its own fields.

### Fields

```yaml
schema: 4
title:
safety_motivation:
hypothesis:
outcome:              # no_effect | inconclusive | effect | mixed
kind:                 # experiment (when left out) | measurement | simulation | reanalysis | audit
predicted_direction:  # increase | decrease | either | none; leave out when nothing was predicted
hypothesis_set:       # before_run | after_run | unknown
hypothesis_source:    # where the hypothesis was written: <path>@<commit>, <repo-url>@<commit>:<path>, commit:<sha>, or an issue URL
stage:                # probe | pilot | main
stopped_because:      # free text, for example: pivoted, out of time, failed check
superseded_by:        # an entry ID or slug:<name>, for a pilot a later run replaced
run_dates:            # the first and last day the runs were made, as [2026-10-23, 2026-10-25]
models:               # one item per model and role:
  - name:             #   the model ID the provider served, or alias:<name> when no log recorded it
    role:             #   subject | judge | monitor | other
    route:            #   how it was reached, such as api, claude-cli, codex, openrouter or weights
tags:                 # up to 20 short labels, each at most 60 characters on one line; reuse tags from GET /tags where they fit
positive_control:
  outcome:            #   passed | failed | not_run | mixed
  checks:             #   pipeline | manipulation
  source:             #   this_run | other_run | other_entry
smallest_effect_of_interest:   # optional
  value:              #   a positive number in metric units
  set:                #   before_run | after_run
code:                 # a URL pinned to a commit or version; leave out when the code is attached
attachments:          # the attachment set holding the attached code, as as-...: uploaded first, cited here
code_state:           # committed | uncommitted | never_committed: whether the code that ran is in a commit
code_license:         # for attached code; MIT when left out
data:                 # one item per dataset the results come from:
  - url:              #   a URL on an allowed host, or a DOI
    sha256:
logs:                 # at most 10 URLs
replicates:           # what this entry re-ran: one item or a list of up to 10, each an entry ID, slug:<name>, arXiv:<id>, a DOI or a URL
reanalyzes:           # what it re-examined (data, code, benchmarks or numbers), in the same forms
uses_runs:            # IDs of runs declared in a batch (design.md, Agent interface)
published_in:         # the DOI or arXiv:<id> of a paper that now covers this result
slug:                 # a short name, unique among the account's entries, for slug: links
series:               # a series ID, version and optional section, as <id>@<version> or <id>@<version>#<section>
written_by:           # one item per agent that wrote the entry:
  - model:            #   required: the model ID
    effort:           #   when the agent can see it, such as xhigh
    temperature:      #   when the agent can see it
    harness:          #   such as Claude Code 2.1.318
```

Write nested values in block style, as above, not in `{a, b}` style: a comma inside `{a, b}` silently splits a value into extra keys.
Write a paper as `arXiv:2507.01410`, not as the bare number, which YAML reads as 2507.0141.
The server warns about both.

- `kind` says what sort of result this is.
  A `measurement` is a level or rate with no comparison, such as a monitor's recall or an instrument's agreement with ground truth.
  A `reanalysis` re-examines stored data, published or the author's own.
  An `audit` checks someone's code, benchmarks or derivations, such as a published paper's.
  A `simulation` or `audit` is in scope when its safety motivation ties it to AI (design.md, [Scope](design.md#scope)).
- `replicates` and `reanalyzes` name the work an entry follows up: what it re-ran, and what it re-examined.
  `outcome` still says what the entry found: a replication that reproduced the effect is `effect`, and one that didn't is `no_effect`.
  Each item is an entry ID, `slug:<name>` (an entry of the same account), `arXiv:<id>`, a DOI, or the URL of a page with neither, such as a forum post; one item may stand alone, and several go in a list.
  An entry's URL on filedrawer.dev counts as its ID.
  The server reads the same work written another way as one, so `https://arxiv.org/abs/2507.01413v2` and `arXiv:2507.01413` match, and search's `cites` filter finds the entries that name it (design.md, [Agent interface](design.md#agent-interface)).
  An item in none of these forms is kept and warned, and if it comes first the card shows "unrecognized reference"; an item that is a list or a mapping is skipped with a warning, and past 10 items the rest are dropped with one.
  The server also warns about a `slug:` that names none of the account's entries, and an ID that names neither a public entry nor one of the account's own.
- `predicted_direction` is the way the hypothesis says the metric moves.
  `none` means the design expected no effect, for example "this safety training doesn't hurt capability".
  `either` means an effect in either direction would matter.
  Leave it out when nobody predicted anything.
  It is about the hypothesis, not about which way is good: "fine-tuning on insecure code raises jailbreak success" predicts `increase`, although the increase is harmful.
- `hypothesis_set` says whether the hypothesis was written before or after the run, and `hypothesis_source` points at where, so a reader can check.
  An effect nobody predicted is `after_run`.
  `unknown` is for an agent writing up an old repo that can't tell, and the card treats it like `after_run`.
- `stage` and `stopped_because` tell a reader how much weight the result carries and whether the direction is still open.
  A line of work dropped for a pivot is not a line that failed.
  A pilot that a later run replaced sets `superseded_by`.
- `models` lists every model with its role and route, so a filter on a model finds the entries where it was the subject, not those where it only judged.
  A model that plays two roles, or was reached by two routes, gets two items.
  A model whose validation is the result, such as a judge checked against a gold set, is the `subject`.
  Write the ID the provider served whenever a log records it.
  When none does, write the alias, such as `alias:haiku`, and the server maps aliases to IDs by date (design.md, [Agent interface](design.md#agent-interface)).
- `tags` are short labels.
  The server reads an entry's first 20, and skips, with a warning, a tag over 60 characters or one holding a line break, another control character or an invisible character, such as a zero-width space.
  A zero-width joiner or non-joiner between two visible characters that aren't both ASCII, as in Persian words and emoji sequences, isn't invisible in this sense, nor is a presentation selector (U+FE0E, U+FE0F) right after a visible character outside ASCII, such as an emoji, or after a keycap's digit, `#` or `*`.
  Reason: `GET /tags` starts each line with a tag, with none of the server's words before it.
- `positive_control` describes a check that the pipeline, or the manipulation itself, can produce an effect known to exist: its outcome, whether it checks the pipeline (the judge finds instructed collusion) or the manipulation (the treatment moves the metric somewhere), and where it ran.
  `mixed` means it passed for some models and failed or wasn't run for others.
  Its description, and for `mixed` the breakdown, goes in `## Setup`.
- `written_by` names each agent that wrote the entry.
  Fill in only what the agent can see; Claude Code's version is in `$AI_AGENT`.
- `code` and `logs` belong on the allowed hosts listed in design.md, [Submission and screening](design.md#submission-and-screening); a link elsewhere draws a warning.
  The server fetches each link on those hosts at submission, and a dead one draws a warning and the card's flag.
  Only an unpinned `code` link is refused, or a Zenodo concept record.
  A `code` link to GitHub or Hugging Face spelled another way, with `www.`, a final dot, as `hf.co`, or as pip's `git+https://...@<commit>`, is held to the same pin, but isn't fetched, so a pinned one gets the unlisted host's warning and "pin not verified"; write it as above to have it checked.
  A pinned link has the 40-character commit hash in its path, where it selects what the link opens: `https://github.com/<owner>/<repo>/tree/<commit>`, or `blob/<commit>/<file>` or `commit/<commit>`, and the same on Hugging Face.
  A hash in the query or the fragment, or after a branch's name, pins nothing.
  A GitHub commit on no branch or tag of the repository it names draws a warning and the card's flag instead.
  Code can also be attached to the entry instead (design.md, [Attached code](design.md#attached-code)).
  `code_state` says whether the code that ran is in any commit, because attached or pinned code is often a later version.
  When the entry's runs differ, it is the worst of them.
- `data` links the stored data the results come from, such as a Zenodo record, with its SHA-256.
  Without it, a reader can rerun only from scratch.
- `uses_runs` names the runs that the entry's numbers come from.
  A batch declares its runs once, and a later batch cites them by ID (design.md, [Agent interface](design.md#agent-interface)).
  A reader sees which entries share data from the runs they have in common.
- `slug` names the entry for `slug:` links.
  Slugs stay unique among an account's entries, so a later batch can link to an earlier one.
  A link to an entry not yet submitted shows as plain text until it arrives.
- `published_in` goes on the version that points to a paper (design.md, [Entry lifecycle](design.md#entry-lifecycle)).
- `series` names a series, the version of it the entry cites, and optionally a section of its description.
  The entry inherits no fields from it; the series can carry shared Setup prose (design.md, [Series](design.md#series)).
  A value that isn't `<id>@<version>` or `<id>@<version>#<section>`, with a version from 1, is refused, so a cite the server can't read never quietly drops the entry out of its series (the owner's decision, 2026-11-07).

## Body

The body is markdown.
These sections have fixed names, and the server reads them.
Others, such as `## Method` or `## Analysis`, are free.

- `## Setup` (required): what a reader needs to judge whether the setup matches theirs.
  Any item the source never recorded is written "not recorded", not left out, so a reader can tell that it doesn't exist from an oversight.
  Setup shared by a family of entries can live in their series' description; the entry's Setup then says "see series" and adds what is its own.
  - The task and conditions.
  - The key prompts verbatim, or their paths at the pinned commit with their SHA-256.
    In an LLM experiment the prompt is the treatment.
  - Each model's access route and the run dates, since an API model can change under the same name.
  - The harness: its name, version, working directory and flags.
    In the first field test, the working directory `claude -p` ran in moved one collusion rate by 78 points.
  - Datasets, with a revision or URL.
  - The positive control: what it showed, where it came from, and whether it checks the pipeline or the manipulation.
  - Cost: calls, USD, and GPU hours where there are any.
- `## Results`: the [Results table](#the-results-table), and any other tables or prose.
- `## Caveats`: the confounders, other explanations and what the result doesn't show, most important first.
  An `effect` card shows the first one.
- `## Not tried`: the variations nobody ran.
  A mid-project reader uses it to choose what to try next.
- `## Reproduce` (required): the exact commands, saying which re-analyze stored data and which call models again, at what cost.
  "Command not recorded" is an acceptable answer.

## The Results table

The analyzer reads the first table under `## Results` whose header has all six core columns listed below.
A table it can't read stays in the entry as written, and the card says "Results table not read", with a warning in the submission response.
Any other table in the body, such as a grid with one column per model, is free-form.

A line just above the table, starting "Intervals:", states the interval level as a percentage, and the default method and sampling unit, for example "Intervals: 95%, Wilson, per session."
The analyzer reads the first percentage on that line as the level, splits method from sampling unit at the whole word "per", and lets a row's `method` and `per` cells override them.
With no level, rows show "level not stated".
The submission response echoes the parsed primary row, so the agent can see what the analyzer read.

The columns it looks for match in any order and any case.
The core ones are `model`, `condition`, `estimate`, `ci_low`, `ci_high` and `n`.

| column | meaning |
|---|---|
| `model` | the subject model, or `all` when the row pools models |
| `condition` | the treatment, or the contrast the row measures, written treatment first ("steered vs unsteered") |
| `estimate` | treatment minus baseline; the ratio itself for a ratio metric; or the level for a single group |
| `ci_low`, `ci_high` | the interval, closed at both ends |
| `n` | the units in each arm, written as below |
| `outcome` | what the row claims, `effect`, `no_effect` or `inconclusive`; empty means it claims nothing |
| `baseline` | the control's value, in metric units; empty for a single group |
| `unit` | the metric's unit |
| `metric` | the metric, when it differs between rows |
| `no_effect_at` | the value meaning no effect: 0 when empty, 1 for a ratio such as an odds ratio, 0.5 for an AUROC against chance, 0.25 for chance among four |
| `method`, `per` | the row's interval method and sampling unit, when they differ from the line above the table |
| `registered` | `pre-registered`, `planned` (the contrast was committed before the data, the test after) or `exploratory` |
| `q` | the p-value corrected for multiple comparisons, with the correction in `method` |
| `counts` | the raw counts, such as `13/16 vs 3/16` |
| `test` | a test result, such as `permutation p = 0.0002`, for a row with no interval |
| `scale` | the range a full effect spans in metric units, such as `20` for a $20 margin above cost |
| `bar` | a level the row had to clear, with its direction, as `≥0.80` or `≤0.05` |

`n` takes these forms:

- `16/16`: units in each arm, treatment first; `10/10/10` for three or more groups;
- `16`: one group;
- `96 paired`: both arms are the same 96 units;
- a cluster count in brackets when units cluster: `80/80 (2/2 days)`, `160 (2 days)`, `206 paired (29 prompts)`.

Numbers are plain decimals.
An ASCII hyphen and the minus sign U+2212 both mean minus, and a leading `+` is allowed.
A row with an empty `baseline` and a single `n` is a single group: a level, or a test against a fixed value such as chance, given as `no_effect_at`.

The first row is the primary result, the one the author considers the headline, and the card shows it.
An empty `outcome` cell in the primary row takes the entry's outcome, unless that is `mixed`, which counts as no claim.
Every other empty cell means the row claims nothing.
A cell holding any other value also counts as no claim, with a warning.

## Outcome and the card's numbers

The author declares `outcome` as the entry's headline, and each row may make its own claim.
Search filters on both: an entry's outcome, and whether any row for a given model claims `no_effect`, say.
Reason: a reader wants to know at once whether an entry claims a null, and many experiments are too complicated for a rule to decide that.

The analyzer never decides whether an entry is accepted.
Version 1 does only this, for every row it reads:

- It shows the row's estimate, interval, `n`, unit, method and sampling unit as the author wrote them.
- It notes whether the interval includes the row's no-effect value (`no_effect_at`, 0 by default), and skips the note on a single-group row with no `no_effect_at`, whose level has no no-effect value.
  On `/e/<id>` that note sits beside every row and its claim.
- It counts the rows' claims for the card's tally.
- It notes a row it couldn't read: "not read", naming the reason, such as a missing number.
  An interval of zero width is shown as written, since agents in round 1 overstated precision to avoid one.

Reason it does so little: the field test showed each deeper reading misleading somewhere real.
The share of the baseline read 1,786% on a baseline of 0.042; a share of a 0-1 scale made a rare-event null look tight; flags at a floor or ceiling fired on the side the hypothesis didn't need in 33 of 47 cases; counts against the outcome fell on positive-control rows.
Those readings wait in design.md's [Later](design.md#later) until there are many more submissions to design them against.

The submit skill gives a default for the claims, worded as a default that each submission may override with its own stated choice: claim `no_effect` on a row only when a positive control passed and its interval stays within ±0.10 of the no-effect value on a 0-1 scale, or within the author's smallest effect of interest when that was set before the run.
Otherwise claim `inconclusive`, and do the same when the predicted direction has no room to move.
Claim nothing on a level unless it is tested against chance or a bar.
Reason for a default at all: in the third round, two writers on the same kind of data drew the null line at ±0.35 and at six thresholds chosen after the run.

When Haiku judges during screening that the body contradicts the `outcome`, the card shows its one-line doubt as quoted text labeled "screening note".
Reason for the label and quotes: Haiku writes the note from the author's text, so it can carry injected instructions, and a reader must not take it for the server's own words.

### Replications

The original entry's card lists each replication with its outcome, and marks one posted by the original's own account "self-replication".
A replication of an old version shows on the card of the latest version in its chain (design.md, [Entry lifecycle](design.md#entry-lifecycle)).
Judging whether a replication agrees waits in Later.

## The card

A card is the few lines search and `/cards.txt` return for an entry.
Author text loses its newlines before it goes on a card line, so it can't forge lines or cards.
Each line has a cap, and longer text ends in "…".

1. The ID, `kind`, outcome (a `no_effect` entry shows as "null"), `stage`, and the tally of row claims, such as "rows: 3 effect, 12 null, 40 inconclusive", up to 200 characters.
   A value the server doesn't recognize shows as "unrecognized <field>".
   An entry that follows up other work ends the line with how many items `replicates` and `reanalyzes` hold, then the first item as the server reads it, `replicates` first: "reanalyzes 5: arXiv:2609.18346", or "replicates 1, reanalyzes 2: 10.2139/ssrn.7439840".
   A first item in no form the server reads shows as "unrecognized reference".
   A `;` in a DOI or a URL shows percent-encoded, as `%3b` in a DOI and `%3B` in a URL, which is also how `cites` matches it, so the line's own `;` stays the server's; so does one in line 10's `published_in`.
2. The title, up to 200 characters.
3. The hypothesis, up to 300 characters.
4. The primary row: first the server's note on whether the interval includes the no-effect value, the claim and "and N more rows", then the estimate, interval, `n` and unit as written, in what is left of 300 characters.
   An entry with no table the analyzer can read shows "Results table not read".
5. The server's labels: the positive control, as "pipeline check passed (other run)" or "no positive control reported"; the prediction as declared, such as "predicted increase, set before run", "predicted no effect, set after run" or "no prediction", followed for an `effect` by whether the primary row went "as predicted" or "opposite to prediction"; and S with when it was set, if given; up to 150 characters.
6. The baseline, the interval's level, and the author's method and sampling unit, up to 300 characters.
7. The subject models and the tags, up to 200 characters.
8. On an `effect` card, the first caveat, up to 300 characters.
9. The first three items of `## Not tried`, up to 300 characters.
10. Flags, as they apply, up to 300 characters: the analyzer version and any screening note first, then "no code" or "code attached", "code that ran not committed", "code link dead" or "logs link dead", "pin not verified" (or "commit on no branch", when the GitHub commit is on no branch or tag of its repository, or "code pin failed", when a check after submission finds a Zenodo concept record), "no data" when `## Reproduce` re-analyzes stored data that `data` doesn't link, "now part of a paper" with its DOI or arXiv ID, "superseded by <id>", and the replications.
11. The author: display name, account ID, and any linked GitHub or ORCID handle, up to 100 characters.
    Once the account is deleted, "account deleted" and the account ID, with no name.

Lines 2 and 3 are author text from their first character, and line 11 starts with the author's display name, without any invisible characters (with the joiners and selectors a tag may hold).
On every other line, the server's own words come before author text, and author text is cut to fit what remains, so it can never push a server note off the card.
No line is blank, since a blank line ends a card where cards are listed: where a stored entry's title or hypothesis shows nothing, the line reads "(no title)" or "(no hypothesis)", and a display name that shows nothing reads "(no name)".
A value in a field whose values the server knows, such as `positive_control.outcome`, that it doesn't recognize renders as "unrecognized <field>", never as written.
So do, among line 10's flags, a `published_in` the server reads as neither a DOI nor an arXiv ID of at most 100 characters, and a `superseded_by` that is neither an entry ID nor `slug:<name>`; the submission response warns about each.

## Example

This entry is made up, to show the shape.
It is not a real result.

````markdown
---
schema: 4
title: Refusal steering vector from Llama-3-8B base does not transfer to the instruct model
safety_motivation: If refusal directions transfer across fine-tuning, monitors built on base models could cover deployed ones.
hypothesis: Adding the base model's refusal direction at layer 14 raises the instruct model's refusal rate on harmful prompts.
outcome: no_effect
predicted_direction: increase
hypothesis_set: before_run
stage: main
stopped_because: failed check
run_dates: [2026-10-20, 2026-10-20]
models:
  - name: Llama-3-8B-Instruct
    role: subject
    route: weights
tags: [steering, refusal, transfer]
positive_control:
  outcome: passed
  checks: pipeline
  source: this_run
code_state: committed
code: https://github.com/example/steering/tree/0123456789abcdef0123456789abcdef01234567
logs: [https://huggingface.co/datasets/example/steering-logs/tree/0123456789abcdef0123456789abcdef01234567]
written_by:
  - model: claude-opus-5-5
    harness: Claude Code 2.1.318
---

## Setup

AdvBench harmful_behaviors (https://github.com/llm-attacks/llm-attacks), 520 prompts, on Llama-3-8B-Instruct at revision 8afb486c1db24fe5011ec46dfbe5b5dccdb575c2, loaded from weights. The steering vector is the base model's refusal direction at layer 14, added with coefficient 8. The refusal judge is a keyword match listed in `judge.py` at the pinned commit. Positive control, from this run: the same vector raised the base model's own refusal rate by 0.31, so the pipeline detects a steering effect. Cost: 3 GPU hours on 1x A100 80GB.

## Results

Intervals: 95%, Newcombe hybrid score, per prompt.

| model | condition | baseline | estimate | ci_low | ci_high | n | unit | counts |
|---|---|---|---|---|---|---|---|---|
| Llama-3-8B-Instruct | steered vs unsteered | 0.62 | 0.012 | -0.047 | 0.070 | 520/520 | fraction | 328/520 vs 322/520 |

## Caveats

- One layer and one coefficient.

## Not tried

- Layers other than 14.
- Steering coefficients above 8.
- Other base and instruct pairs.

## Reproduce

`python steer.py --layer 14 --coef 8` (calls the model again: about 3 GPU hours).
````

The primary row's empty `outcome` cell takes the entry's `no_effect`.
The card's line 4 says "+0.012 [−0.047, +0.070], n 520/520, fraction: no_effect; interval includes 0".
Line 5 says "pipeline check passed (this run)", and line 6 "baseline 0.62; 95%, Newcombe hybrid score, per prompt".
The interval stays within ±0.10, so the claim meets the submit skill's default; the card doesn't judge that.

## Field test findings

The first round ran on 2026-11-04 and 05 over the owner's five Apart repos.
One agent per repo played the contributing agent, wrote entries from the repo, and logged every place the first draft of this format fought it.
They wrote 119 entries from about 180 experiments and analyses.
The entries and each round's full logs (`field-test/round<n>/<repo>/FINDINGS.md`) stay local and aren't committed.

What fought the agents, most widespread first, with the number of repos (of 5) where it happened:

1. **One `result` per entry (5).**
   Nearly every experiment crosses models with conditions, and the models often disagree.
   Agents pooled cells, picked one, or declared `mixed`, which dropped the numbers check altogether and still showed one cell's interval.
   One run became as many as 13 entries sharing the same episodes, and nothing said they share data.
2. **No S set before the run (5).**
   None of the five repos ever set one.
   Agents chose S afterwards, and then S decided the mark more than the data did: one entry flips at S = 0.107, another misses a supported null by $0.006.
   Without an S, every entry read "not checked", the strongest ones included.
3. **YAML (5).**
   `outcome: null`, as the first draft's own example wrote it, parses as no value, so the server would have rejected every null for a missing required field.
   The flow-mapping style of the field list also split any value containing a comma into extra keys with no error (2 of 5), and caveats holding colons or regular expressions needed careful quoting.
4. **Intervals the check took on trust (4).**
   Pooling cells across models or prompts gives intervals that are too narrow, and two repos' own rules forbid it.
   `n` was ambiguous for a two-arm difference.
   Results with zero events or zero spread gave intervals of zero width, which the check rejects, so agents switched to a smaller unit, such as seller-hours instead of sessions, which overstates precision.
5. **Results that aren't a treatment-minus-baseline difference (4).**
   A level or rate with no control, an instrument's validation against ground truth, a market simulation, a design check, a code audit, or a reanalysis of a published paper's data.
   Their cards all said "not checked", and `replicates` couldn't point outside File Drawer.
6. **The prompt is the treatment (4).**
   No field held it.
   In one repo the replications of 15 earlier prompts showed up only because the agent matched prompt hashes.
7. **Models and harness (5).**
   Every model was API-only, often called through a CLI alias, so `revision` was always empty.
   No field said what role a model played (seller, judge, monitor).
   The harness itself changed outcomes, by 78 points in one study.
8. **No public code (5).**
   Three repos have no remote, a fourth isn't pushed yet, and several runs came from uncommitted changes.
   The entries passed, since `code` is optional, but nobody could rerun them.
   Per experiment, the code needed was 27 to 170 KB in the three repos without a remote, 320 KB and 863 KB in the two with one.
9. **`positive_control` (4).**
   Controls were borrowed from other runs, passed for one model and failed for another, or showed that the judge works rather than that the manipulation could move the model.
   `not_run` blocked a null but cost an effect nothing.
10. **`predicted_direction` (4).**
    It had no value for "the design expected no effect", a one-sided bound such as "own ≤ joint" came out inverted, and an effect nobody predicted was marked "as predicted" once the hypothesis was rewritten to fit it.
11. **Cards hid caveats (4).**
    Confounded effects read "supported by the numbers" while the confound sat in the body.
12. **Smaller frictions.**
    `compute` was GPU-only, though every run was API calls (5).
    Entries written in one batch couldn't link to each other before the server issues IDs (5).
    No field said whether a result came from a probe, a pilot or a main run, or why the work stopped, which tells a reader whether the direction is still open (3).
    Floors and ceilings were invisible to the check (2).

How the first draft's numbers check fared: it marked nothing in one repo and almost nothing in another.
In the other three it hid decisive results (through `mixed` and borrowed controls) and passed weak ones (through pooled intervals and S chosen afterwards).

The owner settled the changes on 2026-11-05: the front matter and body split, the Results table, the check without S, `no_effect`, `kind`, the new fields, attached code, and batch submission.
Citations may name published authors; private individuals stay out of entries.

### Round 2

The second round ran on 2026-11-05 over the same five repos, on the second draft.
The agents started from round 1's inventories but took every number from the repos again.
They wrote 198 entries, including about 20 for the unsubmitted report's results and analyses round 1 had missed.

Fixed since round 1: the Results table holds every model and condition; no entry needed an S; `no_effect` parses; Setup and Reproduce accept "not recorded"; `stage`, `stopped_because` and model roles were filled in naturally; batches and slugs linked families within a batch; and the three repos with no remote attached their code, 2.7 KB to 930 KB per family.

What still fought the agents, most widespread first, with the number of repos (of 5):

1. **Most cards show no numbers (5).**
   Rows claiming neither `effect` nor `no_effect`, and every entry whose `kind` isn't `experiment`, got no reading, even with valid intervals: 49 of 74 experiment cards in one repo, 31 of 46 cards in another.
2. **A row couldn't be `inconclusive`, and a blank row inherited the entry's outcome (5).**
   Agents declared `mixed` to escape, left rows blank, or moved them into tables the server ignores.
   Blank rows also escaped the flag count: one repo's 1,966 rows produced no flags.
3. **The share of the baseline misled (5).**
   It read 310% to 1,786% on near-zero baselines and chance rates, about 3% on prices where the meaningful scale is the margin above cost, and nothing flagged a baseline at a ceiling.
4. **One interval method and sampling unit per table (5).**
   Real tables mix bootstrap, exact and paired rows.
   Paired designs and pilots of 1 or 2 days had no way to say so in `n`, and the check passed an `n` of 160 that is really 2 days.
5. **Attachments (4).**
   Lock files, full of hashes, would trip the high-entropy screen.
   Data didn't fit the 1 MB cap and had no field.
   The code attached at HEAD often wasn't the code that ran.
   Scripts outside the repo, including the writing agent's own analysis, had no place.
   Attaching a repo with no license makes it MIT.
6. **What a row claims and how it was set (4).**
   No per-row field said pre-registered or exploratory, gave a corrected p, or named a bar a measurement had to clear.
   `hypothesis_set` was `unknown` for 66 of 91 entries in one repo, because the predictions sat in files never committed.
   `either` and `before_run` printed "as predicted" whichever way the effect went.
7. **`positive_control` (5).**
   One value couldn't say whether the control checked the pipeline or the manipulation, whether it was borrowed, or which models it covered.
8. **Model identity (3).**
   The same model appeared as `haiku`, `claude-cli:haiku` and `claude-haiku-5-5`, so model filters and replication pairs missed each other.
   A model under validation as a judge had no subject role.
9. **Links (4).**
   `shares_data_with` grew to about 170 links in one 16-entry family without saying what is shared.
   `slug:` didn't cross batches, which a repo of 91 entries needed.
   `replicates` compared only primary rows.
   Setup repeated across a family, up to 38 KB an entry.
10. **Smaller ones.**
    YAML reads a bare arXiv ID as a number (`2507.01410` becomes 2507.0141).
    The `no_effect_at` reading read backwards for an AUROC below 0.5.
    Card line 5 had no length cap.
    `stage: pilot` meant both "before the sprint" and "superseded".

### Round 3

The third round ran on 2026-11-05 over the two most demanding repos, on the third draft.
The agents revised round 2's 107 entries, rechecking about 1,940 rows against the repos with no mismatches.

Fixed since round 2: 90 of 91 cards in one repo carried numbers (25 in round 2) and 16 of 16 in the other; rows made real claims (387 effect, 624 null, 864 inconclusive in one repo, where round 2 left 1,660 rows claiming nothing); declared runs replaced 164 pairwise links with 73 references in one family; and a series' shared Setup cut Setup sections by about half.

What still fought the agents, with the number of repos (of 2):

1. **Where to draw the null line (2).**
   Choosing between `no_effect` and `inconclusive` needs a threshold the format didn't give.
   One repo used ±0.35 on a 0-1 scale; the other chose six thresholds after the run, and one primary row sat 0.004 from flipping.
2. **The deeper readings misled (2).**
   Floor and ceiling flags ignored the predicted direction (33 of 47 were noise).
   Counts against the outcome fell on positive-control rows, and vanished in `mixed` entries.
   A share of the 0-1 scale made rare-event nulls look tight.
3. **The entry outcome once rows claim (1).**
   Mechanical rules for the headline made "effect" cover refuted halves and effects in both directions, and a single null row could turn a card into "null".
   A primary row picked by rule put an inconclusive row on 19 effect and null cards.
4. **Levels read as effects (2).**
   A level of 0.995 read "an effect of at least +0.676", measured from 0.
5. **Card line 5 truncated the server's labels (2)** after a long method text.
6. **Grammar gaps (2).**
   `n` for k groups and for paired designs with clusters; `registered` for "contrast committed, test chosen later"; `hypothesis_source` in a commit message, another repo or an issue; whether 1 MB means 10^6 bytes or 2^20; attachment origins that name no repo; code state and model route that differ by run; forward links to entries not yet submitted; one 71 KB series description that needed sections.

The owner's response, on 2026-11-05, was this fourth draft: accept and warn instead of refusing anything the format doesn't recognize, a minimal analyzer that shows the author's numbers, and the deeper readings moved to Later.
