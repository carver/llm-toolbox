"""paper/paper.toml: which of the optional rules a build checks, and the names it writes. Every rule is off
until its key is set; a key paperkit doesn't know is an error, so a misspelt rule can't go quietly unchecked.

    page_cap = 8                       # pages before References, the title block included
    abstract_words = [150, 250]        # the abstract's length, in words
    required_sections = ["Code and Data", "LLM Usage Statement"]
    after_references = ["Limitations"] # sections that must come after References (appendices)
    template = "apart.typ"             # the project's own template, from paper/ (else paperkit's)
    citations_read_check = true        # every cited source's row in citations.md says `full text`
    logs_dir = "logs"                  # --final needs each log under it that a number came from
                                       # pushed to `upstream`
    upstream = "origin/main"           # the branch --final checks the logs against
    final_name = "paper"               # --final writes paper/<final_name>.pdf and its ledger
    max_pdf_mb = 25                    # the PDF's size cap, in MB
    cell = ["model", "prompt_hash"]    # fields one cell's rows share: a rate refuses rows across cells
    figure_font = "Old Standard TT"    # the figures' font; paper/fonts/ holds fonts for Typst and figures
"""

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import tomllib


class ConfigError(ValueError):
    """paper.toml has a key paperkit doesn't know, or a value of the wrong type."""


@dataclass(frozen=True)
class Config:
    page_cap: int | None = None
    abstract_words: tuple[int, int] | None = None
    required_sections: tuple[str, ...] = ()
    after_references: tuple[str, ...] = ()
    template: str | None = None
    citations_read_check: bool = False
    logs_dir: str | None = None
    upstream: str = "origin/main"
    final_name: str = "paper"
    max_pdf_mb: float | None = None
    cell: tuple[str, ...] = ()
    figure_font: str | None = None


def _checked(name: str, value: Any) -> Any:
    """`value` in the type Config holds for `name`, or ConfigError."""
    match name, value:
        case "page_cap", int() if not isinstance(value, bool):
            return value
        case "abstract_words", [int() as low, int() as high]:
            return (low, high)
        case "required_sections" | "after_references" | "cell", list() if all(
            isinstance(item, str) for item in value
        ):
            return tuple(value)
        case "citations_read_check", bool():
            return value
        case "max_pdf_mb", int() | float() if not isinstance(value, bool):
            return float(value)
        case "template" | "logs_dir" | "upstream" | "final_name" | "figure_font", str():
            return value
    raise ConfigError(
        f"paper.toml: {name} = {value!r} isn't the right kind of value (see paperkit/config.py)"
    )


def load(paper_dir: Path) -> Config:
    """<paper_dir>/paper.toml, or every default when there is none."""
    path = paper_dir / "paper.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"paper.toml: {error}") from None
    known = {field.name for field in fields(Config)}
    if unknown := sorted(raw.keys() - known):
        raise ConfigError(
            f"paper.toml: unknown key {', '.join(unknown)} (known: {', '.join(sorted(known))})"
        )
    return Config(**{name: _checked(name, value) for name, value in raw.items()})
