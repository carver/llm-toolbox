"""build-paper: builds the project's paper/paper.typ as a draft, or with --final the submission (README.md)."""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from paperkit.config import ConfigError
from paperkit.data import Source
from paperkit.fit import report as fit_report
from paperkit.git import Repo
from paperkit.lookup import shown

# The build imports Typst and matplotlib, which find, keys and vendor don't need
if TYPE_CHECKING:
    from paperkit.build import Report

SHOWN_CHANGES = 12  # build/changes.md lists them all


def project_paper() -> Path:
    """paper/ in the project: under the git repo's root holding the working directory, else under it."""
    repo = Repo.of(Path.cwd())
    return (repo.root if repo else Path.cwd()) / "paper"


def status(report: "Report", final: bool) -> str:
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
        new = (
            f", {len(report.new)} new since the last build"
            if report.new is not None
            else ""
        )
        lines += ["", f"{len(report.problems)} problems{new} ({verdict}):"]
        lines += [
            f"  {'[new] ' if report.new and problem in report.new else ''}{problem.check}: {problem.detail}"
            for problem in report.problems
        ]
        if report.over_cap:
            lines += ["", *fit_report(report.over_cap)]
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


def main_tree_refusal(paper_dir: Path, here: bool) -> str | None:
    """Why an agent shouldn't build here: in a repo's main worktree, where the user's `build-paper watch`
    runs, two builds can collide (a half-written numbers.json, a viewer that reloads mid-write)."""
    repo = Repo.of(paper_dir)
    if (
        here
        or not os.environ.get("AI_AGENT")
        or repo is None
        or not repo.is_main_worktree()
    ):
        return None
    return (
        f"build-paper: {repo.root} is the repo's main worktree, and $AI_AGENT is set. An agent builds the "
        "paper in a worktree of its own, so its build can't collide with the user's `build-paper watch` "
        f"here:\n  git worktree add --detach ../{repo.root.name}-wt-paper\nthen build there. --here builds "
        "here anyway."
    )


def build_main(
    argv: list[str], paper_dir: Path, sources: Mapping[str, Source] | None
) -> int:
    parser = argparse.ArgumentParser(
        prog="build-paper",
        description="Build the project's paper/paper.typ: fill in its numbers, tables and figures from the "
        "data sources in paper/data/, compile it, and check it against the rules paper/paper.toml turns on. "
        "Writes paper/build/DRAFT-paper.pdf, a PNG of each page in paper/build/pages/, and the ledger in "
        "paper/build/numbers.json and numbers.md (paperkit's README.md). Other commands, each with its "
        "own -h: `build-paper watch` rebuilds the draft as the paper changes; `build-paper find TEXT` lists "
        "the pages holding TEXT; `build-paper keys PREFIX...` prints ledger keys; `build-paper vendor` copies "
        "paperkit into the project, and `vendor --check` lists where that copy differs from the toolbox's.",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="the submission: every check must pass, every link resolve and the tree be clean (and, with "
        "paper.toml's logs_dir, the logs pushed); only then writes the unmarked paper/<final_name>.pdf and "
        "its ledger",
    )
    parser.add_argument(
        "--here",
        action="store_true",
        help="build even in the repo's main worktree when $AI_AGENT is set (an agent builds in a "
        "worktree of its own by default, so its build can't collide with the user's `build-paper watch`)",
    )
    args = parser.parse_args(argv)
    if not (paper_dir / "paper.typ").is_file():
        print(f"build-paper: no paper/paper.typ in {paper_dir.parent}", file=sys.stderr)
        return 1
    if refusal := main_tree_refusal(paper_dir, args.here):
        print(refusal, file=sys.stderr)
        return 1
    from paperkit.build import BuildError, build

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
    if argv[:1] in (["find"], ["keys"]):
        from paperkit.lookup import find_main, keys_main

        return (find_main if argv[0] == "find" else keys_main)(argv[1:], paper_dir)
    if argv[:1] == ["vendor"]:
        from paperkit.vendor import main as vendor_main

        return vendor_main(argv[1:], paper_dir.parent)
    return build_main(argv, paper_dir, sources)


if __name__ == "__main__":
    sys.exit(main())
