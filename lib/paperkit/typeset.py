"""Typst: compiles paper.typ to a PDF and page images, and reads back what the checks need (the abstract,
headings, figures and references, links, and the markers lib.typ leaves) by querying the document.

paper.typ imports paperkit's lib.typ from build/paperkit.typ, which every compile writes there first, with
paperkit's default template beside it as build/paperkit-template.typ. lib.typ imports the template paper.toml
names, else that default."""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typst
from pypdf import PdfReader

from paperkit.atomic import write_atomic
from paperkit.config import Config

PPI = 100  # the page images: enough to read a table, small enough to look at every page
HERE = Path(__file__).resolve().parent
LIB = "paperkit.typ"  # in build/: paperkit's lib.typ, which paper.typ imports
DEFAULT_TEMPLATE = "paperkit-template.typ"  # in build/: paperkit's template.typ


class BuildError(RuntimeError):
    """Typst couldn't compile the paper."""


@dataclass(frozen=True)
class Typeset:
    pdf: bytes
    pngs: list[bytes]
    warnings: list[str]
    abstract: str
    # "key: why", for each number, table, figure or text the ledger couldn't supply
    missing: list[str]
    # Whether each claim holds (None: a key is missing), and its text
    claims: list[tuple[bool | None, str]]
    todos: int
    # The page and height (pt) just before the References heading
    references: tuple[int, float] | None
    figures: list[str]  # each figure's label, or its caption when it has none
    refs: set[str]  # the labels referenced
    cites: set[str]  # the bibliography keys cited, by @key or #cite
    headings: list[str]
    links: list[str]
    # Each figure drawn under another section: name, written, drawn
    misplaced: list[tuple[str, str, str]]
    height: float  # the page's height (pt)
    top: float  # its top margin (pt)
    bottom: float  # its bottom margin (pt)
    texts: list[str]  # each page's text, as the PDF holds it, line by line


def page_texts(pdf: bytes) -> list[str]:
    """Each page's text, from the PDF."""
    return [page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages]


def plain(node: Any) -> str:
    """The text of a queried piece of content."""
    if isinstance(node, list):
        return "".join(plain(child) for child in node)
    if not isinstance(node, dict) or node.get("func") == "metadata":
        return ""
    if node.get("func") in ("space", "linebreak", "parbreak"):
        return " "
    if node.get("func") == "smartquote":
        return '"' if node.get("double") else "'"
    return (
        node.get("text", "")
        + plain(node.get("children", []))
        + plain(node.get("body"))
        + plain(node.get("child"))
    )


def install_lib(paper_dir: Path) -> None:
    """Writes paperkit's lib.typ and default template into <paper_dir>/build/, where paper.typ imports them,
    each only when it differs, so a watch's viewer isn't sent a change that isn't one."""
    out = paper_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    for name, source in ((LIB, "lib.typ"), (DEFAULT_TEMPLATE, "template.typ")):
        text = (HERE / source).read_bytes()
        if not (out / name).exists() or (out / name).read_bytes() != text:
            write_atomic(out / name, text)


def paper_compiler(
    paper_dir: Path, inputs: dict[str, str], config: Config
) -> typst.Compiler:
    """Typst for <paper_dir>/paper.typ with `inputs` as sys.inputs, and the template paper.toml names. The
    paper reads only inside paper_dir, and its fonts are Typst's own and paper/fonts/."""
    install_lib(paper_dir)
    template = (
        f"/{config.template}" if config.template else f"/build/{DEFAULT_TEMPLATE}"
    )
    fonts = paper_dir / "fonts"
    return typst.Compiler(
        paper_dir / "paper.typ",
        root=paper_dir,
        font_paths=[fonts] if fonts.is_dir() else [],
        ignore_system_fonts=True,
        sys_inputs=inputs | {"template": template},
    )


def typeset(paper_dir: Path, inputs: dict[str, str], config: Config) -> Typeset:
    """Compiles <paper_dir>/paper.typ with `inputs` as sys.inputs."""
    compiler = paper_compiler(paper_dir, inputs, config)

    def query(selector: str) -> list[dict]:
        return json.loads(compiler.query(selector, format="json"))

    def label(node: dict) -> str:
        return node["label"].strip("<>")

    try:
        pdf, warnings = compiler.compile_with_warnings(format="pdf")
        pngs = compiler.compile(format="png", ppi=PPI)
        if not isinstance(pdf, bytes) or pngs is None:
            raise BuildError("Typst returned no PDF, or no page images")
        abstract = query("<abstract>")
        references = query("<references>")
        figures = query("figure")
        geometry = query("<page-geometry>")
        height, top, bottom = (
            (geometry[0]["value"][side] for side in ("height", "top", "bottom"))
            if geometry
            else (792, 72, 72)
        )
        typeset = Typeset(
            pdf=pdf,
            pngs=pngs if isinstance(pngs, list) else [pngs],
            texts=page_texts(pdf),
            warnings=[warning.message for warning in warnings],
            abstract=" ".join(plain(abstract[0]["value"]).split()) if abstract else "",
            missing=[
                f"{m['value']['key']}: {m['value']['why']}" for m in query("<missing>")
            ],
            claims=[(c["value"]["ok"], c["value"]["text"]) for c in query("<claim>")],
            todos=len(query("<todo>")),
            references=(references[0]["value"]["page"], references[0]["value"]["y"])
            if references
            else None,
            figures=[
                label(f) if "label" in f else plain(f["caption"]["body"])
                for f in figures
            ],
            refs={ref["target"].strip("<>") for ref in query("ref")},
            cites={cite["key"].strip("<>") for cite in query("cite")},
            headings=[plain(heading["body"]) for heading in query("heading")],
            links=[
                link["dest"] for link in query("link") if isinstance(link["dest"], str)
            ],
            misplaced=[
                (
                    m["value"]["label"] or plain(m["value"]["caption"]),
                    plain(m["value"]["written"]),
                    plain(m["value"]["drawn"]),
                )
                for m in query("<misplaced>")
            ],
            height=height,
            top=top,
            bottom=bottom,
        )
    except typst.TypstError as error:
        raise BuildError(error.diagnostic or error.message) from None
    return typeset
