"""install.py against a temp copy of the repo layout and a temp
~/.claude/skills. Never touches the real one."""

import json

import pytest

import install


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    src = tmp_path / "repo" / "skills"
    dst = tmp_path / "home" / ".claude" / "skills"
    src.mkdir(parents=True)
    monkeypatch.setattr(install, "SKILLS_SRC", src)
    monkeypatch.setattr(install, "SKILLS_DST", dst)
    monkeypatch.setattr(
        install, "AGENTS_SKILLS_DST", tmp_path / "home" / ".agents" / "skills"
    )
    # Pruning walks every destination, so keep it off the real ~/.local/bin.
    monkeypatch.setattr(install, "BIN_SRC", tmp_path / "repo" / "bin")
    monkeypatch.setattr(install, "BIN_DST", tmp_path / "home" / ".local" / "bin")
    return src, dst


@pytest.fixture
def guidelines(tmp_path, monkeypatch):
    """A guidelines file in a temp repo, and a temp HOME for its two links."""
    src = tmp_path / "repo" / "GLOBAL_CLAUDE.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Guidelines\n")
    home = tmp_path / "home"
    monkeypatch.setattr(install, "GUIDELINES_SRC", src)
    monkeypatch.setattr(
        install,
        "GUIDELINES_DSTS",
        [home / ".claude" / "CLAUDE.md", home / ".agents" / "AGENTS.md"],
    )
    return src, home


@pytest.fixture
def tools(tmp_path, monkeypatch):
    src = tmp_path / "repo" / "bin"
    dst = tmp_path / "home" / ".local" / "bin"
    src.mkdir(parents=True)
    monkeypatch.setattr(install, "BIN_SRC", src)
    monkeypatch.setattr(install, "BIN_DST", dst)
    return src, dst


def make_tool(src, name, executable=True):
    path = src / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return path


def test_links_executable_tools_only(tools):
    src, dst = tools
    make_tool(src, "gui-shot")
    make_tool(src, "notes.txt", executable=False)
    assert install.link_tools() == 0
    assert (dst / "gui-shot").resolve() == src / "gui-shot"
    assert not (dst / "notes.txt").exists()
    assert install.link_tools() == 0, "idempotent"


def test_tool_name_taken_by_another_file_is_a_conflict(tools, capsys):
    src, dst = tools
    make_tool(src, "gui-shot")
    dst.mkdir(parents=True)
    (dst / "gui-shot").write_text("someone else's")
    assert install.link_tools() == 1
    assert "CONFLICT" in capsys.readouterr().out
    assert (dst / "gui-shot").read_text() == "someone else's"


def test_prune_removes_link_to_deleted_tool(dirs, tools):
    src, dst = tools
    make_tool(src, "gone")
    install.link_tools()
    (src / "gone").unlink()
    install.prune_stale_links()
    assert not (dst / "gone").is_symlink()


def make_skill(src, name, with_md=True):
    d = src / name
    d.mkdir()
    if with_md:
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    return d


def test_links_skills_and_skips_dirs_without_skill_md(dirs, capsys):
    src, dst = dirs
    make_skill(src, "good")
    make_skill(src, "bare", with_md=False)
    assert install.link_skills() == 0
    assert (dst / "good").is_symlink() and (dst / "good").resolve() == src / "good"
    assert not (dst / "bare").exists()
    assert "skipped bare: no SKILL.md" in capsys.readouterr().out


def test_prune_removes_link_to_deleted_skill(dirs):
    src, dst = dirs
    gone = make_skill(src, "gone")
    install.link_skills()
    (gone / "SKILL.md").unlink()
    gone.rmdir()
    install.prune_stale_links()
    assert not (dst / "gone").is_symlink()


def test_prune_removes_link_to_skill_that_lost_its_skill_md(dirs):
    # Same rule as link_skills: a dir without SKILL.md is not a skill, so a
    # link to it is stale even though the dir still exists.
    src, dst = dirs
    skill = make_skill(src, "hollow")
    install.link_skills()
    (skill / "SKILL.md").unlink()
    install.prune_stale_links()
    assert not (dst / "hollow").is_symlink()
    assert (src / "hollow").is_dir()


def test_prune_leaves_links_into_other_repos_alone(dirs, tmp_path):
    _src, dst = dirs
    dst.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "theirs"
    other.mkdir(parents=True)
    (dst / "theirs").symlink_to(other)
    other.rmdir()  # dangling, but not ours to prune
    install.prune_stale_links()
    assert (dst / "theirs").is_symlink()


def test_link_reports_conflicts(dirs, capsys):
    src, dst = dirs
    make_skill(src, "taken")
    dst.mkdir(parents=True)
    (dst / "taken").mkdir()
    assert install.link_skills() == 1
    assert "CONFLICT" in capsys.readouterr().out
    assert (dst / "taken").is_dir() and not (dst / "taken").is_symlink()


def test_skills_are_linked_into_agents_skills_too(dirs, tmp_path):
    src, _dst = dirs
    make_skill(src, "good")
    install.link_skills()
    agents = tmp_path / "home" / ".agents" / "skills" / "good"
    assert agents.is_symlink() and agents.resolve() == src / "good"


def test_guidelines_reach_claude_and_agents_files(guidelines):
    src, home = guidelines
    install.link_guidelines()
    for dst in (home / ".claude" / "CLAUDE.md", home / ".agents" / "AGENTS.md"):
        assert dst.is_symlink() and dst.resolve() == src, dst


def test_guidelines_link_keeps_a_real_file_as_bak(guidelines, capsys):
    src, home = guidelines
    real = home / ".claude" / "CLAUDE.md"
    real.parent.mkdir(parents=True)
    real.write_text("mine\n")
    install.link_guidelines()
    assert real.is_symlink() and real.resolve() == src
    assert (home / ".claude" / "CLAUDE.md.bak").read_text() == "mine\n"
    assert "moved" in capsys.readouterr().out


def test_guidelines_link_replaces_a_link_that_points_elsewhere(guidelines):
    src, home = guidelines
    dst = home / ".agents" / "AGENTS.md"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(home / "somewhere-else.md")
    install.link_guidelines()
    assert dst.resolve() == src
    assert not (home / ".agents" / "AGENTS.md.bak").exists()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".claude" / "settings.json"
    monkeypatch.setattr(install, "SETTINGS", path)
    monkeypatch.setattr(install, "BIN_DST", tmp_path / "home" / ".local" / "bin")
    return path


def test_hooks_register_once_beside_existing_settings(settings):
    settings.parent.mkdir(parents=True)
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "log-it"}]}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [other]}}))
    assert install.register_hooks() == 0
    assert install.register_hooks() == 0
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    groups = data["hooks"]["PreToolUse"]
    assert groups[0] == other and len(groups) == 2
    assert groups[1]["hooks"][0]["command"] == str(
        install.BIN_DST / "agent-commit-gate"
    )


def test_hooks_create_a_missing_settings_file(settings):
    assert install.register_hooks() == 0
    assert "agent-commit-gate" in settings.read_text()


def test_hooks_leave_a_broken_settings_file_alone(settings, capsys):
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    assert install.register_hooks() == 1
    assert settings.read_text() == "{not json" and "CONFLICT" in capsys.readouterr().out


@pytest.fixture
def third_party(tmp_path, monkeypatch):
    """A temp ~/.agents/skills, and a record of the installer commands run.
    The fake installer creates every skill it is asked for, as the real one does."""
    dst = tmp_path / "home" / ".agents" / "skills"
    dst.mkdir(parents=True)
    monkeypatch.setattr(install, "AGENTS_SKILLS_DST", dst)
    monkeypatch.setattr(
        install,
        "THIRD_PARTY_SKILLS",
        {"someone/skills": ["tdd", "retro"], "other/skills": ["find"]},
    )
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        for name in cmd[cmd.index("-s") + 1 : cmd.index("-a")]:
            (dst / name).mkdir()
        return install.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    return dst, calls


def test_third_party_installs_only_missing_skills(third_party):
    dst, calls = third_party
    (dst / "tdd").mkdir()
    (dst / "find").mkdir()
    assert install.install_third_party_skills() == 0
    assert len(calls) == 1
    cmd = calls[0]
    assert "someone/skills" in cmd and "-g" in cmd
    assert cmd[cmd.index("-s") + 1 : cmd.index("-a")] == ["retro"]


def test_third_party_runs_nothing_when_all_present(third_party):
    dst, calls = third_party
    for name in ("tdd", "retro", "find"):
        (dst / name).mkdir()
    assert install.install_third_party_skills() == 0
    assert calls == []


def test_third_party_reports_a_failed_install(third_party, monkeypatch, capsys):
    def failing_run(cmd, **kw):
        return install.subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(install.subprocess, "run", failing_run)
    assert install.install_third_party_skills() == 2
    assert "FAILED" in capsys.readouterr().out


def test_third_party_reports_a_skill_the_source_no_longer_has(
    third_party, monkeypatch, capsys
):
    # The real CLI exits 0 when a requested skill isn't in the source.
    def skips_everything(cmd, **kw):
        return install.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(install.subprocess, "run", skips_everything)
    assert install.install_third_party_skills() == 2
    out = capsys.readouterr().out
    assert "FAILED someone/skills" in out and "tdd, retro" in out


def test_third_party_reports_missing_npx(third_party, monkeypatch, capsys):
    def no_npx(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(install.subprocess, "run", no_npx)
    assert install.install_third_party_skills() == 2
    assert "npx" in capsys.readouterr().out
