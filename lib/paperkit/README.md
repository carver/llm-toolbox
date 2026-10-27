# paperkit

Builds a paper written in [Typst](https://typst.app/docs/) whose numbers, tables and figures come from code, never typed by hand, and checks it. A project keeps its paper in `paper/`:

| file | what |
|---|---|
| `paper/paper.typ` | the paper |
| `paper/paper.toml` | which optional rules the build checks (below); optional |
| `paper/data/*.py` | the data sources, which fill the ledger |
| `paper/*.bib` | the references |
| `paper/citations.md` | what each source supports and how much of it was read, for `citations_read_check` |
| `paper/fonts/` | fonts for Typst and the figures; optional |
| `paper/build/` | the build's output; don't commit it (add `paper/build/` to `.gitignore`) |

```bash
build-paper           # the draft: paper/build/DRAFT-paper.pdf
build-paper --pages   # also a picture of each page, paper/build/pages/page-NN.png (a build without
                      # --pages moves the last ones to paper/build/pages.old/; until 2026-10-27 every
                      # build drew them)
build-paper --final   # the submission: paper/<final_name>.pdf, only when every check passes
build-paper watch     # the draft now, recompiled on each save and rebuilt on each commit (below)
build-paper find TEXT # the pages of the last build holding TEXT, with their PNGs when it drew them
build-paper keys PREFIX...  # each ledger key starting with a PREFIX: its value and rate (--how: how, and its logs)
```

`find` ignores case and whitespace, so a phrase matches across the PDF's line breaks, and a word hyphenated at a line's end matches whole. `keys` reads `build/numbers.json`, one line a key, where `build/numbers.md` lists the whole ledger.

`build-paper` is llm-toolbox's `bin/build-paper` (or a project's own, below). It runs paperkit inside the project's own uv environment (`uv run`, so a data source can import the project's code), with paperkit's pinned dependencies (`requirements.txt`) added. The project is the git repository holding the working directory. paperkit started as a copy of the paper build in apart-research-collusion-sprint-2026 (`paper/`, at 1f78fba).

## Sharing the build

```bash
build-paper vendor           # copy paperkit into the project as paperkit/, and write bin/build-paper there
build-paper vendor --check   # list the files edited in the project, then those behind the toolbox's (exit 1)
```

A collaborator without llm-toolbox builds with the project's `bin/build-paper`, which runs its copy. While a project has `paperkit/`, llm-toolbox's `build-paper` builds with that copy too, never the toolbox's, so everyone builds with the same code; run `vendor` again to update the copy. A project without one always gets the toolbox's current paperkit.

The project's copy runs no tests of its own: llm-toolbox's tests cover the version it was vendored from. `paperkit/VENDORED` records that version's commit and a hash of each file vendor wrote. A collaborator can still patch the copy; each build then lists every file edited, added or removed since vendoring among its warnings, which never refuse a draft or `--final`, with a reminder to upstream the change. `vendor` refuses to replace a copy holding an edit the toolbox doesn't have yet (a file removed in the project holds nothing to lose, so it doesn't count), so the flow is: port the edit into llm-toolbox's `lib/paperkit/`, where its tests run, or undo it in the project, then vendor again. Hashes ignore line endings, so a checkout that writes CRLF edits nothing. A copy vendored before there were manifests, or whose manifest can't be read (a build warns of that), has nothing to tell edits from an older version, so `vendor` replaces it and names the files that differed from the toolbox's.

## Writing

Start `paper.typ` with paperkit's functions and the layout:

```typst
#import "build/paperkit.typ": *
#show: paper.with(
  title: [The title],
  authors: ((name: [Ada Lovelace], affiliation: [Analytical Engines]),),
  abstract: [...],
)
```

Every build writes `build/paperkit.typ` (this folder's `lib.typ`) and the default template beside it, so paper.typ compiles from a fresh checkout after one build.

Write one sentence per line, so diffs and merges go sentence by sentence. Never type a number from the data. Cite it by its key, and the build fills it in:

| in paper.typ | renders | for |
|---|---|---|
| `#n("exp1.same.hits")` | 43/48 | a count, or a plain value |
| `#pct("exp1.same.hits")` | 90% | a count as a whole percent |
| `#ci("exp1.same.hits")` | 90% (78–96%) | one cell's rate with its 95% Wilson interval |
| `#claim("text", (keys...), test)` | nothing, when it holds | wording that depends on the numbers |
| `#data-table("exp1-table")` | a table | inside `#figure(...)`, captioned and labelled |
| `#data-figure("exp1-figure", width: 100%)` | the figure | the same |
| `#data-text("exp1.prompt")` | the text, verbatim | prompts in an appendix |
| `#todo[...]` | red TODO | anything still to write |
| `#references("refs.bib")` | the References section | where the body ends, for `page_cap` |

`build/numbers.md` lists every key with its value, how it was computed and the logs it came from, then each table's rows.

Point to a section with `@label` (`= Methods <sec-methods>`): it prints "Section 3", with no period of its own, so a sentence can end with it. A subsection, or an unnumbered section, prints its title, and `@label[the scope]` prints those words; either links to the heading.

An interval belongs to one cell: rows drawn from one distribution. A count pooled across cells has no interval, and `#ci` on it shows a red marker. Name the fields one cell's rows share in paper.toml (`cell = ["model", "prompt_hash"]`), and a rate over rows that differ in one of them is refused.

Put a `#claim` beside any sentence whose wording the data could make false: "most", "fell", "every model". The test gets each key's rate (or a plain value's value), in order:

```typst
#claim("the new prompt raises the hit rate", ("exp1.new.hits", "exp1.base.hits"), (new, base) => new > base)
```

A draft marks a failing claim in red. `--final` refuses it.

## Data sources

Each module in `paper/data/` registers sources. A source is a function that fills a ledger:

```python
from paperkit import Ledger, source


@source("exp1")
def exp1(ledger: Ledger) -> None:
    rows = load_rows("logs/exp1/results.jsonl")  # the project's own reader
    ledger.rate(
        "exp1.same.hits", cell_rows, lambda row: row["hit"], how="hits / replies"
    )  # one cell
    ledger.pooled(
        "exp1.hits", rows, lambda row: row["hit"], how="all models"
    )  # no interval
    ledger.table("exp1-table", header, rows_of_strings, how="...")
    ledger.figure(
        "exp1-figure", draw, data, how="..."
    )  # draw(ax, data) with matplotlib
    ledger.value("exp1.calls", len(rows), how="calls")
    ledger.text("exp1.prompt", prompt, how="the prompt, verbatim")
```

The modules are imported as `paper.data.NAME`, with the project root on the import path. The build runs each source into its own ledger and refuses two that claim one key. A source that raises adds nothing, and its keys show as missing. It records which logs (`.jsonl` files) each source opened, for `numbers.md` and `keys --how`. Figures come out as SVG with their text left as text, in `figure_font`.

## Templates

paperkit's `template.typ` is a plain article. A project's own template, named by `template` in paper.toml, is a `.typ` file in `paper/` that exports three functions: `paper(..args, body)`, the layout, taking whatever named arguments paper.typ passes (`abstract` among them); `unnumbered(title)`, a section heading with no number; and `appendix(body)`, which numbers its sections as appendices. paperkit adds the draft marks, the abstract's word count and the cross-reference rules around it. lib.typ sets the draft marks as the page's `background` and `footer`, so a template that sets its own `footer` (page numbers, say) hides the draft stamp with the commit and build time, as its own `background` would hide the watermark.

## What a build checks

Always:

- every number, table, figure and text in the ledger
- every claim holds
- no `#todo` markers
- no call followed straight by a semicolon (`#n("key");`): Typst ends the call there and drops the `;`, so write `\;`
- no `#` before a digit (`(#39)`): Typst prints the number without the `#`, so write `\#39`
- every figure referenced in the text, and drawn under the section it's written in (a float that doesn't fit on its page moves on, past the next section's heading; a figure without a caption isn't checked)
- in `--final`: every link in the PDF resolves (a network failure counts), and there are no uncommitted changes

It also warns, without refusing anything, about a sentence that cites the ledger and holds a word such as "every", "none", "most" or "fell" with no `#claim` later in its paragraph.

`paper/paper.toml` turns on the rest. Without its key, a check doesn't run:

```toml
page_cap = 8                                # pages before References, the title block included
abstract_words = [150, 250]                 # the abstract's length, in words
required_sections = ["Code and Data"]       # headings the paper must have
after_references = ["Limitations"]          # headings that must come after References (appendices)
template = "mytemplate.typ"                 # the project's template, in paper/ (else paperkit's)
citations_read_check = true                 # each cited source's row in citations.md reads `full text`
logs_dir = "logs"                           # have --final refuse a log under it that a number came
                                            # from, when it isn't pushed to `upstream`
upstream = "origin/main"                    # the default
final_name = "paper"                        # --final writes paper/<final_name>.pdf (the default)
max_pdf_mb = 25                             # the PDF's size cap
cell = ["model", "prompt_hash"]             # the fields one cell's rows share
figure_font = "Old Standard TT"             # the figures' font (put its files in paper/fonts/)
```

A key paperkit doesn't know is an error, so a misspelt rule can't go quietly unchecked.

A build over `page_cap` adds a report for cutting it back: each line of the PDF past the cap, with the `paper.typ` line it comes from (`paper.typ:41`; found by matching the PDF's line against paper.typ's, which works because the paper keeps one sentence per line), the blank space at the bottom of each page before References, and the short last lines of paragraphs, each with its `paper.typ` line, where a few words cut save a line.

`citations_read_check` reads the first table in `citations.md` with `key` and `read` columns: a source the paper cites passes when its row says `full text` (`abstract`, `catalogue`, `secondhand` and `not checked` are the other values it names).

The status block a build ends with marks `[new]` each problem the last build that compiled didn't have (a draft against the last draft, a final build against the last final one, kept in `build/problems-built.json` and `problems-built-final.json`; a problem whose line number moved isn't new), and says how many are new, so a new problem doesn't hide among old ones. A draft lists what fails and exits 0. `--final` exits 1 on any problem and writes nothing; only a clean one writes the unmarked `<final_name>.pdf` and `<final_name>-numbers.json`, which get committed together.

Each draft page carries a DRAFT watermark and a footer with the commit and build time, so a draft can't pass for the submission. Each build lists the ledger entries that changed since the last build that compiled, all of them in `build/changes.md`.

## Watching

`watch` keeps `build/DRAFT-paper.pdf` current while you write. A save of any `.typ` or `.bib` in `paper/` recompiles it within a second or two, from the numbers and figures of the last full build, and prints Typst's error when it can't. A full build runs in the background each time HEAD moves, and when the text cites a key the ledger lacks, once the text has been still for five seconds (`--settle`). Open the PDF in a viewer that reloads a changed file, such as Skim, VS Code or Zathura; macOS Preview doesn't.

Only one build of a tree runs at a time: a build holds `build/.lock`, and another waits for it, so a watch's builds and another build can't clear each other's figures. A watch says when another process's build pauses it. Whether a lock taken on a host is seen by a build inside a sandbox that mounts the repo, or the other way round, is untested. Every output (the ledger, the PDF, the page images, the figures) is written to a temporary file beside it and renamed into place, so a reader never sees half of one.

## Agents build in a worktree

An agent builds the paper in a worktree of its own (`git worktree add --detach ../<repo>-wt-paper`), never in the repo's main worktree, where the user's `build-paper watch` runs: builds from a sandbox and from the host collided there, leaving a corrupt `numbers-built.json`, and the lock didn't stop them. So when `$AI_AGENT` is set, a build in the main worktree is refused with that command; `--here` builds there anyway. `watch` is the user's, so it isn't refused.

## Files

| file | what |
|---|---|
| `__main__.py` | the command line |
| `build.py` | a build: the sources into the ledger, Typst, the checks, the outputs |
| `checks.py` | the rules |
| `config.py` | paper.toml |
| `data.py` | `@source` and finding the project's sources |
| `ledger.py`, `figures.py` | the ledger, and drawing its figures |
| `typeset.py` | compiling with Typst and querying the result |
| `watch.py` | `build-paper watch` |
| `vendor.py` | `build-paper vendor` |
| `lookup.py` | `build-paper find` and `keys` |
| `fit.py` | the over-cap report |
| `git.py` | the repository: its commit, its changes, what's pushed |
| `lib.typ` | the Typst functions paper.typ uses, and the draft marks |
| `template.typ` | the default template |
| `requirements.txt` | the pinned dependencies `build-paper` adds |
