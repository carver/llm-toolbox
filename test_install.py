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
    return src, dst


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
