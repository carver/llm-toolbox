"""install.py against a temp copy of the repo layout and a temp
~/.claude/skills. Never touches the real one."""

import pytest

import install


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    src = tmp_path / "repo" / "skills"
    dst = tmp_path / "home" / ".claude" / "skills"
    src.mkdir(parents=True)
    monkeypatch.setattr(install, "SKILLS_SRC", src)
    monkeypatch.setattr(install, "SKILLS_DST", dst)
    monkeypatch.setattr(install, "AGENTS_SKILLS_DST", tmp_path / "home" / ".agents" / "skills")
    return src, dst


@pytest.fixture
def guidelines(tmp_path, monkeypatch):
    """A guidelines file in a temp repo, and a temp HOME for its two links."""
    src = tmp_path / "repo" / "GLOBAL_CLAUDE.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Guidelines\n")
    home = tmp_path / "home"
    monkeypatch.setattr(install, "GUIDELINES_SRC", src)
    monkeypatch.setattr(install, "GUIDELINES_DSTS",
                        [home / ".claude" / "CLAUDE.md", home / ".agents" / "AGENTS.md"])
    return src, home


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
    src, dst = dirs
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
    src, dst = dirs
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
