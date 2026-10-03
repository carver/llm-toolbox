"""session-digest condenses a Claude Code session, subagents included, for a retrospective."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "session-digest"
BULKY = "x" * 4000
REPORT = "The fix is in. " * 100
NOTIFICATION = f"""<task-notification>
<task-id>abc</task-id>
<tool-use-id>t3</tool-use-id>
<output-file>/tmp/tasks/abc.output</output-file>
<status>completed</status>
<summary>Agent "Implement fix" finished</summary>
<note>A task-notification fires each time this agent stops.</note>
<result>{REPORT}</result>
</task-notification>"""


def assistant(request: str, *blocks: dict, out: int = 10) -> dict:
    return {
        "type": "assistant",
        "requestId": request,
        "timestamp": "2026-10-02T12:00:00Z",
        "message": {
            "content": list(blocks),
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": out,
            },
        },
    }


def user(content: str | list, **extra: object) -> dict:
    return {
        "type": "user",
        "timestamp": "2026-10-02T12:00:00Z",
        "message": {"content": content},
        **extra,
    }


def tool_use(tid: str, name: str, **tool_input: object) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def result(tid: str, content: str | list, error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tid,
        "content": content,
        "is_error": error,
    }


MAIN = [
    user("Fix the flaky test"),
    assistant(
        "r1",
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "Looking."},
    ),
    assistant("r1", tool_use("t1", "Bash", command="cat big.log", description="Read")),
    user([result("t1", BULKY)]),
    assistant("r2", tool_use("t2", "Read", file_path="/repo/missing.py")),
    user([result("t2", "File does not exist.", error=True)]),
    assistant(
        "r3",
        tool_use("t3", "Agent", description="Implement fix", prompt="Do it"),
    ),
    user([result("t3", [{"type": "text", "text": "Done, tests green."}])]),
    user(NOTIFICATION, origin={"kind": "task-notification"}),
    user("No, keep the old name", isMeta=False),
    user("<skill body that is long>", isMeta=True),
]

SUB = [
    user("Do it"),
    assistant("s1", tool_use("u1", "Edit", file_path="/repo/a.py")),
    user([result("u1", "ok")]),
    assistant("s2", tool_use("u2", "Bash", command="pytest")),
    user([result("u2", "1 failed", error=True)]),
]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.fixture
def config(tmp_path: Path) -> Path:
    project = tmp_path / "claude" / "projects" / "-work-repo"
    write_jsonl(project / "sess1.jsonl", MAIN)
    subagents = project / "sess1" / "subagents"
    write_jsonl(subagents / "agent-abc.jsonl", SUB)
    (subagents / "agent-abc.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Implement fix",
                "toolUseId": "t3",
            }
        )
    )
    return tmp_path / "claude"


def digest(config: Path, *args: str, cwd: str = "/work/repo") -> str:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config)}
    proc = subprocess.run(
        [str(SCRIPT), "--cwd", cwd, *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_bulky_output_is_sized_not_printed(config: Path) -> None:
    out = digest(config)
    assert BULKY not in out
    assert "Bash: cat big.log" in out
    assert "4000 chars ~1000 tok" in out


def test_errors_are_printed(config: Path) -> None:
    out = digest(config)
    assert "ERROR File does not exist." in out
    assert "ERROR 1 failed" in out


def test_user_turns_shown_and_meta_shortened(config: Path) -> None:
    out = digest(config)
    assert "USER Fix the flaky test" in out
    assert "USER No, keep the old name" in out
    assert "<skill body that is long>" not in out


def test_task_notification_is_one_line_without_the_report(config: Path) -> None:
    out = digest(config)
    assert 'NOTIFY abc completed: Agent "Implement fix" finished' in out
    assert "The fix is in." not in out
    assert "<task-notification>" not in out


def test_monitor_event_has_no_status_and_drops_its_event(tmp_path: Path) -> None:
    event = """<task-notification>
<task-id>b2</task-id>
<summary>Monitor event: "build -&gt; deploy"</summary>
<event>EXTEND: checkpoint_3 165/174</event>
If this event is something the user would act on now, send a PushNotification.
</task-notification>"""
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", [user(event)])
    out = digest(tmp_path)
    assert 'NOTIFY b2: Monitor event: "build -> deploy"' in out
    assert "checkpoint_3" not in out


def test_subagent_is_linked_and_digested(config: Path) -> None:
    out = digest(config)
    assert "Agent: Implement fix -> agent-abc" in out
    assert "== agent-abc (general-purpose: Implement fix)" in out
    assert "Edit: /repo/a.py" in out


def test_summary_totals_per_agent_and_tool(config: Path) -> None:
    summary = digest(config).split("\n== main")[0]
    # main: 3 calls, 1 error; ~tok = (4000 + 20 + 18) / 4
    assert "main" in summary and "agent-abc" in summary
    lines = {line.split()[0]: line.split() for line in summary.splitlines() if line}
    assert lines["main"][1:4] == ["3", "1", "1.0k"]
    assert lines["agent-abc"][1:3] == ["2", "1"]
    assert lines["Bash"][1:3] == ["2", "1"]


def test_api_usage_counts_each_request_once(config: Path) -> None:
    summary = digest(config).split("\n== main")[0]
    main = next(line for line in summary.splitlines() if line.startswith("main"))
    # 3 requests (r1 twice): context in 3 x 1105, out 3 x 10.
    assert main.split()[-2:] == ["3.3k", "30"]


def test_session_by_id(config: Path) -> None:
    assert "USER Fix the flaky test" in digest(config, "sess1")


def test_unknown_session_fails(config: Path) -> None:
    proc = subprocess.run(
        [str(SCRIPT), "--cwd", "/work/repo", "nope"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)},
    )
    assert proc.returncode == 1
    assert "nope" in proc.stderr


def test_summary_lists_biggest_outputs(config: Path) -> None:
    summary = digest(config).split("\n== main")[0]
    biggest = summary.split("biggest outputs")[1].strip().splitlines()
    assert biggest[0].split() == ["1.0k", "main", "Bash:", "cat", "big.log"]
    assert len(biggest) == 5
