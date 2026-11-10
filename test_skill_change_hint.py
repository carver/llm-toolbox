"""bin/skill-change-hint: a skill loaded this session that changed on disk since gets one note."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "skill-change-hint"

AN_HOUR_AGO = time.time() - 3600


class Session:
    """A home, a project and a state folder, and the hook run against them as Claude Code runs it."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.project = tmp_path / "project"
        self.state = tmp_path / "state"
        self.cwd = self.project
        for path in (self.home, self.project, self.state):
            path.mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.state),
            "CLAUDE_PROJECT_DIR": str(self.project),
        }
        if "TZ" in os.environ:  # the hook's local times, as the test reckons them
            self.env["TZ"] = os.environ["TZ"]

    def skill(self, root: Path, name: str) -> Path:
        """A SKILL.md under root/.../<name>, last changed an hour ago."""
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# {name}\n")
        os.utime(path, (AN_HOUR_AGO, AN_HOUR_AGO))
        return path

    def user_skill(self, name: str) -> Path:
        return self.skill(self.home / ".claude" / "skills", name)

    def project_skill(self, name: str) -> Path:
        return self.skill(self.project / ".claude" / "skills", name)

    def run(self, event: dict | str, session="s1", agent=None) -> tuple[int, str, str]:
        if isinstance(event, dict):
            event = {"session_id": session, "cwd": str(self.cwd), **event}
            if agent:
                event["agent_id"] = agent
            event = json.dumps(event)
        result = subprocess.run(
            [SCRIPT],
            input=event,
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def load(self, name: str, **kw) -> tuple[int, str, str]:
        """The Skill tool loading a skill, as its PostToolUse event reports it."""
        return self.run(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Skill",
                "tool_input": {"skill": name},
                "tool_response": {"success": True, "commandName": name},
            },
            **kw,
        )

    def prompt(self, text: str, **kw) -> tuple[int, str, str]:
        return self.run({"hook_event_name": "UserPromptSubmit", "prompt": text}, **kw)

    def before(self, tool="Bash", **kw) -> str:
        """The note added before a tool call, or "" for none; the hook must exit 0, silent on stderr."""
        tool_input = {"command": "echo hi"} if tool == "Bash" else {"prompt": "go"}
        code, out, err = self.run(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": tool_input,
            },
            **kw,
        )
        assert (code, err) == (0, "")
        if not out:
            return ""
        output = json.loads(out)["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in output
        return output["additionalContext"]


def touch(path: Path, at: float | None = None) -> None:
    """Change the file, its mtime moving to `at` (now by default)."""
    path.write_text(path.read_text() + "a new step\n")
    if at is not None:
        os.utime(path, (at, at))


@pytest.fixture
def session(tmp_path):
    return Session(tmp_path)


def test_a_loaded_skill_that_changed_gets_a_note_naming_its_time_and_path(session):
    path = session.user_skill("build")
    os.utime(path, (time.time() - 2 * 86400,) * 2)
    session.load("build")
    assert session.before() == ""
    today_0104 = time.mktime(time.localtime()[:3] + (1, 4, 0, 0, 0, -1))
    touch(path, today_0104)
    assert session.before() == (
        "skill `build` changed at 01:04 since you loaded it "
        f"({session.home}/.claude/skills/build/SKILL.md); "
        "read it again before its next step."
    )


def test_the_note_comes_once_per_change(session):
    path = session.user_skill("build")
    session.load("build")
    touch(path)
    assert "skill `build` changed" in session.before()
    assert session.before() == ""
    assert session.before("Agent") == ""
    touch(path, time.time() + 5)
    assert "skill `build` changed" in session.before("Agent")


def test_loading_it_again_takes_the_new_copy_as_read(session):
    path = session.user_skill("build")
    session.load("build")
    touch(path)
    session.load("build")
    assert session.before() == ""


def test_a_change_from_a_day_before_names_the_date(session):
    path = session.user_skill("build")
    os.utime(path, (AN_HOUR_AGO - 3 * 86400,) * 2)
    session.load("build")
    yesterday = time.time() - 2 * 86400
    touch(path, yesterday)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(yesterday))
    assert f"changed at {stamp} since" in session.before()


def test_a_project_skill_is_found_under_the_project_and_the_user_one_wins(session):
    project = session.project_skill("review")
    session.load("review")
    touch(project)
    assert f"({project})" in session.before()

    user = session.user_skill("deploy")
    shadowed = session.project_skill("deploy")
    session.load("deploy")
    touch(shadowed)
    assert "deploy" not in session.before()
    touch(user)
    assert f"({user})" in session.before()


def test_a_project_skill_is_found_from_a_folder_below_the_project(session):
    session.env.pop("CLAUDE_PROJECT_DIR")
    path = session.project_skill("review")
    session.cwd = session.project / "src" / "deep"
    session.cwd.mkdir(parents=True)
    session.load("review")
    touch(path)
    assert f"({path})" in session.before()


def test_claude_config_dir_moves_the_user_skills(session):
    config = session.home / "elsewhere"
    session.env["CLAUDE_CONFIG_DIR"] = str(config)
    path = session.skill(config / "skills", "build")
    session.load("build")
    touch(path)
    assert f"({path})" in session.before()


def test_a_skill_reached_through_a_link_counts_the_file_it_points_to(session, tmp_path):
    real = session.skill(tmp_path / "toolbox" / "skills", "build")
    (session.home / ".claude" / "skills").mkdir(parents=True)
    (session.home / ".claude" / "skills" / "build").symlink_to(real.parent)
    session.load("build")
    touch(real)
    assert "skill `build` changed" in session.before()


def test_a_plugin_skill_is_found_through_the_installed_plugins(session, tmp_path):
    install_path = tmp_path / "cache" / "market" / "tools" / "1.0.0"
    path = session.skill(install_path / "skills", "lint")
    plugins = session.home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "tools@market": [
                        {"scope": "user", "installPath": str(install_path)}
                    ],
                    "other@market": [{"scope": "user", "installPath": "/nowhere"}],
                },
            }
        )
    )
    session.load("tools:lint")
    touch(path)
    note = session.before()
    assert "skill `tools:lint` changed at" in note and f"({path})" in note


@pytest.mark.parametrize(
    "prompt",
    [
        "/build fix the tests",
        "/build",
        "<command-name>/build</command-name>\n<command-args>x</command-args>",
    ],
)
def test_a_slash_command_counts_as_a_load(session, prompt):
    path = session.user_skill("build")
    assert session.prompt(prompt) == (0, "", "")
    touch(path)
    assert "skill `build` changed" in session.before()


@pytest.mark.parametrize(
    "prompt", ["please run /build", "/clear", "/nothing-here x", "", "/"]
)
def test_a_prompt_that_loads_no_skill_records_nothing(session, prompt):
    session.user_skill("build")
    assert session.prompt(prompt) == (0, "", "")
    assert not any(session.state.rglob("*.json"))


def test_a_skill_that_cannot_be_resolved_is_skipped(session):
    path = session.user_skill("build")
    assert session.load("anthropic-skills:docs") == (0, "", "")
    session.load("build")
    touch(path)
    assert session.before().count("skill `") == 1


def test_two_changed_skills_get_a_line_each(session):
    build = session.user_skill("build")
    tdd = session.user_skill("tdd")
    session.load("build")
    session.load("tdd")
    touch(build)
    touch(tdd)
    lines = session.before().splitlines()
    assert sorted(line.split("`")[1] for line in lines) == ["build", "tdd"]


def test_sessions_and_subagents_keep_their_own_loads(session):
    path = session.user_skill("build")
    session.load("build", session="s1", agent="a1")
    touch(path)
    assert session.before(session="s1") == ""
    assert session.before(session="s2", agent="a1") == ""
    assert "skill `build` changed" in session.before(session="s1", agent="a1")


def test_a_deleted_skill_says_nothing(session):
    path = session.user_skill("build")
    session.load("build")
    path.unlink()
    assert session.before() == ""


def test_state_lives_under_xdg_state_home_and_old_sessions_are_removed(session):
    session.user_skill("build")
    folder = session.state / "skill-change-hint"
    folder.mkdir()
    old = folder / "gone.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    recent = folder / "kept.json"
    recent.write_text("{}")
    session.load("build")
    files = sorted(p.name for p in folder.iterdir())
    assert "gone.json" not in files and "kept.json" in files and len(files) == 2


def test_a_session_id_cannot_write_outside_the_state_folder(session):
    path = session.user_skill("build")
    session.load("build", session="../../escape")
    assert not (session.state.parent / "escape.json").exists()
    assert all(
        p.is_relative_to(session.state) for p in session.state.parent.rglob("*.json")
    )
    touch(path)
    assert "skill `build` changed" in session.before(session="../../escape")


@pytest.mark.parametrize(
    "event",
    [
        "not json",
        "[]",
        json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"}),
        json.dumps(
            {"hook_event_name": "PostToolUse", "tool_name": "Skill", "session_id": "s1"}
        ),
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Skill",
                "session_id": "s1",
                "tool_input": {"skill": 7},
            }
        ),
        json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": None}
        ),
    ],
)
def test_an_event_it_cannot_read_passes_silently(session, event):
    assert session.run(event) == (0, "", "")


def test_a_broken_state_file_passes_silently_and_a_new_load_replaces_it(session):
    path = session.user_skill("build")
    folder = session.state / "skill-change-hint"
    folder.mkdir()
    (folder / "s1.json").write_text("{broken")
    assert session.before() == ""
    session.load("build")
    touch(path)
    assert "skill `build` changed" in session.before()


def test_an_unwritable_state_folder_passes_silently(session):
    session.user_skill("build")
    (session.state / "skill-change-hint").write_text("a file, not a folder")
    assert session.load("build") == (0, "", "")
    assert session.before() == ""


def test_help_prints_the_docstring_without_reading_stdin():
    result = subprocess.run(
        [SCRIPT, "-h"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "skill-change-hint < hook-input.json" in result.stdout
