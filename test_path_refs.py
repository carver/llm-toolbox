"""bin/path-refs, run as an agent runs it, against a small workspace of the test's own:
PATH_REFS_ROOT points at it, HOME at a home folder beside it, and a fake crontab comes
first on PATH."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "path-refs"


class Workspace:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "claudedev"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "fakebin"
        self.crontab = tmp_path / "crontab.txt"
        for folder in (self.root, self.home, self.bin):
            folder.mkdir()
        fake = self.bin / "crontab"
        fake.write_text(
            "#!/bin/sh\n"
            f'[ "$1" = -l ] && [ -f "{self.crontab}" ] || '
            "{ echo 'no crontab for user' >&2; exit 1; }\n"
            f'cat "{self.crontab}"\n'
        )
        fake.chmod(0o755)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        path: str | None = None,
        root: Path | None = None,
        home: Path | None = None,
    ):
        env = {
            **os.environ,
            "PATH_REFS_ROOT": str(root or self.root),
            "HOME": str(home or self.home),
            "PATH": path if path is not None else f"{self.bin}:{os.environ['PATH']}",
        }
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=cwd or self.root.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def test_each_form_of_reference_is_listed_with_its_line_and_the_path_as_written(ws):
    ws.write(
        "alpha/README.md",
        f"Absolute: {ws.root}/cache/proj/data.csv\n"
        "Backticks: `cache/proj/refs.py`.\n"
        "Link: [notes](../cache/proj/notes.md)\n"
        "Deep: ../../../cache/proj\n"
        "Home form: ~/code/claudedev/cache/proj/x\n"
        'Named: "claudedev/cache/proj"\n'
        'Computed: "$(cd .. && pwd)/cache/proj/run" and f"{root}/cache/proj"\n'
        "Sentence end: see cache/proj.\n"
        "Variable: cd $ROOT/cache/proj/v\n"
        "Placeholder: WORKSPACE/cache/proj/w\n"
        "Unopened: pwd)/cache/proj/z\n"
        "Nothing here.\n",
    )
    ws.write("alpha/sub/code.py", 'DATA = "cache/proj/sub/file.json"\n')

    result = ws.run("--all", "cache/proj")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 13\n"
        f"  alpha/README.md:1: {ws.root}/cache/proj/data.csv\n"
        "  alpha/README.md:2: cache/proj/refs.py\n"
        "  alpha/README.md:3: ../cache/proj/notes.md\n"
        "  alpha/README.md:4: ../../../cache/proj\n"
        "  alpha/README.md:5: ~/code/claudedev/cache/proj/x\n"
        "  alpha/README.md:6: claudedev/cache/proj\n"
        "  alpha/README.md:7: $(cd .. && pwd)/cache/proj/run\n"
        "  alpha/README.md:7: {root}/cache/proj\n"
        "  alpha/README.md:8: cache/proj\n"
        "  alpha/README.md:9: $ROOT/cache/proj/v\n"
        "  alpha/README.md:10: WORKSPACE/cache/proj/w\n"
        "  alpha/README.md:11: )/cache/proj/z\n"
        "  alpha/sub/code.py:1: cache/proj/sub/file.json\n"
        "total: 13\n"
    )


def test_a_path_inside_a_longer_path_or_name_is_not_a_reference(ws):
    ws.write(
        "alpha/notes.md",
        "uv's cache: ~/.cache/uv/wheels\n"
        "system: /var/cache/uv\n"
        "mycache/uv and cache/uvx and cache/uv.bak and cache/uv-old\n"
        "https://example.com/repo/cache/uv\n"
        "${ROOT}x/cache/uv\n"
        "the real one (cache/uv)\n",
    )

    result = ws.run("cache/uv")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "alpha: 1\n  alpha/notes.md:6: cache/uv\ntotal: 1\n"


def test_skipped_folders_and_git_ignored_files_are_not_searched_from_any_cwd(ws):
    ref = "see cache/proj/x\n"
    ws.write("alpha/.gitignore", "ignored.md\n")
    subprocess.run(["git", "init", "-q", str(ws.root / "alpha")], check=True)
    for skipped in (
        "cache/proj/self.md",
        "keep/other/notes.md",
        "alpha/.git/notes.txt",
        "alpha/sub/.git/info",
        "alpha-wt-topic/README.md",
        "alpha/node_modules/pkg/index.js",
        "alpha/.venv/lib/site.py",
        "alpha/sub/target/debug/build.txt",
        "alpha/ignored.md",
    ):
        ws.write(skipped, ref)
    for searched in (
        "alpha/.config/settings.json",
        "alpha/cache/local.md",
        "alpha/notes-wt-old.md",
    ):
        ws.write(searched, ref)
    elsewhere = ws.root.parent / "elsewhere"
    elsewhere.mkdir()

    result = ws.run("cache/proj", cwd=elsewhere)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 3\n"
        "  alpha/.config/settings.json:1: cache/proj/x\n"
        "  alpha/cache/local.md:1: cache/proj/x\n"
        "  alpha/notes-wt-old.md:1: cache/proj/x\n"
        "total: 3\n"
    )


def test_groups_are_the_root_then_each_repo_and_a_frozen_repo_is_marked(ws):
    ws.write("CLAUDE.md", "Logs in cache/proj/boots/<boot>/\n")
    ws.write("beta/FROZEN", "Frozen since 2026-10-30: done.\n")
    ws.write("beta/notes.md", "old data: ../cache/proj/old.csv\n")
    ws.write("alpha/a.md", "one cache/proj\n\ntwo cache/proj/b and cache/proj/b\n")

    full = ws.run("cache/proj")
    counts = ws.run("--count", "cache/proj")

    assert full.returncode == 0, full.stderr
    assert full.stdout == (
        "(workspace): 1\n"
        "  CLAUDE.md:1: cache/proj/boots/\n"
        "alpha: 2\n"
        "  alpha/a.md:1: cache/proj\n"
        "  alpha/a.md:3: cache/proj/b\n"
        "beta (frozen): 1\n"
        "  beta/notes.md:1: ../cache/proj/old.csv\n"
        "total: 4\n"
    )
    assert counts.returncode == 0, counts.stderr
    assert counts.stdout == "(workspace): 1\nalpha: 2\nbeta (frozen): 1\ntotal: 4\n"


def test_no_reference_exits_1_and_paths_nothing_references_are_named(ws):
    ws.write("alpha/a.md", "cache/proj/x\n")

    none = ws.run("cache/none")
    some = ws.run("--count", "cache/proj", "cache/none", "keep/gone")

    assert none.returncode == 1, none.stderr
    assert none.stdout == "total: 0\n"
    assert some.returncode == 0, some.stderr
    assert some.stdout == "alpha: 1\nno refs to: cache/none, keep/gone\ntotal: 1\n"


def test_path_is_relative_to_the_root_from_any_cwd_or_absolute_inside_it(ws):
    ws.write("alpha/a.md", "cache/proj/x\n")
    inside = ws.root / "alpha"
    expected = "alpha: 1\n  alpha/a.md:1: cache/proj/x\ntotal: 1\n"

    for args in (
        ("cache/proj/",),
        (f"{ws.root}/cache/proj",),
        ("./cache//proj",),
    ):
        result = ws.run(*args, cwd=inside)
        assert (result.returncode, result.stdout) == (0, expected), args


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", ".", "cache/../.."])
def test_a_path_outside_the_workspace_is_a_usage_error(ws, path):
    result = ws.run(path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert "workspace" in result.stderr


def link(at: Path, target: str | Path) -> None:
    at.parent.mkdir(parents=True, exist_ok=True)
    at.symlink_to(target)


def test_links_lists_symlinks_into_the_path_and_crontab_lines_that_name_it(ws):
    ws.write("cache/proj/bin/tool", "")
    ws.write("alpha/a.md", "cache/proj/x\n")
    link(ws.home / ".local/bin/tool", ws.root / "cache/proj/bin/tool")
    link(ws.home / "a/b/c/d/five", ws.root / "cache/proj")
    link(ws.home / "a/b/c/d/e/six", ws.root / "cache/proj")
    link(ws.home / "other", ws.root / "cache/projx")
    link(ws.home / ".npm", ws.root / "cache/npm")
    link(ws.root / "alpha/data", "../cache/proj/dl")
    link(ws.root / "alpha/sub/deep/up", "../../../cache/proj")
    link(ws.root / "alpha/node_modules/x", "../../cache/proj")
    link(ws.root / "alpha/cache/l", "../../cache/proj")
    link(ws.root / "keep/k/link", "../../cache/proj")
    link(ws.root / "cache/other/link", "../proj")
    link(ws.root / "alpha-wt-topic/link", "../cache/proj")
    ws.write("beta/FROZEN", "")
    link(ws.root / "beta/out", ws.root / "cache/proj/out")
    ws.crontab.write_text(
        f"0 * * * * {ws.root}/cache/proj/bin/tool --run\n"
        "5 * * * * echo cache/projx\n"
        "@reboot cd ~/code/claudedev && cache/proj/bin/tool\n"
    )

    plain = ws.run("cache/proj")
    linked = ws.run("--links", "cache/proj")

    assert plain.stdout == "alpha: 1\n  alpha/a.md:1: cache/proj/x\ntotal: 1\n"
    assert linked.returncode == 0, linked.stderr
    assert linked.stdout == (
        "alpha: 4\n"
        "  alpha/a.md:1: cache/proj/x\n"
        "  alpha/cache/l -> ../../cache/proj\n"
        "  alpha/data -> ../cache/proj/dl\n"
        "  alpha/sub/deep/up -> ../../../cache/proj\n"
        "beta (frozen): 1\n"
        f"  beta/out -> {ws.root}/cache/proj/out\n"
        "~: 2\n"
        f"  ~/.local/bin/tool -> {ws.root}/cache/proj/bin/tool\n"
        f"  ~/a/b/c/d/five -> {ws.root}/cache/proj\n"
        "crontab: 2\n"
        f"  crontab:1: {ws.root}/cache/proj/bin/tool\n"
        "  crontab:3: cache/proj/bin/tool\n"
        "total: 9\n"
    )


def test_links_walks_a_workspace_inside_home_once(ws, tmp_path):
    link(ws.root / "alpha/data", "../cache/proj")
    link(tmp_path / "x", ws.root / "cache/proj")

    result = ws.run("--links", "cache/proj", home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 1\n"
        "  alpha/data -> ../cache/proj\n"
        "~: 1\n"
        f"  ~/x -> {ws.root}/cache/proj\n"
        "total: 2\n"
    )


def test_a_root_reached_through_a_symlink_matches_paths_and_links_either_way(
    ws, tmp_path
):
    via = tmp_path / "via"
    via.symlink_to(ws.root)
    ws.write("alpha/a.md", "cache/proj/x\n")
    link(ws.home / "real", ws.root / "cache/proj")
    link(ws.home / "linked", via / "cache/proj")

    root_via_link = ws.run("--links", f"{ws.root}/cache/proj", root=via)
    path_via_link = ws.run("--links", f"{via}/cache/proj")

    expected = (
        "alpha: 1\n"
        "  alpha/a.md:1: cache/proj/x\n"
        "~: 2\n"
        f"  ~/linked -> {via}/cache/proj\n"
        f"  ~/real -> {ws.root}/cache/proj\n"
        "total: 3\n"
    )
    assert (root_via_link.returncode, root_via_link.stdout) == (0, expected)
    assert (path_via_link.returncode, path_via_link.stdout) == (0, expected)


def test_links_without_a_crontab_lists_the_rest(ws):
    link(ws.home / "x", ws.root / "cache/proj")

    result = ws.run("--links", "cache/proj")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"~: 1\n  ~/x -> {ws.root}/cache/proj\ntotal: 1\n"


def test_moved_says_where_each_referenced_path_is_now(ws):
    # cache/proj was split: its notes moved to keep/proj, its downloads stayed.
    ws.write("keep/proj/notes.md", "")
    ws.write("keep/proj/notes/a.md", "")
    ws.write("cache/proj/dl/x.pdf", "")
    ws.write("cache/tool/run.sh", "")
    ws.write(
        "alpha/README.md",
        "1 cache/proj/notes.md\n"
        "2 `cache/proj/dl/x.pdf`\n"
        "3 cache/proj/\n"
        "4 cache/proj/gone.txt\n"
        "5 cache/proj/notes/*.md\n"
        "6 keep/proj/notes.md, already fixed\n",
    )
    ws.write("beta/FROZEN", "")
    ws.write("beta/run.sh", "../cache/tool/run.sh\n")
    link(ws.home / "n", ws.root / "cache/proj/notes.md")
    ws.crontab.write_text(f"0 * * * * {ws.root}/cache/proj/dl/x.pdf\n")

    result = ws.run(
        "--links",
        "--moved",
        "cache/proj",
        "keep/proj",
        "--moved",
        "cache/tool",
        "keep/tool",
    )
    counts = ws.run("--count", "--moved", "cache/proj", "keep/proj")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 5 (2 moved, 1 stayed, 1 split, 1 missing)\n"
        "  moved   alpha/README.md:1: cache/proj/notes.md\n"
        "  stayed  alpha/README.md:2: cache/proj/dl/x.pdf\n"
        "  split   alpha/README.md:3: cache/proj/\n"
        "  missing alpha/README.md:4: cache/proj/gone.txt\n"
        "  moved   alpha/README.md:5: cache/proj/notes/*.md\n"
        "beta (frozen): 1 (1 stayed)\n"
        "  stayed  beta/run.sh:1: ../cache/tool/run.sh\n"
        "~: 1 (1 moved)\n"
        f"  moved   ~/n -> {ws.root}/cache/proj/notes.md\n"
        "crontab: 1 (1 stayed)\n"
        f"  stayed  crontab:1: {ws.root}/cache/proj/dl/x.pdf\n"
        "total: 8 (3 moved, 3 stayed, 1 split, 1 missing)\n"
    )
    assert counts.stdout == (
        "alpha: 5 (2 moved, 1 stayed, 1 split, 1 missing)\n"
        "total: 5 (2 moved, 1 stayed, 1 split, 1 missing)\n"
    )


@pytest.mark.parametrize(
    "args",
    [
        ["cache/proj", "--moved", "cache/proj", "keep/proj"],
        ["--moved", "cache/proj", "/elsewhere"],
        ["--count"],
    ],
    ids=["path-and-moved", "new-outside", "no-path"],
)
def test_moved_with_paths_or_outside_the_workspace_or_no_path_is_a_usage_error(
    ws, args
):
    result = ws.run(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip()


def test_without_rg_the_search_fails_with_exit_3(ws, tmp_path):
    only_python = tmp_path / "only-python"
    only_python.mkdir()
    (only_python / "python3").symlink_to(sys.executable)

    result = ws.run("cache/proj", path=str(only_python))

    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr == "path-refs: rg not found on PATH; install ripgrep\n"


def test_a_root_that_is_not_a_folder_is_a_usage_error(ws, tmp_path):
    result = ws.run("cache/proj", root=tmp_path / "nowhere")

    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert "PATH_REFS_ROOT" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_error_from_rg_exits_3_after_the_hits_it_found(ws):
    ws.write("alpha/a.md", "cache/proj/x\n")
    ws.write("alpha/locked.md", "cache/proj/y\n").chmod(0)

    result = ws.run("cache/proj")

    assert result.returncode == 3
    assert result.stdout == "alpha: 1\n  alpha/a.md:1: cache/proj/x\ntotal: 1\n"
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith("path-refs: rg: ")
    assert "locked.md" in result.stderr


def test_a_file_with_more_than_10_references_is_one_line_unless_all(ws):
    ws.write("alpha/numbers.md", "".join(f"cache/proj/n{i}.csv\n" for i in range(12)))
    ws.write("alpha/few.md", "cache/proj/a\n")

    short = ws.run("cache/proj")
    full = ws.run("--all", "cache/proj")
    moved = ws.run("--moved", "cache/proj", "keep/proj")

    assert short.returncode == 0, short.stderr
    assert short.stdout == (
        "alpha: 13\n"
        "  alpha/few.md:1: cache/proj/a\n"
        "  alpha/numbers.md: 12 refs on lines 1-12\n"
        "total: 13\n"
    )
    assert full.stdout.splitlines()[1:] == [
        "  alpha/few.md:1: cache/proj/a",
        *(f"  alpha/numbers.md:{i + 1}: cache/proj/n{i}.csv" for i in range(12)),
        "total: 13",
    ]
    assert moved.stdout == (
        "alpha: 13 (13 missing)\n"
        "  missing alpha/few.md:1: cache/proj/a\n"
        "          alpha/numbers.md: 12 refs on lines 1-12 (12 missing)\n"
        "total: 13 (13 missing)\n"
    )


def test_moved_expands_braces_in_a_reference(ws):
    ws.write("keep/proj/runs/a/out.jsonl", "")
    ws.write("keep/proj/runs/b/out.jsonl", "")
    ws.write(
        "alpha/a.md",
        "cat cache/proj/runs/{a,b}/out.jsonl\n"
        "cat cache/proj/runs/{c,d}/out.jsonl\n"
        "\\input{cache/proj/runs/a/out.jsonl}\n",
    )

    result = ws.run("--moved", "cache/proj", "keep/proj")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 3 (2 moved, 1 missing)\n"
        "  moved   alpha/a.md:1: cache/proj/runs/{a,b}/out.jsonl\n"
        "  missing alpha/a.md:2: cache/proj/runs/{c,d}/out.jsonl\n"
        "  moved   alpha/a.md:3: cache/proj/runs/a/out.jsonl\n"
        "total: 3 (2 moved, 1 missing)\n"
    )


def test_a_one_segment_path_bare_needs_a_slash_after_it_or_quotes_around_it(ws):
    ws.write(
        "alpha/a.md",
        "keep this in mind\n"
        "see keep/x.md\n"
        "`keep` holds notes\n"
        "up: ../keep\n"
        "the keep-out sign, a keep.\n",
    )

    result = ws.run("keep")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "alpha: 3\n"
        "  alpha/a.md:2: keep/x.md\n"
        "  alpha/a.md:3: keep\n"
        "  alpha/a.md:4: ../keep\n"
        "total: 3\n"
    )


def test_inside_its_repo_a_path_is_also_found_relative_to_the_repo(ws):
    ws.write(
        "alpha/README.md", "Edit `WORKSPACE.md`, then bin/tool.\nThe bin is full.\n"
    )
    ws.write("alpha/bin/run.sh", "cat ../WORKSPACE.md ./WORKSPACE.md\n")
    ws.write("beta/notes.md", "WORKSPACE.md and ../alpha/WORKSPACE.md\n")

    file = ws.run("alpha/WORKSPACE.md")
    folder = ws.run("alpha/bin")

    assert file.returncode == 0, file.stderr
    assert file.stdout == (
        "alpha: 3\n"
        "  alpha/README.md:1: WORKSPACE.md\n"
        "  alpha/bin/run.sh:1: ../WORKSPACE.md\n"
        "  alpha/bin/run.sh:1: ./WORKSPACE.md\n"
        "beta: 1\n"
        "  beta/notes.md:1: ../alpha/WORKSPACE.md\n"
        "total: 4\n"
    )
    assert folder.stdout == "alpha: 1\n  alpha/README.md:1: bin/tool\ntotal: 1\n"


def test_a_path_built_from_parts_in_code_is_listed_as_maybe(ws):
    ws.write(
        "alpha/setup.py",
        'MEMORY = parents[2] / "keep" / "claude-memory"\n'
        'OTHER = HOST_CACHE_ROOT / "claude-memory"\n'
        'NAME = "claude-memory"\n'
        'os.path.join(ROOT, "keep", \'claude-memory\', "x")\n'
        'DIRECT = "keep/claude-memory"\n'
        'HOME / ".cache" / "tectonic": HOST_CACHE_ROOT / "tectonic",\n'
        'TEX_DIR = Path.home() / ".cache" / "tectonic"\n',
    )

    memory = ws.run("keep/claude-memory")
    tectonic = ws.run("cache/tectonic")

    assert memory.returncode == 0, memory.stderr
    assert memory.stdout == (
        "alpha: 3\n"
        '  alpha/setup.py:1: maybe "keep" / "claude-memory"\n'
        "  alpha/setup.py:4: maybe \"keep\", 'claude-memory'\n"
        "  alpha/setup.py:5: keep/claude-memory\n"
        "total: 3\n"
    )
    assert tectonic.stdout == (
        'alpha: 1\n  alpha/setup.py:6: maybe HOST_CACHE_ROOT / "tectonic"\ntotal: 1\n'
    )
