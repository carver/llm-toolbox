// paperkit's lib.typ: what paper.typ imports, from build/paperkit.typ, where every build writes it.
// The template (paper.toml's `template`, else paperkit's template.typ) with the draft marks applied,
// and the numbers, tables, figures and texts of the ledger (build/numbers.json, which build-paper
// writes before it compiles). Keys are the ledger's; paperkit's README.md lists the functions.
//
// A template is a .typ file that exports `paper(..args, body)`, the layout, which takes whatever
// named arguments paper.typ passes (title, authors, abstract, ...); `unnumbered(title)`, a section
// heading without a number; and `appendix(body)`, which numbers the sections in it as appendices.

#let template-path = sys.inputs.at("template", default: "/build/paperkit-template.typ")
#import template-path as template
#import template-path: appendix, unnumbered

#let draft = sys.inputs.at("draft", default: "true") != "false"

// The marks on every page of a draft: a faint diagonal watermark, and a footer with the stamp
// (the commit and build time).
#let draft-marks(stamp) = (
  background: place(center + horizon, rotate(-45deg, text(fill: luma(88%), weight: "bold", align(center)[
    #text(size: 96pt)[DRAFT] \ #text(size: 24pt)[not for submission]
  ]))),
  footer: align(center, text(size: 8pt, fill: luma(40%))[DRAFT, not for submission · #stamp]),
)

// A caption marked when its figure is drawn under another section than the one it's written in: a
// float that doesn't fit on its page moves on, past the next section's heading, and one placed at
// the top of its page can land above its own (paperkit/checks.py, "placement"). The mark sits in the
// caption, so it moves with the float, and a figure without a caption goes unchecked; a figure's own
// location is where it's written, and so is the order `before` follows.
#let mark-misplaced(caption) = {
  caption
  context {
    let at = here().position()
    let sections = heading.where(level: 1)
    let written = query(selector(sections).before(here()))
    let drawn = query(sections).filter(h => {
      let p = h.location().position()
      p.page < at.page or (p.page == at.page and p.y <= at.y)
    })
    if written.len() > 0 and drawn.len() > 0 and written.last().location() != drawn.last().location() {
      let fig = query(selector(figure).before(here())).last()
      let label = if fig.has("label") { str(fig.label) } else { none }
      let found = (label: label, caption: fig.caption.body, written: written.last().body, drawn: drawn.last().body)
      [#metadata(found) <misplaced>]
    }
  }
}

// A reference to a heading. The template numbers only sections ("1.", "A."), so a subsection, or an
// unnumbered section like Code and Data, prints its title (or the reference's supplement, `@x[the
// scope]`), linked; a section prints its number without the trailing period ("Section 4"), so a
// sentence can end with it. Other references, page references and function supplements stay Typst's.
#let heading-ref(it) = {
  let target = it.element
  if target == none or target.func() != heading or it.form != "normal" or type(it.supplement) == function {
    return it
  }
  let number = if target.numbering != none {
    numbering(target.numbering, ..counter(heading).at(target.location()))
  }
  if number in (none, "") {
    return link(target.location(), if it.supplement in (auto, none) { target.body } else { it.supplement })
  }
  let supplement = if it.supplement == auto { target.supplement } else { it.supplement }
  let number = number.trim(".", at: end)
  link(target.location(), if supplement == none { number } else { [#supplement~#number] })
}

// The page's height and its top and bottom margins (pt), as the template sets them: whether the
// References heading starts its page, and the blank space at the bottom of each page.
#let page-geometry = context {
  let short = calc.min(page.width, page.height)
  let margin(side) = {
    let m = page.margin
    let b = if type(m) == dictionary { m.at(side, default: m.at("y", default: m.at("rest", default: auto))) } else { m }
    let b = if b == auto { 2.5 / 21 * short } else if type(b) == relative {
      b.length.to-absolute() + b.ratio * short
    } else { b.to-absolute() }
    b.pt()
  }
  [#metadata((height: page.height.pt(), top: margin("top"), bottom: margin("bottom"))) <page-geometry>]
}

// The layout: the template's, with the draft marks, and the rules that apply to the whole paper,
// the abstract included. The metadata lets the build count the abstract's words.
#let paper(abstract: none, ..args, body) = {
  let commit = sys.inputs.at("commit", default: "")
  set document(description: if commit != "" { "Built from commit " + commit })
  set page(..draft-marks(sys.inputs.at("stamp", default: ""))) if draft
  show figure.caption: mark-misplaced
  show ref: heading-ref
  template.paper(..if abstract != none { (abstract: abstract) }, ..args, {
    [#metadata(abstract) <abstract>]
    page-geometry
    body
  })
}
#let ledger = json("numbers.json")


// Red, so nothing unfinished goes unseen in a draft.
#let flag(body) = text(fill: rgb("#c00000"), weight: "bold", body)

// A key the ledger lacks, or holds as the wrong kind: marked in red, and reported by the build.
#let missing(key, why: "not in the ledger") = [#metadata((key: key, why: why)) <missing>#flag[[#key?]]]

// The ledger's entry under key when it is one of these kinds, else none.
#let entry(key, ..kinds) = {
  let e = ledger.at(key, default: none)
  if e != none and e.kind in kinds.pos() { e }
}

// A share as a whole percent, rounded half up.
#let percent(x) = str(int(calc.floor(x * 100 + 0.5))) + "%"

// A count ("43/48"), from one cell or pooled across cells, or a plain value as the ledger holds it.
#let n(key) = {
  let e = entry(key, "rate", "pooled", "value")
  if e == none { missing(key) } else if e.kind == "value" { [#e.value] } else [#e.k/#e.n]
}

// A count as a percent ("90%").
#let pct(key) = {
  let e = entry(key, "rate", "pooled")
  if e == none { missing(key) } else { percent(e.rate) }
}

// One cell's rate as a percent with its 95% Wilson interval ("90% (78–96%)"). A count pooled
// across cells (models, market sizes, prompts) has no interval.
#let ci(key) = {
  let e = entry(key, "rate")
  if e != none [#percent(e.rate) (#percent(e.lo).trim("%")–#percent(e.hi))] else if entry(key, "pooled") != none {
    missing(key, why: "pooled across cells, so it has no interval")
  } else { missing(key) }
}

// A sentence whose wording depends on the data ("most", "fell", "every model"). `test` gets the
// rate (or value) of each key in `keys`, in order, and says whether the numbers still support
// the wording. The build reports a failing claim, and one whose keys the ledger lacks; a draft
// also marks it in red.
#let claim(text, keys, test) = {
  let entries = keys.map(key => entry(key, "rate", "pooled", "value"))
  let ok = if none in entries { none } else {
    test(..entries.map(e => if e.kind == "value" { e.value } else { e.rate }))
  }
  [#metadata((ok: ok, text: text)) <claim>]
  if draft and ok != true { flag[[claim #if ok == none [can't be checked] else [fails]: #text]] }
}

// Something still to write. The build counts them; --final refuses any.
#let todo(body) = [#metadata(none) <todo>#flag[[TODO: #body]]]

// A table of the ledger's rows, styled by the template: the first column left-aligned, the rest
// centred. Extra named arguments (align, columns) go to table().
#let data-table(key, ..args) = {
  let e = entry(key, "table")
  if e == none { missing(key) } else {
    let named = (columns: e.header.len(), align: (x, _) => if x == 0 { left } else { center }) + args.named()
    table(..named, table.header(..e.header.map(h => strong(h))), ..e.rows.flatten())
  }
}

// A figure the build drew from the ledger. Extra arguments (width, height) go to image().
#let data-figure(key, ..args) = {
  let e = entry(key, "figure")
  if e == none { rect(width: 100%, height: 1in, stroke: 0.5pt + red, missing(key)) } else {
    image("figures/" + e.path, ..args)
  }
}

// A text from the ledger (a prompt, say), verbatim.
#let data-text(key) = {
  let e = entry(key, "text")
  if e == none { missing(key) } else { raw(e.text, block: true) }
}

// The References heading and the bibliography (APA), from `path` in paper/ (this file is in
// paper/build/). The metadata just before the heading tells the build where the body ends, for the
// page cap: every page before References counts.
#let references(path, style: "apa") = {
  context [#metadata((page: here().page(), y: here().position().y.pt())) <references>]
  unnumbered[References]
  bibliography(if path.starts-with("/") { path } else { "/" + path }, title: none, style: style)
}
