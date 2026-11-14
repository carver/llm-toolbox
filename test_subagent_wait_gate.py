"""bin/subagent-wait-gate: when it refuses a subagent's stop, what it says, and when it lets go."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "subagent-wait-gate"


NOW = datetime.now(UTC).isoformat()
LIVE_PID = os.getpid()  # a process that runs as long as the test does


def started(
    tid: str, task: str, output: Path, when: str = NOW, agent: str = "a1"
) -> list[dict]:
    """A transcript's records for one Bash call that went to the background as task."""
    return [
        {
            "type": "assistant",
            "agentId": agent,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": "Bash",
                        "input": {"command": "x", "run_in_background": True},
                    }
                ]
            },
        },
        {
            "type": "user",
            "agentId": agent,
            "timestamp": when,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": f"Command running in background with ID: {task}. "
                        f"Output is being written to: {output}. You will be notified.",
                    }
                ]
            },
        },
    ]


def test_a_job_moved_to_the_background_is_its_own(env, tmp_path):
    """Claude Code moves a subagent's Bash call to the background after 180 s."""
    records = started("t1", "b1", tmp_path / "b1.output")
    records[1]["message"]["content"][0]["content"] = [
        {
            "type": "text",
            "text": "Command was moved to the background (ID: b1) after running for "
            f"180s. Output is being written to: {tmp_path / 'b1.output'}.",
        }
    ]
    path = write(tmp_path / "agent-a1.jsonl", records)
    _, decision, _ = stop(env, path, [task("b1")])
    assert decision["decision"] == "block"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}


def write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    """A subagent that started task b1 (bgrun --wait, whose PID line follows the line
    saying it moved an old LOG aside) and task b2 (a plain command printing digits)."""
    out1, out2 = tmp_path / "b1.output", tmp_path / "b2.output"
    out1.write_text(f"bgrun: moved log to log.2026-11-13T10-00-00\n{LIVE_PID}\n")
    out2.write_text(f"{LIVE_PID}\n")
    records = started("t1", "b1", out1) + started("t2", "b2", out2)
    return write(tmp_path / "agent-a1.jsonl", records)


def task(tid: str, status: str = "running", kind: str = "shell") -> dict:
    command = f"bgrun --wait -o log -- job-{tid}" if tid != "b2" else "make test"
    return {
        "id": tid,
        "type": kind,
        "status": status,
        "description": f"Job {tid}",
        "command": command,
    }


def tool_call(transcript: Path) -> None:
    """The subagent makes one more tool call, such as a wait."""
    records = started(f"w{transcript.stat().st_size}", "", transcript)[:1]
    with transcript.open("a") as f:
        f.write(json.dumps(records[0]) + "\n")


def stop(
    env: dict[str, str],
    transcript: Path | None,
    tasks: list[dict] | None,
    event_name: str = "SubagentStop",
    agent: str | None = "a1",
    raw: str | None = None,
) -> tuple[int, dict | None, str]:
    """The hook's exit status, its JSON decision (None when it printed nothing), stderr."""
    event = {
        "session_id": "s1",
        "transcript_path": "/main.jsonl",
        "cwd": "/r",
        "hook_event_name": event_name,
        "stop_hook_active": False,
        "last_assistant_message": "Started; waiting for the notification.",
    }
    if agent:
        event |= {"agent_id": agent, "agent_type": "general-purpose"}
    if transcript:
        event["agent_transcript_path"] = str(transcript)
    if tasks is not None:
        event["background_tasks"] = tasks
    result = subprocess.run(
        [SCRIPT],
        input=raw if raw is not None else json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    out = result.stdout.strip()
    return result.returncode, json.loads(out) if out else None, result.stderr


def test_refuses_a_stop_while_its_own_job_runs_and_names_the_wait(env, transcript):
    code, decision, _ = stop(env, transcript, [task("b1")])
    assert code == 0
    assert decision["decision"] == "block"
    reason = decision["reason"]
    assert f"waitone --pid {LIVE_PID} -t 170" in reason
    assert "Job b1" in reason
    assert "b1.output" in reason
    assert "TaskStop" in reason


def test_a_job_not_run_by_bgrun_gets_a_way_to_find_its_pid(env, transcript):
    _, decision, _ = stop(env, transcript, [task("b2")])
    assert "safeps -f" in decision["reason"]
    assert f"--pid {LIVE_PID}" not in decision["reason"]


def test_a_pid_that_no_longer_runs_is_not_offered(env, transcript, tmp_path):
    (tmp_path / "b1.output").write_text("999999999\n")
    _, decision, _ = stop(env, transcript, [task("b1")])
    assert "safeps -f" in decision["reason"]


def test_lets_the_third_stop_in_a_row_through(env, transcript):
    decisions = [stop(env, transcript, [task("b1")])[1] for _ in range(3)]
    assert [d and d["decision"] for d in decisions] == ["block", "block", None]


def test_a_tool_call_between_stops_starts_the_count_again(env, transcript):
    for _ in range(2):
        stop(env, transcript, [task("b1")])
    tool_call(transcript)
    _, decision, _ = stop(env, transcript, [task("b1")])
    assert decision["decision"] == "block"


def test_a_job_an_hour_old_is_let_go(env, tmp_path):
    old = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
    path = write(
        tmp_path / "agent-a1.jsonl", started("t1", "b1", tmp_path / "o", when=old)
    )
    assert stop(env, path, [task("b1")])[:2] == (0, None)


def test_a_job_another_agent_started_is_not_its_own(env, tmp_path):
    """A fork's transcript starts with a copy of its parent's history."""
    path = write(
        tmp_path / "agent-a1.jsonl", started("t1", "b1", tmp_path / "o", agent="p")
    )
    assert stop(env, path, [task("b1")])[:2] == (0, None)


def test_a_new_job_is_refused_after_an_old_one_was_let_go(env, transcript):
    for _ in range(3):
        stop(env, transcript, [task("b1")])
    _, decision, _ = stop(env, transcript, [task("b1"), task("b2")])
    assert decision["decision"] == "block"
    assert "Job b2" in decision["reason"]
    assert "Job b1" not in decision["reason"]


def test_counts_are_kept_per_agent(env, transcript, tmp_path):
    for _ in range(2):
        stop(env, transcript, [task("b1")])
    other = write(
        tmp_path / "agent-a2.jsonl",
        started("t1", "b1", tmp_path / "b1.output", agent="a2"),
    )
    _, decision, _ = stop(env, other, [task("b1")], agent="a2")
    assert decision["decision"] == "block"


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [task("b1", status="completed")],
        [task("b1", status="failed")],
        [task("b9")],  # another agent's job, or the parent's
        [task("b1", kind="subagent")],
        None,  # an older Claude Code that sends no background_tasks
    ],
    ids=["none", "completed", "failed", "not-its-own", "not-a-shell", "no-field"],
)
def test_lets_the_stop_through_without_a_live_job_of_its_own(env, transcript, tasks):
    assert stop(env, transcript, tasks) == (0, None, "")


def test_the_main_sessions_stop_is_never_refused(env, transcript):
    assert stop(env, transcript, [task("b1")], event_name="Stop", agent=None) == (
        0,
        None,
        "",
    )


@pytest.mark.parametrize(
    "case",
    ["not-json", "no-transcript", "unreadable-transcript", "state-unwritable"],
)
def test_lets_the_stop_through_when_something_is_wrong(env, transcript, tmp_path, case):
    if case == "not-json":
        assert stop(env, transcript, [], raw="{not json")[:2] == (0, None)
        return
    if case == "no-transcript":
        assert stop(env, None, [task("b1")])[:2] == (0, None)
        return
    if case == "unreadable-transcript":
        assert stop(env, tmp_path / "missing.jsonl", [task("b1")])[:2] == (0, None)
        return
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the state folder would go")
    env = {**env, "XDG_STATE_HOME": str(blocker)}
    assert stop(env, transcript, [task("b1")])[:2] == (0, None)


def test_help_prints_the_docstring():
    result = subprocess.run([SCRIPT, "-h"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "SubagentStop" in result.stdout
