"""bin/build-paper, run in throwaway projects: numbers come from the project's data source, a draft lists
every problem and exits 0, each optional rule runs only when paper.toml sets it, and only a clean --final
build, from a pushed commit whose links all resolve, writes the submission."""

import hashlib
import http.server
import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from conftest import without_repo_vars
from paperkit_testing import LIB, build_paper, git, make_project, pdf_text, write_log

# A paper with one problem of every kind: a missing number, an interval asked of a pooled count, a false
# claim, a claim on a missing key, a TODO, an unreferenced figure, Limitations before References, no LLM
# Usage Statement, an abstract of 8 words, 10 pages before References, three links that don't resolve, a
# source cited though only its catalogue entry was read, and a data source that fails.
FLAWED = """#import "build/paperkit.typ": *
#show: paper.with(
  title: [Fixture],
  authors: ((name: [Ada], affiliation: [Unaffiliated]),),
  abstract: [Same arm: #n("demo.same"), #pct("demo.same"), #ci("demo.same"). Missing: #n("demo.gone").],
)
= Introduction
Prior work #cite(<calvano2020>, form: "prose"). Pooled: #n("demo.pooled"), #ci("demo.pooled").
#claim("half or more land on the cartel", ("demo.same",), r => r >= 0.5)
#claim("few land on the cartel", ("demo.same",), r => r < 0.5)
#claim("the gone arm is lower", ("demo.gone", "demo.same"), (gone, same) => gone < same)
#todo[say why]
See @tab-demo and #link("{url}/gone")[a dead link], #link("http://127.0.0.1:9/")[a closed port] and
#link("https://example.com/a b")[a malformed link].
#figure(data-table("demo-table"), caption: [A table.]) <tab-demo>
#figure(rect(width: 50%, height: 1cm), caption: [A figure.]) <fig-demo>
= Limitations
#for _ in range(9) {{ pagebreak() }}
#unnumbered[Code and Data]
#references("refs.bib")
"""

CLEAN = """#import "build/paperkit.typ": *
#show: paper.with(
  title: [Fixture],
  authors: ((name: [Ada], affiliation: [Unaffiliated]),),
  abstract: [#n("demo.same") #("word " * 160)],
)
= Introduction
Prior work @calvano2020.
#claim("half or more land on the cartel", ("demo.same",), r => r >= 0.5)
See @tab-demo, @fig-demo and #link("{url}/ok")[a live link].
#figure(data-table("demo-table"), caption: [A table.]) <tab-demo>
#figure(rect(width: 50%, height: 1cm), caption: [A figure.]) <fig-demo>
#unnumbered[Code and Data]
#unnumbered[LLM Usage Statement]
#pagebreak()
#references("refs.bib")
#show: appendix
= Limitations
"""

# CLEAN with a float too tall for what's left of page 1, written at the end of the Introduction just before a
# new section: Typst draws it at the top of page 2, past that section's heading, which stays on page 1.
TALL = (
    "#figure(placement: auto, rect(width: 100%, height: 20cm), caption: [Tall.]) <fig-tall>\n"
    "See @fig-tall.\n"
    "= Background\n"
)
MISPLACED = CLEAN.replace(
    "Prior work @calvano2020.\n", "Prior work @calvano2020.\n" + TALL
)

# CLEAN with a section that starts partway down a later page and holds a figure: drawn where it's written.
MIDPAGE = CLEAN.replace(
    "#unnumbered[Code and Data]",
    "= Background\nMore.\n#figure(rect(width: 100%, height: 2cm), caption: [Low.]) <fig-low>\n"
    "See @fig-low.\n#unnumbered[Code and Data]",
)

# CLEAN with a reference to each kind of heading, each ending a sentence, beside a figure's reference.
REFERENCES = (
    CLEAN.replace(
        "Prior work @calvano2020.\n",
        "Prior work @calvano2020.\n== Scope <sec-scope>\n"
        "One: @sec-intro. Two: @sec-scope. Three: @sec-intro[Part]. Four: @app-lim. Five: @fig-demo.\n"
        "Six: @sec-scope[the scope]. Seven: @code.\n",
    )
    .replace("= Introduction\n", "= Introduction <sec-intro>\n")
    .replace("#unnumbered[Code and Data]", "#unnumbered[Code and Data] <code>")
    .replace("= Limitations\n", "= Limitations <app-lim>\n")
)


@pytest.fixture(scope="module")
def server():
    """A local web server: /ok answers 200, anything else 404."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/ok" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as httpd:
        # A 10 ms shutdown poll, not the default half second
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_port}"
        httpd.shutdown()


@pytest.fixture(scope="module")
def flawed(tmp_path_factory, server):
    root = tmp_path_factory.mktemp("flawed") / "repo"
    project = make_project(root, FLAWED, url=server, read="catalogue", broken=True)
    return project, build_paper(project)


@pytest.fixture
def clean(tmp_path, server):
    return make_project(tmp_path / "repo", CLEAN, url=server)


def problems(out: str) -> list[str]:
    """The problem lines of a build's status block, without their [new] marks."""
    lines = out.split("\n")
    start = next(
        (i for i, line in enumerate(lines) if re.match(r"\d+ problems[ ,]", line)),
        len(lines),
    )
    listed = lines[start + 1 :]
    listed = listed[: listed.index("") if "" in listed else None]
    return [line.strip().removeprefix("[new] ") for line in listed]


def test_a_draft_is_named_draft_draws_no_page_pictures_unless_asked_and_exits_0(flawed):
    project, result = flawed
    assert result.returncode == 0, result.stderr
    draft = project / "paper" / "build" / "DRAFT-paper.pdf"
    # Code and Data and References share the last page
    assert result.stdout.startswith(
        "paper/build/DRAFT-paper.pdf: 10 pages (no pictures of them; "
    )
    assert "--pages" in result.stdout.splitlines()[0]
    assert draft.read_bytes().startswith(b"%PDF")
    assert not (project / "paper" / "build" / "pages").exists()
    assert not (project / "paper" / "submission.pdf").exists()


def pictures(folder: Path) -> list[Path]:
    return sorted(folder.glob("page-*.png"))


def test_pages_draws_a_picture_of_every_page(two_pages):
    pages = pictures(two_pages / "paper" / "build" / "pages")
    assert [page.name for page in pages] == ["page-01.png", "page-02.png"]
    assert all(page.read_bytes().startswith(b"\x89PNG") for page in pages)


def test_a_build_without_pages_keeps_the_last_pictures_as_pages_old(tmp_path):
    project = make_project(tmp_path / "repo", TWO_PAGES, toml=None, origin=False)
    build = project / "paper" / "build"
    build_paper(project, "--pages")
    first = [page.read_bytes() for page in pictures(build / "pages")]
    build_paper(project)
    assert not (build / "pages").exists()
    assert [page.read_bytes() for page in pictures(build / "pages.old")] == first
    build_paper(project)  # no pages/ to move this time: pages.old stays
    assert len(pictures(build / "pages.old")) == 2
    result = build_paper(project, "find", "second page")
    assert result.stdout == "page 2\n"  # no picture to name
    build_paper(project, "--pages")
    again = build_paper(project)  # moves these pictures over the older pages.old
    assert again.returncode == 0, again.stderr
    assert len(pictures(build / "pages.old")) == 2


# A paper that sets the figure source's figure: the only build here that draws one.
FIGURED = """#import "build/paperkit.typ": *
#show: paper.with(abstract: [x])
#figure(data-figure("demo-figure"), caption: [The demo bars.]) <fig-bars>
See @fig-bars.
"""


def test_a_figure_a_data_source_draws_is_set_in_the_paper(tmp_path):
    project = make_project(
        tmp_path / "repo", FIGURED, toml=None, origin=False, figure=True
    )
    result = build_paper(project)
    assert result.returncode == 0, result.stderr
    svg = project / "paper" / "build" / "figures" / "demo-figure.svg"
    assert "<svg" in svg.read_text()
    # The bar's label: an SVG's text stays text, so it is in the PDF only if the figure is
    assert "bar-label" in pdf_text(project / "paper" / "build" / "DRAFT-paper.pdf")
    assert "demo-figure" in build_paper(project, "keys", "demo-").stdout


def test_numbers_are_filled_in_from_the_project_s_data_source(flawed):
    project, _ = flawed
    text = pdf_text(project / "paper" / "build" / "DRAFT-paper.pdf")
    assert "Same arm: 5/10, 50%, 50% (24–76%)." in text
    assert "[demo.gone?]" in text


def test_a_draft_lists_every_problem(flawed):
    found = problems(flawed[1].stdout)
    assert {
        "missing: demo.gone: not in the ledger",
        "missing: demo.pooled: pooled across cells, so it has no interval",
        "claim: few land on the cartel",
        "claim: the gone arm is lower (can't be checked: a key isn't in the ledger)",
        "todo: 1 TODO markers",
        "unreferenced: fig-demo",
        "sections: no LLM Usage Statement section",
        "sections: Limitations comes before References; it must be an appendix",
        "page-cap: 10 pages before References (cap 8)",
        "abstract: 8 words (needs 150-250)",
        "citation: @calvano2020 is cited, but citations.md says only its catalogue was read",
        "source: broken: RuntimeError: its log is missing",
    } <= set(found)
    # Only --final checks links
    assert not [line for line in found if line.startswith("link:")]


def test_a_broken_source_is_reported_and_the_others_still_fill_the_ledger(flawed):
    project, result = flawed
    assert "source: broken: RuntimeError: its log is missing" in problems(result.stdout)
    assert "Same arm: 5/10" in pdf_text(project / "paper" / "build" / "DRAFT-paper.pdf")


def test_the_status_block_shows_each_configured_measure(flawed):
    out = flawed[1].stdout
    assert "  pages before References  10 (cap 8)\n" in out
    assert "  abstract                 8 words (150-250)\n" in out
    assert "  missing sections         LLM Usage Statement\n" in out
    assert " MB (cap 25 MB)\n" in out


def test_the_ledger_says_which_logs_each_number_came_from(flawed):
    project, _ = flawed
    entry = json.loads((project / "paper" / "build" / "numbers.json").read_text())[
        "demo.same"
    ]
    assert entry["source"] == {
        "module": "paper.data.demo",
        "function": "demo",
        "logs": ["logs/demo.jsonl"],
    }
    assert "demo.same" in (project / "paper" / "build" / "numbers.md").read_text()


def test_without_paper_toml_only_the_framework_s_checks_run(tmp_path, server):
    root = tmp_path / "repo"
    project = make_project(
        root, FLAWED, toml=None, url=server, read="catalogue", origin=False
    )
    result = build_paper(project)
    checks = {line.split(":")[0] for line in problems(result.stdout)}
    assert checks == {"missing", "claim", "todo", "unreferenced"}
    assert "abstract  " not in result.stdout
    assert "missing sections" not in result.stdout
    assert "  pages before References  10\n" in result.stdout  # measured, with no cap
    entry = json.loads((project / "paper" / "build" / "numbers.json").read_text())[
        "demo.same"
    ]
    # Recorded without logs_dir too
    assert entry["source"]["logs"] == ["logs/demo.jsonl"]


def test_a_key_paper_toml_doesnt_know_is_an_error(tmp_path):
    project = make_project(
        tmp_path / "repo", CLEAN, toml="page_cap = 8\npagecap = 9\n", origin=False
    )
    result = build_paper(project)
    assert result.returncode == 1
    assert "unknown key pagecap" in result.stderr


def test_a_final_build_with_problems_writes_nothing_and_exits_1(flawed, server):
    project, _ = flawed
    result = build_paper(project, "--final")
    assert result.returncode == 1
    assert result.stdout.startswith("Final build refused")
    assert "nothing written" in result.stdout
    links = [line for line in problems(result.stdout) if line.startswith("link:")]
    assert len(links) == 3
    assert f"link: {server}/gone: HTTP 404" in links
    assert any(
        link.startswith("link: http://127.0.0.1:9/: unreachable") for link in links
    )
    assert any(
        link.startswith("link: https://example.com/a b: invalid URL") for link in links
    )
    assert not (project / "paper" / "submission.pdf").exists()


def test_a_clean_final_build_writes_the_submission_and_its_ledger(clean):
    result = build_paper(clean, "--final")
    assert result.returncode == 0, result.stdout + result.stderr
    pdf = clean / "paper" / "submission.pdf"
    assert (
        "Wrote paper/submission.pdf and submission-numbers.json beside it."
        in result.stdout
    )
    # References starts page 2
    assert "  pages before References  1 (cap 8)\n" in result.stdout
    assert (
        f"Built from commit {git(clean, 'rev-parse', 'HEAD').strip()}".encode()
        in pdf.read_bytes()
    )
    assert "demo.same" in json.loads(
        (clean / "paper" / "submission-numbers.json").read_text()
    )


def test_a_final_build_refuses_uncommitted_changes_and_says_what_moved(clean):
    build_paper(clean)
    write_log(clean / "logs" / "demo.jsonl", hits=6, replies=12)
    result = build_paper(clean, "--final")
    assert result.returncode == 1
    assert "dirty: logs/demo.jsonl" in problems(result.stdout)
    assert "    demo.same: 5/10 → 6/12\n" in result.stdout
    assert not (clean / "paper" / "submission.pdf").exists()


def test_a_final_build_refuses_a_log_that_isnt_pushed(clean):
    write_log(clean / "logs" / "demo.jsonl", hits=6, replies=12)
    git(clean, "commit", "-q", "-am", "more replies")
    result = build_paper(clean, "--final")
    assert [
        line for line in problems(result.stdout) if line.startswith("unpushed")
    ] == ["unpushed: logs/demo.jsonl"]


def test_a_final_build_checks_the_paper_s_repo_whatever_git_variables_are_set(
    clean, tmp_path
):
    # Git hooks and `git rebase --exec` export GIT_DIR, which would point git at another repo.
    decoy = tmp_path / "decoy"
    git(tmp_path, "init", "-q", str(decoy))
    # From a folder inside the repo, where git with GIT_DIR set would take the folder for the top
    result = build_paper(
        clean / "logs", "--final", env={"GIT_DIR": str(decoy / ".git")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clean / "paper" / "submission.pdf").exists()


def test_changes_count_from_the_last_build_that_compiled(clean):
    build_paper(clean)
    paper = clean / "paper" / "paper.typ"
    text = paper.read_text()
    paper.write_text(text + "#nonsense\n")
    write_log(clean / "logs" / "demo.jsonl", hits=6, replies=12)
    assert build_paper(clean).returncode == 1
    paper.write_text(text)
    result = build_paper(clean)
    assert "    demo.same: 5/10 → 6/12\n" in result.stdout
    changes = (clean / "paper" / "build" / "changes.md").read_text()
    assert "- demo.same: 5/10 → 6/12\n" in changes


def test_a_paper_typst_cannot_compile_fails_with_its_error(tmp_path):
    project = make_project(
        tmp_path / "repo", '#import "build/paperkit.typ": *\n#nonsense\n', origin=False
    )
    result = build_paper(project)
    assert result.returncode == 1
    assert "nonsense" in result.stderr


def test_a_swallowed_semicolon_and_hash_are_problems_and_unclaimed_wording_a_warning(
    tmp_path,
):
    text = (
        '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\n'
        'Every model: #n("demo.same"); then more.\nSee (#39).\n'
    )
    result = build_paper(make_project(tmp_path / "repo", text, toml=None, origin=False))
    assert (
        'semicolon: line 3: #n("demo.same"); drops its ";" (write \\; to keep it)'
        in problems(result.stdout)
    )
    assert "hash: line 4: #39 renders as 39 (write \\#39)" in problems(result.stdout)
    assert (
        "1 warning (neither a draft nor --final refuses them):\n  line 3: every, with no #claim after it"
        in (result.stdout)
    )


def test_a_figure_drawn_under_another_section_than_its_own_is_a_problem(
    tmp_path, server
):
    result = build_paper(
        make_project(tmp_path / "repo", MISPLACED, url=server, origin=False)
    )
    assert [
        line for line in problems(result.stdout) if line.startswith("placement")
    ] == [
        "placement: fig-tall is written under Introduction but drawn under Background"
    ]


def test_a_figure_under_a_section_that_starts_partway_down_a_page_is_where_it_belongs(
    tmp_path, server
):
    result = build_paper(
        make_project(tmp_path / "repo", MIDPAGE, url=server, origin=False)
    )
    assert result.returncode == 0, result.stderr
    assert [
        line for line in problems(result.stdout) if line.startswith("placement")
    ] == []


def test_a_reference_to_a_section_or_subsection_prints_its_number_or_its_title(
    tmp_path, server
):
    project = make_project(tmp_path / "repo", REFERENCES, url=server, origin=False)
    build_paper(project)
    text = pdf_text(project / "paper" / "build" / "DRAFT-paper.pdf")
    assert (
        "One: Section 1. Two: Scope. Three: Part 1. Four: Section A. Five: Figure 1."
        in text
    )
    assert "Six: the scope. Seven: Code and Data." in text


def test_a_project_template_named_in_paper_toml_lays_the_paper_out(tmp_path):
    template = (
        "#let paper(..args, body) = { [MY TEMPLATE]; body }\n"
        "#let unnumbered(title) = heading(numbering: none, title)\n"
        "#let appendix(body) = body\n"
    )
    project = make_project(
        tmp_path / "repo", CLEAN, toml='template = "mine.typ"\n', origin=False
    )
    (project / "paper" / "mine.typ").write_text(template)
    result = build_paper(project)
    assert result.returncode == 0, result.stderr
    assert "MY TEMPLATE" in pdf_text(project / "paper" / "build" / "DRAFT-paper.pdf")


def test_vendor_copies_paperkit_and_a_launcher_into_the_project(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    assert build_paper(project, "vendor").returncode == 0
    copied = {
        p.relative_to(project / "paperkit")
        for p in (project / "paperkit").rglob("*.py")
    }
    assert copied == {
        p.relative_to(LIB / "paperkit") for p in (LIB / "paperkit").rglob("*.py")
    }
    assert (project / "paperkit" / "lib.typ").read_bytes() == (
        LIB / "paperkit" / "lib.typ"
    ).read_bytes()
    check = build_paper(project, "vendor", "--check")
    assert (check.returncode, check.stdout) == (
        0,
        "paperkit/ and bin/build-paper match the toolbox's\n",
    )


def test_the_vendored_launcher_builds_the_paper_with_the_project_s_copy(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    marked = (
        (project / "paperkit" / "__main__.py")
        .read_text()
        .replace(
            "    print(status(report, args.final))",
            "    print('VENDORED COPY')\n    print(status(report, args.final))",
        )
    )
    (project / "paperkit" / "__main__.py").write_text(marked)
    env = {k: v for k, v in without_repo_vars(os.environ).items() if k != "AI_AGENT"}
    own = subprocess.run(
        [str(project / "bin" / "build-paper")],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert own.returncode == 0, own.stderr
    assert own.stdout.startswith("VENDORED COPY\npaper/build/DRAFT-paper.pdf: ")
    # The toolbox's launcher also takes the project's copy
    toolbox = build_paper(project)
    assert toolbox.stdout.startswith("VENDORED COPY\n")
    # No bytecode caches, which --final would count as uncommitted changes in a repo not ignoring them
    assert not list(project.rglob("__pycache__"))


def manifest(project: Path) -> dict:
    return json.loads((project / "paperkit" / "VENDORED").read_text())


def vendored_older(project: Path, name: str, text: str | None = None) -> None:
    """Makes the copy look vendored from a toolbox whose `name` differed: its manifest's hash of it no longer
    matches the toolbox's, and with `text` the copy's file holds that older text, as vendor wrote it."""
    record = manifest(project)
    if text is None:
        record["files"][name] = "0" * 64
    else:
        (project / name).write_text(text)
        record["files"][name] = hashlib.sha256(text.encode()).hexdigest()
    (project / "paperkit" / "VENDORED").write_text(json.dumps(record))


def toolbox_commit() -> str:
    """The toolbox's commit, or how vendor names it when the toolbox is no git checkout (as in the commit
    hook's copy of the index)."""
    run = subprocess.run(
        ["git", "-C", str(LIB), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return run.stdout.strip() if run.returncode == 0 else "an unknown commit"


def test_vendor_records_the_toolbox_commit_and_a_hash_of_each_file_it_wrote(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    record = manifest(project)
    assert record["from"].startswith(
        f"llm-toolbox {toolbox_commit()}"
    )  # perhaps "with uncommitted changes"
    launcher = (project / "bin" / "build-paper").read_bytes()
    assert record["files"]["bin/build-paper"] == hashlib.sha256(launcher).hexdigest()
    assert "paperkit/build.py" in record["files"]
    assert "paperkit/VENDORED" not in record["files"]


def test_a_build_with_an_edited_copy_warns_to_upstream_each_edit(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    assert "upstream" not in build_paper(project).stdout
    with (project / "paperkit" / "fit.py").open("a") as fit:
        fit.write("# a collaborator's fix\n")
    (project / "paperkit" / "extra.py").write_text("")
    result = build_paper(project)
    assert result.returncode == 0, result.stderr
    since = f"since it was vendored from {manifest(project)['from']}"
    upstream = "upstream the change to llm-toolbox's lib/paperkit/ and vendor again"
    assert (
        f"  paperkit/extra.py: added {since}; {upstream}" in result.stdout.splitlines()
    )
    assert (
        f"  paperkit/fit.py: edited {since}; {upstream}" in result.stdout.splitlines()
    )


def test_vendor_refuses_to_replace_a_local_edit_and_offers_no_way_round(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "paperkit" / "fit.py").write_text("# a collaborator's fix\n")
    result = build_paper(project, "vendor")
    assert result.returncode == 1
    assert "paperkit/fit.py" in result.stderr and "upstream" in result.stderr
    assert (project / "paperkit" / "fit.py").read_text() == "# a collaborator's fix\n"
    assert "--force" not in build_paper(project, "vendor", "-h").stdout


def test_vendor_replaces_a_copy_whose_edits_are_already_upstream(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    vendored_older(
        project, "paperkit/build.py"
    )  # the copy now holds what the toolbox does
    assert build_paper(project, "vendor").returncode == 0
    built = (project / "paperkit" / "build.py").read_bytes()
    assert (
        manifest(project)["files"]["paperkit/build.py"]
        == hashlib.sha256(built).hexdigest()
    )


def test_vendor_check_tells_edits_in_the_project_from_a_copy_behind_the_toolbox(
    tmp_path,
):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    vendored_older(project, "paperkit/typeset.py", "# an older typeset\n")
    (project / "paperkit" / "build.py").write_text("# edited\n")
    (project / "paperkit" / "extra.py").write_text("")
    (project / "paperkit" / "template.typ").unlink()
    check = build_paper(project, "vendor", "--check")
    assert check.returncode == 1
    assert check.stdout.splitlines() == [
        "paperkit/build.py: edited in the project",
        "paperkit/extra.py: added in the project",
        "paperkit/template.typ: removed in the project",
        "paperkit/typeset.py: behind the toolbox's",
    ]


def test_an_edited_copy_still_builds_the_final_paper(clean):
    build_paper(clean, "vendor")
    with (clean / "paperkit" / "fit.py").open("a") as fit:
        fit.write("# a collaborator's fix\n")
    git(clean, "add", "-A")
    git(clean, "commit", "-q", "-m", "vendor, and a fix")
    git(clean, "push", "-q", "origin", "main")
    result = build_paper(clean, "--final")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clean / "paper" / "submission.pdf").exists()
    assert "paperkit/fit.py: edited since it was vendored" in result.stdout


def test_a_broken_manifest_is_a_warning_and_vendor_replaces_the_copy(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "paperkit" / "VENDORED").write_text(
        "<<<<<<< HEAD\n"
    )  # a merge conflict, say
    result = build_paper(project)
    assert result.returncode == 0, result.stderr
    assert any(
        line.startswith("  paperkit/VENDORED: unreadable")
        for line in result.stdout.splitlines()
    )
    assert build_paper(project, "vendor").returncode == 0
    assert "upstream" not in build_paper(project).stdout


def test_line_endings_a_checkout_changed_are_no_edit(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    fit = project / "paperkit" / "fit.py"
    fit.write_bytes(
        fit.read_bytes().replace(b"\n", b"\r\n")
    )  # git's autocrlf, on Windows
    assert "upstream" not in build_paper(project).stdout
    assert build_paper(project, "vendor").returncode == 0


def test_vendor_restores_a_file_removed_in_the_project(tmp_path):
    """A removal holds nothing to upstream, so it doesn't stop vendor: the launcher, say."""
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "bin" / "build-paper").unlink()
    (project / "paperkit" / "fit.py").unlink()
    assert build_paper(project, "vendor").returncode == 0
    assert (project / "bin" / "build-paper").exists() and (
        project / "paperkit" / "fit.py"
    ).exists()


def test_a_refused_vendor_says_an_edit_can_also_be_undone(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "paperkit" / "notes.py").write_text("# a file the toolbox never had\n")
    result = build_paper(project, "vendor")
    assert result.returncode == 1
    assert "paperkit/notes.py" in result.stderr and "undo" in result.stderr


def test_vendor_check_without_a_manifest_compares_with_the_toolbox(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "paperkit" / "VENDORED").unlink()
    (project / "paperkit" / "build.py").write_text("# edited\n")
    (project / "paperkit" / "extra.py").write_text("")
    (project / "paperkit" / "template.typ").unlink()
    check = build_paper(project, "vendor", "--check")
    assert check.returncode == 1
    assert check.stdout.splitlines() == [
        "paperkit/extra.py: only in the project",
        "paperkit/template.typ: missing from the project",
        "paperkit/build.py: differs",
    ]


def test_vendor_replaces_a_copy_without_a_manifest_naming_what_differed(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    build_paper(project, "vendor")
    (project / "paperkit" / "VENDORED").unlink()  # vendored before there were manifests
    (project / "paperkit" / "fit.py").write_text("# maybe an edit\n")
    result = build_paper(project, "vendor")
    assert result.returncode == 0, result.stderr
    assert "paperkit/fit.py" in result.stdout
    assert (project / "paperkit" / "fit.py").read_bytes() == (
        LIB / "paperkit" / "fit.py"
    ).read_bytes()


def test_vendor_leaves_a_build_paper_it_didnt_write_alone(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    (project / "bin").mkdir()
    (project / "bin" / "build-paper").write_text("#!/bin/sh\necho mine\n")
    result = build_paper(project, "vendor")
    assert result.returncode != 0
    assert "the project's own" in result.stderr
    assert (project / "bin" / "build-paper").read_text() == "#!/bin/sh\necho mine\n"
    assert not (project / "paperkit").exists()


def test_vendor_check_without_a_copy_is_drift(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    check = build_paper(project, "vendor", "--check")
    assert (check.returncode, check.stdout) == (1, "paperkit/: not in the project\n")


def test_an_agent_s_build_in_the_main_worktree_is_refused_naming_the_worktree_to_use(
    tmp_path,
):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    result = build_paper(project, env={"AI_AGENT": "claude-code"})
    assert result.returncode == 1
    assert "git worktree add --detach ../repo-wt-paper" in result.stderr
    assert "--here" in result.stderr
    assert not (project / "paper" / "build" / "DRAFT-paper.pdf").exists()


def test_a_build_in_a_repo_with_no_paper_says_so(tmp_path):
    (tmp_path / "repo").mkdir()
    git(tmp_path / "repo", "init", "-q")
    for env in ({}, {"AI_AGENT": "claude-code"}):
        result = build_paper(tmp_path / "repo", env=env)
        assert result.returncode == 1
        assert (
            result.stderr == f"build-paper: no paper/paper.typ in {tmp_path / 'repo'}\n"
        )


def test_watch_is_not_refused_in_the_main_worktree_and_its_builds_run(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    env = without_repo_vars(os.environ) | {"AI_AGENT": "claude-code"}
    watch = subprocess.Popen(
        [str(LIB.parent / "bin" / "build-paper"), "watch"],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    numbers = project / "paper" / "build" / "numbers.json"
    try:
        for _ in range(300):  # up to a minute, for its first full build
            if numbers.exists():
                break
            try:
                watch.wait(timeout=0.2)
                break  # it exited: refused
            except subprocess.TimeoutExpired:
                pass
        assert watch.poll() is None, watch.communicate()[0].decode()
        # Its full build, which runs with $AI_AGENT too, wasn't refused either
        assert numbers.exists()
    finally:
        os.killpg(watch.pid, 15)  # SIGTERM, to the watch and its build
        watch.wait(timeout=10)


def test_here_lets_an_agent_build_in_the_main_worktree(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    result = build_paper(project, "--here", env={"AI_AGENT": "claude-code"})
    assert result.returncode == 0, result.stderr
    assert (project / "paper" / "build" / "DRAFT-paper.pdf").exists()


def test_an_agent_builds_in_a_linked_worktree(tmp_path):
    project = make_project(tmp_path / "repo", CLEAN, origin=False)
    git(project, "worktree", "add", "-q", "--detach", str(tmp_path / "repo-wt-paper"))
    result = build_paper(tmp_path / "repo-wt-paper", env={"AI_AGENT": "claude-code"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "repo-wt-paper" / "paper" / "build" / "DRAFT-paper.pdf").exists()


# Two pages, each with words of its own.
TWO_PAGES = """#import "build/paperkit.typ": *
#show: paper.with(abstract: [The first page's abstract.])
= Introduction
Collusion without communication, cited as #n("demo.same").
#pagebreak()
= Results
The second page holds the results, #pct("demo.same"), and collusion again.
#block(width: 2.2cm, text(hyphenate: true)[Supracompetitive incomprehensibilities])
"""


@pytest.fixture(scope="module")
def two_pages(tmp_path_factory):
    project = make_project(
        tmp_path_factory.mktemp("two") / "repo", TWO_PAGES, toml=None, origin=False
    )
    build_paper(project, "--pages")
    return project


def test_find_names_each_page_holding_the_text_and_its_picture(two_pages):
    result = build_paper(two_pages, "find", "COLLUSION")
    assert (result.returncode, result.stdout) == (
        0,
        "page 1  paper/build/pages/page-01.png\npage 2  paper/build/pages/page-02.png\n",
    )


def test_find_ignores_case_and_whitespace(two_pages):
    result = build_paper(two_pages, "find", "the  second\npage HOLDS")
    assert result.stdout == "page 2  paper/build/pages/page-02.png\n"


def test_find_finds_a_word_hyphenated_across_a_line_break(two_pages):
    text = json.loads((two_pages / "paper" / "build" / "pages.json").read_text())[1]
    assert "-\n" in text  # the fixture's narrow block does break a word
    assert build_paper(two_pages, "find", "incomprehensibilities").stdout.startswith(
        "page 2 "
    )


def test_find_finds_a_number_the_build_filled_in(two_pages):
    assert build_paper(two_pages, "find", "results, 50%").stdout.startswith("page 2 ")


def test_find_with_no_page_holding_the_text_exits_1(two_pages):
    result = build_paper(two_pages, "find", "antitrust")
    assert result.returncode == 1
    assert "no page" in result.stderr


def test_find_before_any_build_says_so(tmp_path):
    project = make_project(tmp_path / "repo", TWO_PAGES, toml=None, origin=False)
    result = build_paper(project, "find", "collusion")
    assert result.returncode == 1
    assert "no build yet" in result.stderr


def test_keys_prints_each_matching_key_s_value_and_rate_on_one_line(two_pages):
    result = build_paper(two_pages, "keys", "demo.", "demo-t")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "demo-table   1 rows",
        "demo.pooled  7/12    58%",
        "demo.same    5/10    50% (24–76%)",
    ]


def test_keys_how_adds_how_each_was_computed_and_its_logs(two_pages):
    result = build_paper(two_pages, "keys", "--how", "demo.same")
    assert result.stdout == (
        "demo.same  5/10  50% (24–76%)  how: cartel answers / replies; from paper.data.demo.demo"
        " logs/demo.jsonl\n"
    )


def test_keys_how_lines_up_how_whether_or_not_a_key_has_a_rate(two_pages):
    lines = build_paper(two_pages, "keys", "--how", "demo").stdout.splitlines()
    assert len(lines) == 3  # the pooled count, the rate and the table
    assert len({line.index("how: ") for line in lines}) == 1


def test_keys_with_no_match_exits_1(two_pages):
    result = build_paper(two_pages, "keys", "nothing.")
    assert result.returncode == 1
    assert "no key starts with nothing." in result.stderr


def over_cap_paper() -> str:
    """Three sentences on page 1, then a page break, a heading and five on page 2 (paper.typ lines 10-14),
    the References heading partway down it. Line 13 repeats line 4 word for word, and line 6 and line 14
    each end their paragraph on a short line. Line 16 is a paragraph of one short line, no widow."""
    lines = [
        '#import "build/paperkit.typ": *',
        "#show: paper.with(abstract: [x])",
        "= Introduction",
    ]
    for n in ("one", "two", "three"):
        lines.append(
            f"Sentence {n} opens the paper with words of its own, about {n} apples and {n} rivers, long "
            "enough to wrap onto a second line of the page."
        )
    lines += ["", "#pagebreak()", "= Results"]
    for n in ("four", "five", "six"):
        lines.append(
            f"Sentence {n} lands past the cap, with words of its own about {n} lamps and {n} bridges, long "
            "enough to wrap onto a second line."
        )
    lines.append(lines[3])
    lines.append(
        "A last sentence ends the paragraph on a word or two, after enough text to fill most of the line "
        "before it, widow."
    )
    lines += ["", "A short one.", "", '#references("refs.bib")']
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def over_cap(tmp_path_factory):
    root = tmp_path_factory.mktemp("over") / "repo"
    return build_paper(
        make_project(root, over_cap_paper(), toml="page_cap = 1\n", origin=False)
    )


def report_lines(out: str, heading: str) -> list[str]:
    """The over-cap report's lines under `heading`, up to the next heading."""
    lines = out.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    rest = lines[start + 1 :]
    end = next(
        (i for i, line in enumerate(rest) if not line.startswith("  ")), len(rest)
    )
    return rest[:end]


def test_a_build_over_the_cap_lists_what_lands_past_it_by_paper_typ_line(over_cap):
    past = report_lines(
        over_cap.stdout, "Past the page cap (2 pages before References, cap 1):"
    )
    assert past[0] == '  page 2  paper.typ:9  "2. Results"'
    assert past[1].startswith(
        '  page 2  paper.typ:10  "Sentence four lands past the cap'
    )
    numbers = [int(line.split("paper.typ:")[1].split()[0]) for line in past]
    assert numbers == sorted(numbers) and set(numbers) == {*range(9, 15), 16}


def test_the_over_cap_report_gives_the_blank_space_at_the_bottom_of_each_page(over_cap):
    (blank,) = report_lines(
        over_cap.stdout, "Blank space at the bottom of each page before References:"
    )
    assert blank.startswith("  page 1 ") and blank.endswith(" pt")
    space = float(blank.removeprefix("  page 1 ").removesuffix(" pt"))
    assert 400 < space < 650  # page 1 holds a heading and three sentences, of 648 pt


def test_the_over_cap_report_lists_short_last_lines_of_paragraphs(over_cap):
    assert report_lines(over_cap.stdout, "Short last lines of paragraphs:") == [
        '  page 1  paper.typ:6  "the page."',
        '  page 2  paper.typ:14  "before it, widow."',
    ]


def test_a_build_within_the_cap_has_no_over_cap_report(tmp_path):
    project = make_project(
        tmp_path / "repo", over_cap_paper(), toml="page_cap = 2\n", origin=False
    )
    result = build_paper(project)
    assert result.returncode == 0, result.stderr
    assert "Past the page cap" not in result.stdout


MARKED = '#import "build/paperkit.typ": *\n#show: paper.with(abstract: [x])\n= Introduction\n#todo[say why]\n'


def test_the_status_block_marks_each_problem_new_since_the_last_build(tmp_path):
    project = make_project(tmp_path / "repo", MARKED, toml=None, origin=False)
    first = build_paper(project)
    # No last build to compare with
    assert "1 problems (a draft lists them" in first.stdout
    assert "[new]" not in first.stdout
    paper = project / "paper" / "paper.typ"
    paper.write_text(paper.read_text() + 'It holds #n("demo.same"); and more.\n')
    second = build_paper(project)
    assert "2 problems, 1 new since the last build (a draft lists them" in second.stdout
    assert "\n  todo: 1 TODO markers\n" in second.stdout
    assert '\n  [new] semicolon: line 5: #n("demo.same"); drops its' in second.stdout
    paper.write_text(
        paper.read_text().replace("= Introduction\n", "= Introduction\nMore text.\n")
    )
    third = build_paper(project)  # the semicolon moved to line 6: the same problem
    assert "2 problems, 0 new since the last build" in third.stdout
    assert "[new]" not in third.stdout


def test_a_draft_between_two_final_builds_leaves_the_second_s_problems_unmarked(
    tmp_path,
):
    project = make_project(tmp_path / "repo", MARKED, toml=None, origin=False)
    (project / "paper" / "notes.txt").write_text("uncommitted\n")
    build_paper(project, "--final")  # todo, and dirty: a draft doesn't check the tree
    draft = build_paper(project)
    # No draft before it to compare with
    assert "1 problems (a draft lists them" in draft.stdout
    final = build_paper(project, "--final")
    assert "2 problems, 0 new since the last build" in final.stdout
    assert "[new]" not in final.stdout
