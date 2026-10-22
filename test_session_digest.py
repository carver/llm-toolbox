"""session-digest condenses a Claude Code session, subagents included, for a retrospective."""

import base64
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


def test_newest_session_found_from_a_subdirectory(config: Path) -> None:
    assert "USER Fix the flaky test" in digest(config, cwd="/work/repo/research/notes")


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


WORKFLOW = [
    user("Build the harness"),
    assistant("w1", tool_use("v1", "Bash", command="echo fine")),
    user([result("v1", "fine")]),
    assistant("w2", tool_use("v2", "Bash", command="make boom")),
    user([result("v2", "boom: no rule", error=True)]),
]


@pytest.fixture
def workflow_config(config: Path) -> Path:
    run = (
        config
        / "projects"
        / "-work-repo"
        / "sess1"
        / "subagents"
        / "workflows"
        / "wf_abc123"
    )
    write_jsonl(run / "agent-w9.jsonl", WORKFLOW)
    (run / "agent-w9.meta.json").write_text(
        json.dumps(
            {
                "agentType": "workflow-subagent",
                "description": "build:harness",
                "workflowPhase": "Build",
            }
        )
    )
    return config


def test_workflow_agent_gets_a_summary_row_named_by_run_and_label(
    workflow_config: Path,
) -> None:
    summary = digest(workflow_config).split("\n== main")[0]
    rows = {line.split()[0]: line.split() for line in summary.splitlines() if line}
    assert rows["abc123:build:harness"][1:3] == ["2", "1"]
    assert rows["Bash"][1:3] == ["4", "2"]


def test_workflow_agent_errors_print_with_their_call_but_not_its_timeline(
    workflow_config: Path,
) -> None:
    out = digest(workflow_config)
    section = out.split("abc123:build:harness")[-1]
    assert "Bash: make boom" in section
    assert "ERROR boom: no rule" in section
    assert "echo fine" not in out


def test_timelines_flag_prints_workflow_agents_timelines(workflow_config: Path) -> None:
    out = digest(workflow_config, "--timelines")
    assert "Bash: echo fine" in out
    assert "USER Build the harness" in out


def png_header(width: int, height: int) -> str:
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    raw = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\0" * 64
    return base64.b64encode(raw).decode()


def gif_header(width: int, height: int) -> str:
    raw = (
        b"GIF89a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\0" * 16
    )
    return base64.b64encode(raw).decode()


def jpeg_header(width: int, height: int) -> str:
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\0" + b"\0" * 9
    sof0 = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
    sof0 += height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\0" * 10
    return base64.b64encode(b"\xff\xd8" + app0 + sof0).decode()


def image_session(tmp_path: Path, data: str) -> Path:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }
    write_jsonl(
        tmp_path / "projects" / "-work-repo" / "s.jsonl",
        [
            assistant("r1", tool_use("t1", "Read", file_path="/repo/page.png")),
            user([result("t1", [image])]),
        ],
    )
    return tmp_path


@pytest.mark.parametrize(
    ("data", "tokens"),
    [
        (png_header(680, 880), 798),  # 680 * 880 / 750 = 797.9
        (png_header(3000, 2000), 1600),  # scaled to 1568 on the long edge, then capped
        (png_header(4000, 400), 328),  # scaled to 1568 x 156.8: 327.8, not 1600
        (gif_header(100, 50), 7),  # 100 * 50 / 750 = 6.7
        (
            jpeg_header(1000, 800),
            1067,
        ),  # 1000 * 800 / 750 = 1066.7, past an APP0 segment
        (base64.b64encode(b"not an image at all" * 4).decode(), 1600),  # size unknown
    ],
)
def test_image_tokens_come_from_its_pixel_size(
    tmp_path: Path, data: str, tokens: int
) -> None:
    out = digest(image_session(tmp_path, data))
    assert f"0 chars ~{tokens} tok (1 images)" in out


def test_workflow_agent_errors_are_printed_in_full(workflow_config: Path) -> None:
    run = (
        workflow_config
        / "projects"
        / "-work-repo"
        / "sess1"
        / "subagents"
        / "workflows"
    )
    long_error = "trace line\n" + "detail " * 200 + "\nlast line of the error"
    write_jsonl(
        run / "wf_abc123" / "agent-w9.jsonl",
        [
            assistant("w1", tool_use("v1", "Bash", command="make boom")),
            user([result("v1", long_error, error=True)]),
        ],
    )
    out = digest(workflow_config)
    assert "last line of the error" in out
    assert "…[+" not in out.split("failed calls")[-1]


def test_two_agents_with_one_label_in_a_run_get_distinct_names(
    workflow_config: Path,
) -> None:
    run = (
        workflow_config
        / "projects"
        / "-work-repo"
        / "sess1"
        / "subagents"
        / "workflows"
        / "wf_abc123"
    )
    write_jsonl(run / "agent-x7.jsonl", WORKFLOW)
    (run / "agent-x7.meta.json").write_text(
        json.dumps({"description": "build:harness"})
    )
    agent_table = digest(workflow_config).split("\n\n")[0]
    names = [
        line.split()[0] for line in agent_table.splitlines() if "build:harness" in line
    ]
    assert sorted(names) == ["abc123:build:harness#w9", "abc123:build:harness#x7"]
