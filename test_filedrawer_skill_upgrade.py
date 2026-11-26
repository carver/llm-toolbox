"""filedrawer-skill-upgrade upgrades this repo's copy of a File Drawer skill from its
git tag, in a worktree it lands with wt-ship, and keeps the toolbox's upgrade script
in place of File Drawer's."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "bin" / "filedrawer-skill-upgrade"
VENDORED = ("filedrawer-search", "filedrawer-submit")
SHIM = (ROOT / "skills" / "filedrawer-search" / "references" / "upgrade.sh").read_text()
NAME = "filedrawer-search"


def skill_md(name: str, version: str, files: str) -> str:
    return (
        f"---\nname: {name}\ndescription: x\nmetadata:\n"
        f'  version: "{version}"\n  files: "{files}"\n---\n\n# {name} {version}\n'
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


class World:
    """A toolbox repo holding version 11 of the skill, a tag source holding version 12
    of it, and a fake bgrun that records how it was called."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "llm-toolbox"
        (self.repo / "bin").mkdir(parents=True)
        shutil.copy(TOOL, self.repo / "bin" / TOOL.name)
        self.skill = self.repo / "skills" / NAME
        (self.skill / "references").mkdir(parents=True)
        files = "SKILL.md references/entry-format.md references/upgrade.sh"
        (self.skill / "SKILL.md").write_text(skill_md(NAME, "11", files))
        (self.skill / "references" / "entry-format.md").write_text("format 11\n")
        (self.skill / "references" / "upgrade.sh").write_text(SHIM)
        git(self.repo, "init", "-q", "-b", "main")
        # Before the first commit: a runner has no git identity of its own.
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        self.tags = tmp / "tags"
        self.tag(12, skill_md(NAME, "12", files))
        self.home = tmp / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n"
        )
        self.fakes = tmp / "fakes"
        self.fakes.mkdir()
        self.calls = tmp / "bgrun-calls"
        bgrun = self.fakes / "bgrun"
        bgrun.write_text(f'#!/bin/sh\necho "$PWD $*" >> {self.calls}\necho 4242\n')
        bgrun.chmod(0o755)

    def tag(self, version: int, text: str | bytes) -> None:
        at = self.tags / f"skill-{NAME}-v{version}" / "skills" / NAME
        (at / "references").mkdir(parents=True, exist_ok=True)
        raw = text.encode() if isinstance(text, str) else text
        (at / "SKILL.md").write_bytes(raw)
        (at / "references" / "entry-format.md").write_text(f"format {version}\n")
        (at / "references" / "upgrade.sh").write_text("#!/bin/sh\necho upstream\n")

    @property
    def worktree(self) -> Path:
        return self.tmp / "llm-toolbox-wt-filedrawer-search-v12"

    def run(
        self, *args: str, via_shim: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = (
            ["sh", str(self.skill / "references" / "upgrade.sh"), *args]
            if via_shim
            else [str(self.repo / "bin" / TOOL.name), NAME, *args]
        )
        env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "FILEDRAWER_SKILL_UPGRADE_TAGS": self.tags.as_uri(),
            "PATH": f"{self.fakes}{os.pathsep}{os.environ['PATH']}",
        }
        return subprocess.run(
            command, env=env, capture_output=True, text=True, check=False
        )

    def bgrun_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def untouched(self) -> None:
        assert not self.worktree.exists()
        assert self.bgrun_calls() == []
        assert git(self.repo, "status", "--porcelain") == ""
        assert git(self.repo, "log", "--format=%s") == "start\n"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_an_upgrade_commits_the_tag_in_a_worktree_and_lands_it(world: World) -> None:
    done = world.run("12")
    assert done.returncode == 0, done.stderr
    said = done.stdout
    assert "upgraded filedrawer-search from version 11 to 12" in said
    assert "Tell your human" in said
    assert "waitone --pid 4242" in said
    # The main tree only receives the landing.
    assert 'version: "11"' in (world.skill / "SKILL.md").read_text()
    assert git(world.repo, "status", "--porcelain") == ""
    upgraded = world.worktree / "skills" / NAME
    assert 'version: "12"' in (upgraded / "SKILL.md").read_text()
    assert (upgraded / "references" / "entry-format.md").read_text() == "format 12\n"
    assert (upgraded / "references" / "upgrade.sh").read_text() == SHIM
    assert git(world.worktree, "status", "--porcelain") == ""
    assert "filedrawer-search" in git(world.worktree, "log", "-1", "--format=%s")
    assert "12" in git(world.worktree, "log", "-1", "--format=%s")
    [call] = world.bgrun_calls()
    assert call.startswith(f"{world.repo} -o ")
    assert call.endswith(f"-- wt-ship -C {world.worktree} --local --remove")
    # The agent reads the new version before it lands, from a copy wt-ship won't remove.
    now = next(
        Path(word.rstrip(".:,"))
        for word in said.split()
        if word.rstrip(".:,").endswith("/SKILL.md") and "cache" in word
    )
    assert 'version: "12"' in now.read_text()


def test_a_version_in_place_changes_nothing(world: World) -> None:
    for version in ("11", "9"):
        done = world.run(version)
        assert done.returncode == 0, done.stderr
        assert "already" in done.stdout
    world.untouched()


def test_an_upgrade_already_landing_is_left_to_its_landing(world: World) -> None:
    world.worktree.mkdir()
    done = world.run("12")
    assert done.returncode == 0, done.stderr
    assert str(world.worktree) in done.stdout
    assert "Tell your human" in done.stdout
    assert world.bgrun_calls() == []
    assert git(world.repo, "log", "--format=%s") == "start\n"


def test_a_failed_start_of_the_landing_changes_nothing(world: World) -> None:
    (world.fakes / "bgrun").write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    done = world.run("12")
    assert done.returncode == 1
    assert "changed nothing" in done.stderr
    world.untouched()
    assert git(world.repo, "worktree", "list").count("\n") == 1


def test_a_refused_commit_changes_nothing(world: World) -> None:
    hook = world.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    hook.chmod(0o755)
    done = world.run("12")
    assert done.returncode == 1
    assert "changed nothing" in done.stderr
    world.untouched()
    assert git(world.repo, "worktree", "list").count("\n") == 1


def test_a_tag_without_references_gets_the_shim(world: World) -> None:
    shutil.rmtree(world.tags)
    world.tag(12, skill_md(NAME, "12", "SKILL.md"))
    shutil.rmtree(world.tags / f"skill-{NAME}-v12" / "skills" / NAME / "references")
    done = world.run("12")
    assert done.returncode == 0, done.stderr
    shim = world.worktree / "skills" / NAME / "references" / "upgrade.sh"
    assert shim.read_text() == SHIM


def test_a_skill_md_that_isnt_text_changes_nothing(world: World) -> None:
    world.tag(12, b"\xff\xfe" + skill_md(NAME, "12", "SKILL.md").encode())
    done = world.run("12")
    assert done.returncode == 1
    assert "changed nothing" in done.stderr
    assert "Traceback" not in done.stderr
    world.untouched()


def test_an_upgrade_waits_for_an_earlier_version_s_landing(world: World) -> None:
    world.worktree.mkdir()
    done = world.run("13")
    assert done.returncode == 0, done.stderr
    assert str(world.worktree) in done.stdout
    assert world.bgrun_calls() == []
    assert git(world.repo, "log", "--format=%s") == "start\n"


def test_an_empty_mode_file_is_not_auto(world: World) -> None:
    config = world.home / ".config" / "filedrawer"
    config.mkdir(parents=True)
    (config / "upgrade").write_text("")
    done = world.run("12")
    assert done.returncode == 0, done.stderr
    assert "auto" in done.stdout
    world.untouched()


def test_ask_mode_installs_nothing(world: World) -> None:
    config = world.home / ".config" / "filedrawer"
    config.mkdir(parents=True)
    (config / "upgrade").write_text("ask\ndeclined filedrawer-search 10\n")
    done = world.run("12")
    assert done.returncode == 0, done.stderr
    assert "auto" in done.stdout
    assert "Tell your human" in done.stdout
    world.untouched()


@pytest.mark.parametrize("args", [(), ("VERSION",), ("12", "yes"), ("12", "no")])
def test_bad_arguments_change_nothing(world: World, args: tuple[str, ...]) -> None:
    done = world.run(*args)
    assert done.returncode == 1
    assert "changed nothing" in done.stderr
    world.untouched()


def test_a_tag_holding_another_version_changes_nothing(world: World) -> None:
    world.tag(12, skill_md(NAME, "13", "SKILL.md"))
    done = world.run("12")
    assert done.returncode == 1
    assert "changed nothing" in done.stderr
    world.untouched()


def test_a_missing_tag_changes_nothing(world: World) -> None:
    done = world.run("13")
    assert done.returncode == 1
    assert "skill-filedrawer-search-v13" in done.stderr
    world.untouched()


def test_a_file_outside_the_skill_is_refused(world: World) -> None:
    world.tag(12, skill_md(NAME, "12", "SKILL.md ../../bin/evil"))
    done = world.run("12")
    assert done.returncode == 1
    assert "../../bin/evil" in done.stderr
    world.untouched()


def test_a_skill_not_kept_here_is_refused(world: World) -> None:
    done = subprocess.run(
        [str(world.repo / "bin" / TOOL.name), "coding", "12"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    world.untouched()


@pytest.mark.parametrize("name", VENDORED)
def test_each_vendored_skill_upgrades_through_the_toolbox(name: str) -> None:
    folder = ROOT / "skills" / name
    assert (folder / "references" / "upgrade.sh").read_text() == SHIM
    assert f"name: {name}\n" in (folder / "SKILL.md").read_text()
