// paperkit's default template: a plain article. A build writes it to build/paperkit-template.typ;
// paper.toml's `template` names a project's own instead, which exports the same three functions
// (lib.typ says what each does).
// US Letter, 1-inch margins, Libertinus Serif 11 pt (Typst's own font), numbered sections, a
// centred title block with the authors and an inset abstract.

#let paper(title: none, authors: (), abstract: none, body) = {
  set document(title: title) if title != none
  set page(paper: "us-letter", margin: 1in)
  set text(font: "Libertinus Serif", size: 11pt, lang: "en")
  set par(justify: true, leading: 0.55em, spacing: 0.9em)
  set heading(numbering: (..n) => if n.pos().len() == 1 { numbering("1.", ..n) })
  show heading.where(level: 1): set text(size: 13pt)
  show heading: set block(above: 1.2em, below: 0.6em, sticky: true)
  show link: it => if type(it.dest) == str { text(fill: rgb("#1155cc"), it) } else { it }
  set figure(gap: 0.6em)
  show figure.caption: set text(size: 10pt)
  show figure.where(kind: table): set figure.caption(position: top)
  set table(stroke: 0.5pt + luma(140), inset: (x: 4pt, y: 2.5pt))
  show table: set text(size: 9pt)

  if title != none {
    align(center, text(size: 18pt, weight: "bold", title))
    v(8pt)
  }
  if authors.len() > 0 {
    let cols = calc.min(authors.len(), 3)
    align(center, grid(
      columns: (1fr,) * cols,
      row-gutter: 8pt,
      ..authors.map(a => align(center, [#a.name \ #text(size: 10pt, a.at("affiliation", default: []))])),
    ))
    v(8pt)
  }
  if abstract != none {
    align(center, text(weight: "bold")[Abstract])
    pad(x: 0.6in, par(justify: true, text(size: 10pt, abstract)))
    v(8pt)
  }
  body
}

// A section heading without a number (Acknowledgements, References).
#let unnumbered(title) = heading(level: 1, numbering: none, outlined: false, title)

// Appendix sections, lettered A, B, ...
#let appendix(body) = {
  counter(heading).update(0)
  set heading(numbering: (..n) => if n.pos().len() == 1 { numbering("A.", ..n) })
  body
}
