"""build-paper: builds the project's paper/paper.typ as a draft, or with --final the submission (README.md)."""

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from paperkit.build import BuildError, Report, build
from paperkit.config import ConfigError
from paperkit.data import Source
from paperkit.git import Repo

SHOWN_CHANGES = 12  # build/changes.md lists them all


def project_paper() -> Path:
    """paper/ in the project: under the git repo's root holding the working directory, else under it."""
    repo = Repo.of(Path.cwd())
    return (repo.root if repo else Path.cwd()) / "paper"


def shown(path: Path) -> str:
    """`path` from the working directory when it is under it."""
    return (
        str(path.relative_to(Path.cwd()))
        if path.is_relative_to(Path.cwd())
        else str(path)
    )


def status(report: Report, final: bool) -> str:
    """The block a build ends with: the paper's measures, what changed, its problems and its warnings."""
    config = report.config
    folder = shown(report.pages[0].parent) if report.pages else "no pages"
    head = (
        f"{shown(report.pdf)}: {len(report.pages)} pages"
        if report.pdf
        else "Final build refused"
    )
    cap = f" (cap {config.page_cap})" if config.page_cap is not None else ""
    size = f" (cap {config.max_pdf_mb:g} MB)" if config.max_pdf_mb is not None else ""
    lines = [f"{head} (pictures of each page in {folder})"]
    if report.body_pages is not None or config.page_cap is not None:
        lines.append(f"  pages before References  {report.body_pages}{cap}")
    if config.abstract_words is not None:
        low, high = config.abstract_words
        lines.append(
            f"  abstract                 {report.abstract_words} words ({low}-{high})"
        )
    lines += [
        f"  TODO markers             {report.todos}",
        f"  missing numbers          {len(report.missing)}",
        f"  failed claims            {len(report.failed_claims)}",
        f"  unreferenced figures     {len(report.unreferenced)}",
    ]
    if config.required_sections or config.after_references:
        lines.append(
            f"  missing sections         {', '.join(report.missing_sections) or 'none'}"
        )
    lines += [
        f"  PDF size                 {report.size / 1e6:.1f} MB{size}",
        f"  changed since last build {len(report.changed)}",
        *(f"    {change}" for change in report.changed[:SHOWN_CHANGES]),
        *(
            [f"    and {len(report.changed) - SHOWN_CHANGES} more, in build/changes.md"]
            if len(report.changed) > SHOWN_CHANGES
            else []
        ),
    ]
    if report.problems:
        verdict = (
            "nothing written" if final else "a draft lists them; --final refuses them"
        )
        lines += ["", f"{len(report.problems)} problems ({verdict}):"]
        lines += [f"  {problem.check}: {problem.detail}" for problem in report.problems]
    elif final and report.pdf:
        name = config.final_name
        lines += [
            "",
            f"Wrote {shown(report.pdf)} and {name}-numbers.json beside it. Commit both.",
        ]
    if report.warnings:
        count = (
            f"{len(report.warnings)} warning{'' if len(report.warnings) == 1 else 's'}"
        )
        lines += ["", f"{count} (neither a draft nor --final refuses them):"]
        lines += [f"  {warning}" for warning in report.warnings]
    return "\n".join(lines)


def build_main(
    argv: list[str], paper_dir: Path, sources: Mapping[str, Source] | None
) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper",
        description="Build the project's paper/paper.typ: fill in its numbers, tables and figures from the "
        "data sources in paper/data/, compile it, and check it against the rules paper/paper.toml turns on. "
        "Writes paper/build/DRAFT-paper.pdf, a PNG of each page in paper/build/pages/, and the ledger in "
        "paper/build/numbers.json and numbers.md (paperkit's README.md). Other commands, each with its "
        "own -h: `build-paper watch` rebuilds the draft as the paper changes.",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="the submission: every check must pass, every link resolve and the tree be clean (and, with "
        "paper.toml's logs_dir, the logs pushed); only then writes the unmarked paper/<final_name>.pdf and "
        "its ledger",
    )
    args = parser.parse_args(argv)
    try:
        report = build(paper_dir, sources, final=args.final)
    except ConfigError as error:
        print(f"build-paper: {error}", file=sys.stderr)
        return 1
    except BuildError as error:
        print(
            f"build-paper: Typst couldn't compile paper.typ:\n{error}", file=sys.stderr
        )
        return 1
    print(status(report, args.final))
    return 1 if args.final and report.problems else 0


def main(
    argv: list[str] | None = None,
    paper_dir: Path | None = None,
    sources: Mapping[str, Source] | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    paper_dir = project_paper() if paper_dir is None else paper_dir
    if argv[:1] == ["watch"]:
        from paperkit.watch import main as watch_main

        return watch_main(argv[1:], paper_dir)
    return build_main(argv, paper_dir, sources)


if __name__ == "__main__":
    sys.exit(main())
