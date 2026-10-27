"""bin/build-paper, run in throwaway projects: numbers come from the project's data source, a draft lists
every problem and exits 0, each optional rule runs only when paper.toml sets it, and only a clean --final
build, from a pushed commit whose links all resolve, writes the submission."""

import http.server
import json
import threading

import pytest

from paperkit_testing import build_paper, git, make_project, pdf_text, write_log

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
#figure(data-figure("demo-figure"), caption: [A figure.]) <fig-demo>
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
#figure(data-figure("demo-figure"), caption: [A figure.]) <fig-demo>
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
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
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
    """The problem lines of a build's status block."""
    _, _, listed = out.partition(" problems (")
    lines = listed.split("\n")[1:]
    return [line.strip() for line in lines[: lines.index("") if "" in lines else None]]


def test_a_draft_is_named_draft_has_a_picture_of_every_page_and_exits_0(flawed):
    project, result = flawed
    assert result.returncode == 0, result.stderr
    draft = project / "paper" / "build" / "DRAFT-paper.pdf"
    assert result.stdout.startswith("paper/build/DRAFT-paper.pdf: ")
    assert draft.read_bytes().startswith(b"%PDF")
    pages = sorted((project / "paper" / "build" / "pages").glob("page-*.png"))
    assert len(pages) == 10  # Code and Data and References share the last
    assert all(page.read_bytes().startswith(b"\x89PNG") for page in pages)
    assert not (project / "paper" / "submission.pdf").exists()


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
    assert not [
        line for line in found if line.startswith("link:")
    ]  # only --final checks links


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
    assert entry["source"]["logs"] == [
        "logs/demo.jsonl"
    ]  # recorded without logs_dir too


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
    assert (
        "  pages before References  1 (cap 8)\n" in result.stdout
    )  # References starts page 2
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
