"""session-digest condenses a Claude Code session, subagents included, for a retrospective."""

import base64
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
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


@pytest.fixture(autouse=True)
def outside_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no session argument, session-digest reads $CLAUDE_CODE_SESSION_ID's log, so
    no test inherits the id of the session that runs the suite."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


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


def digest(
    config: Path, *args: str, cwd: str = "/work/repo", session_env: str | None = None
) -> str:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config)}
    if session_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_env
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
    assert main.split()[4:6] == ["3.3k", "30"]


def test_session_by_id(config: Path) -> None:
    assert "USER Fix the flaky test" in digest(config, "sess1")


def test_newest_session_found_from_a_subdirectory(config: Path) -> None:
    assert "USER Fix the flaky test" in digest(config, cwd="/work/repo/research/notes")


def other_session_written_last(config: Path) -> None:
    """A second session in the project, its log the newest, as one still running."""
    path = config / "projects" / "-work-repo" / "sess2.jsonl"
    write_jsonl(path, [user("Another session's turn")])
    later = (config / "projects" / "-work-repo" / "sess1.jsonl").stat().st_mtime + 60
    os.utime(path, (later, later))


def test_no_argument_reads_the_session_it_runs_in(config: Path) -> None:
    other_session_written_last(config)
    out = digest(config, session_env="sess1")
    assert "USER Fix the flaky test" in out
    assert "Another session's turn" not in out


def test_no_argument_reads_the_newest_session_outside_one(config: Path) -> None:
    other_session_written_last(config)
    assert "Another session's turn" in digest(config)


def test_no_argument_reads_the_newest_when_its_own_session_is_another_projects(
    config: Path,
) -> None:
    other_session_written_last(config)
    write_jsonl(
        config / "projects" / "-work-other" / "elsewhere.jsonl",
        [user("A session of another project")],
    )
    assert "Another session's turn" in digest(config, session_env="elsewhere")


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
    # Past the timeline's 400-character cut, within the 1000 an error prints whole.
    long_error = "trace line\n" + "detail " * 100 + "\nlast line of the error"
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


def hook_record(kind: str, tool_use_id: str, **fields: object) -> dict:
    return {
        "type": "attachment",
        "timestamp": "2026-10-02T12:00:00Z",
        "attachment": {
            "type": kind,
            "hookName": "PreToolUse:Bash",
            "toolUseID": tool_use_id,
            "hookEvent": "PreToolUse",
            **fields,
        },
    }


def hook_failed(tool_use_id: str, stderr: str, **extra: object) -> dict:
    fields = {"command": "bin/gate", "stderr": stderr, "stdout": "", "exitCode": 1}
    return hook_record("hook_non_blocking_error", tool_use_id, **(fields | extra))


def hook_timed_out(tool_use_id: str, timed_out: bool = True) -> dict:
    return hook_record(
        "hook_cancelled",
        tool_use_id,
        command="pre-commit check",
        durationMs=900587,
        timedOut=timed_out,
        timeoutMs=900000,
    )


def hook_session(tmp_path: Path, records: list[dict]) -> Path:
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    return tmp_path


def timeline(out: str) -> list[str]:
    return out.split("\n== main")[1].splitlines()


def test_failed_hook_prints_its_last_error_line_under_its_call(tmp_path: Path) -> None:
    out = digest(
        hook_session(
            tmp_path,
            [
                assistant("r1", tool_use("t1", "Bash", command="ls")),
                hook_failed(
                    "t1", "Traceback (most recent call last):\nValueError: bad"
                ),
                user([result("t1", "a.py")]),
            ],
        )
    )
    lines = timeline(out)
    call = next(i for i, line in enumerate(lines) if "Bash: ls" in line)
    assert (
        lines[call + 1].strip()
        == "HOOK ERROR PreToolUse:Bash bin/gate: ValueError: bad"
    )
    assert "Traceback" not in out


def test_hook_logged_before_its_call_still_prints_under_it(tmp_path: Path) -> None:
    out = digest(
        hook_session(
            tmp_path,
            [
                hook_failed("t2", "gate broke"),
                assistant("r1", tool_use("t1", "Bash", command="ls")),
                assistant("r1", tool_use("t2", "Bash", command="pwd")),
                user([result("t1", "a.py")]),
                user([result("t2", "/repo")]),
            ],
        )
    )
    lines = timeline(out)
    call = next(i for i, line in enumerate(lines) if "Bash: pwd" in line)
    assert lines[call + 1].strip() == "HOOK ERROR PreToolUse:Bash bin/gate: gate broke"
    assert sum("HOOK ERROR" in line for line in lines) == 1


@pytest.mark.parametrize(
    ("record", "error"),
    [
        (hook_timed_out("t1"), "timed out after 900s"),
        (hook_timed_out("t1", timed_out=False), "cancelled"),
        (hook_failed("t1", "", stdout="warn\nfrom stdout\n"), "from stdout"),
        (hook_failed("t1", "", exitCode=3), "exit 3"),
        (hook_failed("t1", " \n", stdout="from stdout"), "from stdout"),
    ],
)
def test_hook_error_text(tmp_path: Path, record: dict, error: str) -> None:
    out = digest(
        hook_session(
            tmp_path,
            [assistant("r1", tool_use("t1", "Bash", command="ls")), record],
        )
    )
    assert (
        f"HOOK ERROR PreToolUse:Bash {record['attachment']['command']}: {error}" in out
    )


def test_hook_for_no_call_prints_where_it_ran(tmp_path: Path) -> None:
    stop_hook = hook_failed("", "stop hook broke", hookName="Stop", hookEvent="Stop")
    out = digest(
        hook_session(
            tmp_path,
            [
                user("Fix it"),
                assistant("r1", {"type": "text", "text": "Fixed."}),
                stop_hook,
                user("Thanks"),
            ],
        )
    )
    lines = [line.strip() for line in timeline(out)]
    hook = lines.index("HOOK ERROR Stop bin/gate: stop hook broke")
    assert "Fixed." in lines[hook - 1]
    assert "USER Thanks" in lines[hook + 1]


def test_summary_counts_hook_errors_per_hook_across_agents(config: Path) -> None:
    project = config / "projects" / "-work-repo"
    write_jsonl(
        project / "sess1.jsonl",
        [*MAIN, hook_failed("t1", "x"), hook_failed("t2", "y"), hook_timed_out("t1")],
    )
    write_jsonl(
        project / "sess1" / "subagents" / "agent-abc.jsonl",
        [*SUB, hook_failed("u2", "z")],
    )
    summary = digest(config).split("\n== main")[0]
    counts = summary.split("hook errors")[1].strip().splitlines()
    assert [line.split(None, 1) for line in counts] == [
        ["3", "PreToolUse:Bash bin/gate"],
        ["1", "PreToolUse:Bash pre-commit check"],
    ]


def test_summary_has_no_hook_section_without_hook_errors(config: Path) -> None:
    assert "hook errors" not in digest(config)


def test_workflow_agent_hook_errors_print_with_their_call(
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
    write_jsonl(
        run / "agent-w9.jsonl",
        [
            assistant("w1", tool_use("v1", "Bash", command="echo fine")),
            hook_failed("v1", "gate broke"),
            user([result("v1", "fine")]),
        ],
    )
    out = digest(workflow_config)
    assert "failed calls" in out
    section = out.split("failed calls")[-1]
    assert "abc123:build:harness" in section
    lines = [line.strip() for line in section.splitlines()]
    call = lines.index("> Bash: echo fine")
    assert lines[call + 1] == "HOOK ERROR PreToolUse:Bash bin/gate: gate broke"


QUIET = [
    user("Look around"),
    assistant("q1", {"type": "text", "text": "Listing."}),
    assistant("q2", tool_use("q3", "Bash", command="ls")),
    user([result("q3", "a.py")]),
]


@pytest.fixture
def errors_config(config: Path) -> Path:
    subagents = config / "projects" / "-work-repo" / "sess1" / "subagents"
    write_jsonl(subagents / "agent-quiet.jsonl", QUIET)
    (subagents / "agent-quiet.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "Look around"})
    )
    return config


def sections(out: str) -> dict[str, list[str]]:
    """Each agent's section after the summary, by its header line."""
    found: dict[str, list[str]] = {}
    for chunk in out.split("\n== ")[1:]:
        header, *lines = chunk.splitlines()
        found[header] = [line for line in lines if line]
    return found


def test_errors_prints_the_users_turns_and_failed_calls_in_order(
    errors_config: Path,
) -> None:
    out = sections(digest(errors_config, "--errors"))
    assert out["main"] == [
        "USER Fix the flaky test",
        "  > Read: /repo/missing.py",
        "    ERROR File does not exist.",
        "USER No, keep the old name",
    ]


def test_errors_prints_a_subagents_failures_without_its_prompt(
    errors_config: Path,
) -> None:
    out = sections(digest(errors_config, "--errors"))
    assert out["agent-abc (general-purpose: Implement fix)"] == [
        "  > Bash: pytest",
        "    ERROR 1 failed",
    ]


def test_errors_gives_an_agent_with_nothing_to_show_no_section(
    errors_config: Path,
) -> None:
    out = digest(errors_config, "--errors")
    assert "agent-quiet" in out.split("\n== ")[0]  # its summary row stays
    assert "== agent-quiet" not in out
    assert set(sections(out)) == {"main", "agent-abc (general-purpose: Implement fix)"}


def test_errors_keeps_the_summary(errors_config: Path) -> None:
    def summary(out: str) -> str:
        return out.split("\n== ")[0]

    assert summary(digest(errors_config, "--errors")) == summary(digest(errors_config))


def failed_make(tmp_path: Path, error: str) -> list[str]:
    out = digest(
        hook_session(
            tmp_path,
            [
                assistant("r1", tool_use("t1", "Bash", command="make boom")),
                user([result("t1", error, error=True)]),
            ],
        ),
        "--errors",
    )
    return sections(out)["main"]


def test_errors_prints_an_error_of_up_to_1000_characters_whole(tmp_path: Path) -> None:
    error = "trace line\n" + "detail " * 100 + "\nlast line of the error"
    assert failed_make(tmp_path, error) == [
        "  > Bash: make boom",
        "    ERROR trace line",
        "          " + "detail " * 100,
        "          last line of the error",
    ]


def test_errors_prints_a_longer_errors_first_200_and_last_800_characters(
    tmp_path: Path,
) -> None:
    lines = "".join(f"line {i:04d}\n" for i in range(300))  # 10 characters each
    error = "Exit code 1\n" + lines + "FAILED test_x"
    section = "\n".join(failed_make(tmp_path, error))
    assert "ERROR Exit code 1" in section
    assert "line 0017" in section  # ends at character 200
    assert "line 0018" not in section
    assert f"…[{len(error) - 1000} chars]…" in section
    assert "line 0221" not in section
    assert "line 0222" in section  # the last 800: 79 lines and "FAILED test_x"
    assert section.endswith("FAILED test_x")


def test_errors_prints_a_failed_call_where_it_was_made(tmp_path: Path) -> None:
    out = digest(
        hook_session(
            tmp_path,
            [
                assistant("r1", tool_use("t1", "Bash", command="make")),
                task_notice("b2", "failed", "Background command failed"),
                user([result("t1", "boom", error=True)]),
            ],
        ),
        "--errors",
    )
    assert sections(out)["main"] == [
        "  > Bash: make",
        "    ERROR boom",
        "NOTIFY b2 failed: Background command failed",
    ]


def test_errors_prints_failed_hooks_with_their_call_and_where_they_ran(
    tmp_path: Path,
) -> None:
    stop_hook = hook_failed("", "stop hook broke", hookName="Stop", hookEvent="Stop")
    out = digest(
        hook_session(
            tmp_path,
            [
                user("Fix it"),
                assistant("r1", tool_use("t1", "Bash", command="ls")),
                hook_failed("t1", "gate broke"),
                user([result("t1", "a.py")]),
                assistant("r2", tool_use("t2", "Bash", command="pwd")),
                user([result("t2", "/repo")]),
                stop_hook,
            ],
        ),
        "--errors",
    )
    assert sections(out)["main"] == [
        "USER Fix it",
        "  > Bash: ls",
        "    HOOK ERROR PreToolUse:Bash bin/gate: gate broke",
        "    HOOK ERROR Stop bin/gate: stop hook broke",
    ]


def notice_text(task: str, status: str, summary: str) -> str:
    return (
        f"<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n<result>report</result>\n</task-notification>"
    )


def task_notice(task: str, status: str, summary: str) -> dict:
    return user(notice_text(task, status, summary))


def queued(mode: str | None, prompt: str, **extra: object) -> dict:
    """A prompt or notification that arrived while the agent worked, which Claude
    Code logs as an attachment rather than a user turn."""
    fields = {"commandMode": mode} if mode else {}
    return {
        "type": "attachment",
        "timestamp": "2026-10-02T12:00:00Z",
        "attachment": {
            "type": "queued_command",
            "prompt": prompt,
            **fields,
            **extra,
        },
    }


QUEUED = [
    user("Fix it"),
    queued("prompt", "Also check the docs", origin={"kind": "human"}),
    queued("task-notification", notice_text("b1", "completed", "Tests ran")),
    queued("task-notification", notice_text("b2", "failed", "Build failed")),
    queued(
        None, '<agent-message from="a1">\nScope update</agent-message>', isMeta=True
    ),
    queued(
        "prompt",
        '<cross-session-message from="s2">\nhi</cross-session-message>',
        isMeta=True,
    ),
]


def test_queued_prompts_and_notifications_join_the_timeline(tmp_path: Path) -> None:
    assert sections(digest(hook_session(tmp_path, QUEUED)))["main"] == [
        "USER Fix it",
        "USER Also check the docs",
        "NOTIFY b1 completed: Tests ran",
        "NOTIFY b2 failed: Build failed",
    ]


def test_errors_prints_queued_prompts_and_failed_notifications(tmp_path: Path) -> None:
    out = digest(hook_session(tmp_path, QUEUED), "--errors")
    assert sections(out)["main"] == [
        "USER Fix it",
        "USER Also check the docs",
        "NOTIFY b2 failed: Build failed",
    ]


def test_errors_prints_notifications_of_tasks_that_did_not_complete(
    tmp_path: Path,
) -> None:
    monitor = user(
        "<task-notification>\n<task-id>m1</task-id>\n"
        "<summary>Monitor event: tick</summary>\n</task-notification>"
    )
    out = digest(
        hook_session(
            tmp_path,
            [
                task_notice("b1", "completed", "Background command ran"),
                task_notice(
                    "b2", "failed", "Background command failed with exit code 1"
                ),
                task_notice("a3", "killed", 'Agent "probe" was stopped by Claude'),
                monitor,
            ],
        ),
        "--errors",
    )
    assert sections(out)["main"] == [
        "NOTIFY b2 failed: Background command failed with exit code 1",
        'NOTIFY a3 killed: Agent "probe" was stopped by Claude',
    ]


def test_errors_gives_a_workflow_agent_a_section_like_any_other(
    workflow_config: Path,
) -> None:
    out = digest(workflow_config, "--errors")
    assert sections(out)["abc123:build:harness (workflow phase Build)"] == [
        "  > Bash: make boom",
        "    ERROR boom: no rule",
    ]
    assert "failed calls" not in out


def test_errors_and_timelines_do_not_combine(config: Path) -> None:
    proc = subprocess.run(
        [str(SCRIPT), "--cwd", "/work/repo", "--errors", "--timelines"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)},
    )
    assert proc.returncode == 2
    assert "not allowed with" in proc.stderr


START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def timed_calls(*calls: tuple[str, float, bool]) -> list[dict]:
    """A session of Bash calls, each (command, seconds it took, failed), one after
    another from START."""
    records: list[dict] = [user("Go")]
    now = START
    for n, (command, seconds, failed) in enumerate(calls):
        call = assistant(f"r{n}", tool_use(f"t{n}", "Bash", command=command))
        call["timestamp"] = now.isoformat().replace("+00:00", "Z")
        now += timedelta(seconds=seconds)
        done = user([result(f"t{n}", "boom" if failed else "ok", error=failed)])
        done["timestamp"] = now.isoformat().replace("+00:00", "Z")
        records += [call, done]
    return records


def timed_digest(
    tmp_path: Path, *calls: tuple[str, float, bool], flags: tuple[str, ...] = ()
) -> str:
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", timed_calls(*calls))
    return digest(tmp_path, *flags)


def test_a_call_of_a_minute_or_more_shows_how_long_it_took(tmp_path: Path) -> None:
    out = sections(timed_digest(tmp_path, ("quick", 59, False), ("slow", 192, False)))
    assert out["main"][1:] == [
        "  > Bash: quick",
        "    = 2 chars ~0 tok",
        "  > Bash: slow",
        "    = 2 chars ~0 tok [3m12s]",
    ]


@pytest.mark.parametrize(
    ("seconds", "tag"),
    [
        (40 * 60 - 1, "[39m59s]"),
        (40 * 60, "[40m00s LONG]"),
        (47 * 60 + 3, "[47m03s LONG]"),
        (65 * 60 + 59, "[1h05m LONG]"),
    ],
)
def test_a_call_of_40_minutes_or_more_is_marked_long(
    tmp_path: Path, seconds: int, tag: str
) -> None:
    out = sections(timed_digest(tmp_path, ("wt-ship", seconds, False)))
    assert out["main"][-1] == f"    = 2 chars ~0 tok {tag}"


def test_a_slow_failed_call_shows_its_time_in_both_forms(tmp_path: Path) -> None:
    full = sections(timed_digest(tmp_path, ("make", 120, True)))
    assert full["main"][-1] == "    ERROR [2m00s] boom"
    short_form = sections(
        timed_digest(tmp_path, ("make", 120, True), flags=("--errors",))
    )
    assert short_form["main"][-1] == "    ERROR [2m00s] boom"


@pytest.mark.parametrize("stamp", [None, "not a time"])
def test_a_call_without_a_readable_timestamp_shows_no_time(
    tmp_path: Path, stamp: str | None
) -> None:
    records = timed_calls(("slow", 600, False))
    if stamp is None:
        del records[-1]["timestamp"]
    else:
        records[-1]["timestamp"] = stamp
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert sections(digest(tmp_path))["main"][-1] == "    = 2 chars ~0 tok"


def longest_calls(out: str) -> list[list[str]]:
    summary = out.split("\n== ")[0]
    assert "longest calls" in summary
    block = summary.split("longest calls")[1].strip().split("\n\n")[0]
    return [line.split() for line in block.splitlines()[1:]]  # after the legend


def test_summary_lists_the_five_longest_calls_of_a_minute_or_more(
    tmp_path: Path,
) -> None:
    calls = [(f"c{m}", m * 60, False) for m in (2, 6, 1, 3, 5, 4)]
    rows = longest_calls(timed_digest(tmp_path, *calls, ("fast", 30, False)))
    assert rows == [[f"{m}m00s", "main", "Bash:", f"c{m}"] for m in (6, 5, 4, 3, 2)]


def test_summary_lists_every_long_call_beyond_the_five(tmp_path: Path) -> None:
    calls = [(f"c{m}", m * 60, False) for m in (41, 42, 43, 44, 45, 46, 3)]
    rows = longest_calls(timed_digest(tmp_path, *calls))
    assert [row[-1] for row in rows] == ["c46", "c45", "c44", "c43", "c42", "c41"]
    assert rows[0] == ["46m00s", "LONG", "main", "Bash:", "c46"]


def test_summary_has_no_longest_calls_when_none_took_a_minute(config: Path) -> None:
    assert "longest calls" not in digest(config)


@pytest.mark.parametrize(
    "reply",
    [
        "Command running in background with ID: b5nf. Output is being written to: /t",
        "Async agent launched successfully. (This tool result is internal metadata)",
    ],
)
def test_a_call_started_in_the_background_shows_no_time(
    tmp_path: Path, reply: str
) -> None:
    # It returned at once; its result can be logged when the agent next wakes.
    records = timed_calls(("heavy -- make", 3 * 3600, False))
    records[-1]["message"]["content"][0]["content"] = reply
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    out = digest(tmp_path)
    assert (
        sections(out)["main"][-1] == f"    = {len(reply)} chars ~{len(reply) // 4} tok"
    )
    assert "longest calls" not in out


def test_a_call_moved_to_the_background_keeps_its_time(tmp_path: Path) -> None:
    reply = "Command was moved to the background (ID: bab) after running for 2m"
    records = timed_calls(("make", 120, False))
    records[-1]["message"]["content"][0]["content"] = reply
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert sections(digest(tmp_path))["main"][-1].endswith(" [2m00s]")


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
def test_a_wait_on_the_user_is_tagged_user_and_never_long(
    tmp_path: Path, tool: str
) -> None:
    records = timed_calls(("ask", 50 * 60, False))
    records[1]["message"]["content"][0]["name"] = tool
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    out = digest(tmp_path)
    assert sections(out)["main"][-1] == "    = 2 chars ~0 tok [50m00s user]"
    assert "longest calls" not in out


def test_a_call_of_exactly_a_minute_shows_its_time(tmp_path: Path) -> None:
    out = sections(timed_digest(tmp_path, ("make", 60, False)))
    assert out["main"][-1] == "    = 2 chars ~0 tok [1m00s]"


def test_a_call_whose_start_has_no_timestamp_shows_no_time(tmp_path: Path) -> None:
    records = timed_calls(("slow", 600, False))
    del records[1]["timestamp"]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert sections(digest(tmp_path))["main"][-1] == "    = 2 chars ~0 tok"


def test_a_timestamp_without_a_zone_is_read_as_utc(tmp_path: Path) -> None:
    records = timed_calls(("slow", 120, False))
    records[1]["timestamp"] = "2026-10-02T12:00:00"
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert sections(digest(tmp_path))["main"][-1] == "    = 2 chars ~0 tok [2m00s]"


def test_calls_of_one_batch_are_each_timed_from_the_previous_result(
    tmp_path: Path,
) -> None:
    # Three calls sent at once run one after another, three minutes each.
    def stamp(record: dict, minutes: int) -> dict:
        record["timestamp"] = (START + timedelta(minutes=minutes)).isoformat()
        return record

    records = [user("Go")]
    records += [
        stamp(assistant("r1", tool_use(f"t{n}", "Bash", command=f"wait{n}")), 0)
        for n in (1, 2, 3)
    ]
    records += [stamp(user([result(f"t{n}", "ok")]), 3 * n) for n in (1, 2, 3)]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    times = [line for line in sections(digest(tmp_path))["main"] if "= 2 chars" in line]
    assert times == ["    = 2 chars ~0 tok [3m00s]"] * 3


def request(
    rid: str,
    minutes: float | None,
    written: int,
    read: int = 20_000,
    life: str = "1h",
    text: str = "Next.",
) -> dict:
    """A reply of request rid at START + minutes (None: no timestamp) that wrote
    written tokens to a cache of lifetime life and read read tokens from it."""
    record = assistant(rid, {"type": "text", "text": text})
    if minutes is None:
        del record["timestamp"]
    else:
        record["timestamp"] = (START + timedelta(minutes=minutes)).isoformat()
    record["message"]["usage"] |= {
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read,
        "cache_creation": {f"ephemeral_{life}_input_tokens": written},
    }
    return record


def rebuilds(tmp_path: Path, *records: dict) -> list[str]:
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", list(records))
    out = sections(digest(tmp_path))["main"]
    return [line for line in out if "CACHE REBUILT" in line]


def test_a_cache_rebuild_gets_a_line_before_its_reply(tmp_path: Path) -> None:
    records = [request("r1", 0, 5000), request("r2", 161, 700_000, text="Back.")]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert sections(digest(tmp_path))["main"][-2:] == [
        "  CACHE REBUILT after 2h41m idle (1h cache): 700.0k tokens written",
        "  Back.",
    ]


def test_a_rebuild_after_a_short_gap_counts_with_its_cache_life(tmp_path: Path) -> None:
    lines = rebuilds(
        tmp_path, request("r1", 0, 5000), request("r2", 6, 271_615, life="5m")
    )
    assert lines == [
        "  CACHE REBUILT after 6m00s idle (5m cache): 271.6k tokens written"
    ]


def test_a_rebuild_after_a_user_turn_says_so(tmp_path: Path) -> None:
    lines = rebuilds(
        tmp_path, request("r1", 0, 5000), user("Carry on"), request("r2", 50, 300_000)
    )
    assert lines == [
        (
            "  CACHE REBUILT after 50m00s idle (1h cache): 300.0k tokens written,"
            " after a user turn"
        )
    ]


def test_a_user_turn_before_an_earlier_request_is_not_in_the_gap(
    tmp_path: Path,
) -> None:
    lines = rebuilds(
        tmp_path,
        request("r1", 0, 5000),
        user("Carry on"),
        request("r2", 1, 3000),
        request("r3", 70, 300_000),
    )
    assert lines == [
        "  CACHE REBUILT after 1h09m idle (1h cache): 300.0k tokens written"
    ]


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(
            [request("r1", 0, 5000), request("r2", 90, 99_999)], id="small write"
        ),
        pytest.param(
            [request("r1", 0, 5000), request("r2", 90, 150_000, read=300_000)],
            id="a big output on a warm cache",
        ),
        pytest.param([request("r1", 0, 700_000)], id="the agent's first request"),
    ],
)
def test_no_rebuild_line_for_what_is_not_one(tmp_path: Path, records: list) -> None:
    assert rebuilds(tmp_path, *records) == []


def test_a_rebuild_of_exactly_100k_tokens_counts(tmp_path: Path) -> None:
    assert rebuilds(tmp_path, request("r1", 0, 5000), request("r2", 1, 100_000))


def test_a_rebuild_is_counted_once_per_request(tmp_path: Path) -> None:
    lines = rebuilds(
        tmp_path,
        request("r1", 0, 5000),
        request("r2", 45, 200_000),
        request("r2", 46, 200_000, text="Same request, second block."),
    )
    assert len(lines) == 1


def test_a_rebuild_without_timestamps_leaves_out_the_idle_time(tmp_path: Path) -> None:
    lines = rebuilds(
        tmp_path, request("r1", None, 5000), request("r2", None, 700_000, life="5m")
    )
    assert lines == ["  CACHE REBUILT (5m cache): 700.0k tokens written"]


def test_a_reply_without_a_timestamp_keeps_the_idle_clock(tmp_path: Path) -> None:
    lines = rebuilds(
        tmp_path,
        request("r1", 0, 5000),
        request("r2", None, 3000),
        request("r3", 90, 700_000),
    )
    assert lines == [
        "  CACHE REBUILT after 1h30m idle (1h cache): 700.0k tokens written"
    ]


def test_summary_lists_every_cache_rebuild(tmp_path: Path) -> None:
    records = [
        request("r1", 0, 5000),
        request("r2", 68, 650_000),
        user("Hi again"),
        request("r3", 235, 700_000, life="5m"),
    ]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    summary = digest(tmp_path).split("\n== ")[0]
    block = summary.split("cache rebuilds")[1].strip().split("\n\n")[0]
    rows = [line.split() for line in block.splitlines()[1:]]  # after the legend
    assert rows == [
        ["1h08m", "650.0k", "1h", "main"],
        ["2h47m", "700.0k", "5m", "main", "after", "a", "user", "turn"],
    ]


def test_summary_has_no_cache_rebuilds_without_one(config: Path) -> None:
    assert "cache rebuilds" not in digest(config)


def test_a_rewrite_that_read_as_much_as_it_wrote_is_no_rebuild(tmp_path: Path) -> None:
    records = [request("r1", 0, 5000), request("r2", 90, 150_000, read=150_000)]
    assert rebuilds(tmp_path, *records) == []


def test_a_cache_of_both_lifetimes_is_named_by_the_hour(tmp_path: Path) -> None:
    second = request("r2", 90, 700_000, life="5m")
    second["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"] = 9
    lines = rebuilds(tmp_path, request("r1", 0, 5000), second)
    assert lines == [
        "  CACHE REBUILT after 1h30m idle (1h cache): 700.0k tokens written"
    ]


@pytest.mark.parametrize(
    ("tool_input", "shown"),
    [
        ({"offset": 200, "limit": 100}, "Read: /repo/big.py (offset 200, limit 100)"),
        ({"offset": 200}, "Read: /repo/big.py (offset 200)"),
        ({}, "Read: /repo/big.py"),
    ],
)
def test_a_read_shows_the_part_of_the_file_it_read(
    tmp_path: Path, tool_input: dict, shown: str
) -> None:
    records = [
        assistant("r1", tool_use("t1", "Read", file_path="/repo/big.py", **tool_input)),
        user([result("t1", "x")]),
    ]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert f"  > {shown}" in sections(digest(tmp_path))["main"]


def one_session(tmp_path: Path, records: list[dict], *flags: str) -> list[str]:
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    return sections(digest(tmp_path, *flags))["main"]


LONG_TEXT = "I think the flake is the margin. " * 30  # 990 characters


def test_whole_text_prints_the_assistants_text_whole(tmp_path: Path) -> None:
    records = [user("Go"), assistant("r1", {"type": "text", "text": LONG_TEXT})]
    assert (
        one_session(tmp_path, records, "--whole-text")[-1] == f"  {LONG_TEXT.strip()}"
    )
    cut = one_session(tmp_path, records)[-1]
    assert cut.endswith("chars]") and len(cut) < 450


def test_whole_text_prints_a_user_turn_up_to_10000_characters(tmp_path: Path) -> None:
    turn = "x" * 15_000
    whole = one_session(tmp_path, [user(turn)], "--whole-text")[0]
    assert whole == f"USER {'x' * 10_000}…[+5000 chars]"


def call_with_output(output: str | list) -> list[dict]:
    return [
        user("Go"),
        assistant("r1", tool_use("t1", "Bash", command="pytest")),
        user([result("t1", output)]),
    ]


def test_tails_print_an_outputs_last_three_lines(tmp_path: Path) -> None:
    out = one_session(tmp_path, call_with_output("a\nb\n\nc  d\ne\n\n"), "--tails")
    assert out[-4:] == ["    = 13 chars ~3 tok", "    | b", "    | c d", "    | e"]


def test_tails_keep_no_more_than_300_characters(tmp_path: Path) -> None:
    output = "\n".join(c * 200 for c in "abc")
    out = one_session(tmp_path, call_with_output(output), "--tails")
    # The line that doesn't fit whole keeps its end.
    assert out[-3:] == [
        "    = 602 chars ~150 tok",
        f"    | …{'b' * 100}",
        f"    | {'c' * 200}",
    ]


def test_a_tail_leaves_out_claude_codes_notices(tmp_path: Path) -> None:
    output = (
        "1 failed, 40 passed\n"
        "Shell cwd was reset to /home/carver/code/claudedev/llm-toolbox\n"
    )
    out = one_session(tmp_path, call_with_output(output), "--tails")
    assert out[-1] == "    | 1 failed, 40 passed"


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Agent"])
def test_file_and_agent_tools_get_no_tail(tmp_path: Path, tool: str) -> None:
    records = [
        user("Go"),
        assistant("r1", tool_use("t1", tool, file_path="/repo/a.py")),
        user([result("t1", "the end of the file")]),
    ]
    out = one_session(tmp_path, records, "--tails")
    assert out[-1].startswith("    = ")


def test_subagents_get_whole_text_and_tails(config: Path) -> None:
    subagents = config / "projects" / "-work-repo" / "sess1" / "subagents"
    write_jsonl(
        subagents / "agent-abc.jsonl",
        [
            user("Do it"),
            assistant("s1", {"type": "text", "text": LONG_TEXT}),
            assistant("s2", tool_use("u1", "Bash", command="pytest")),
            user([result("u1", "1 passed")]),
        ],
    )
    out = sections(digest(config, "--whole-text", "--tails"))
    sub = out["agent-abc (general-purpose: Implement fix)"]
    assert f"  {LONG_TEXT.strip()}" in sub
    assert "    | 1 passed" in sub


def test_subagents_written_at_once_keep_one_order(config: Path) -> None:
    # Twelve names, so the directory's own order is unlikely to be sorted too.
    subagents = config / "projects" / "-work-repo" / "sess1" / "subagents"
    names = [f"{c}{n}" for n, c in enumerate("qwertyuiopzx")]
    for name in names:
        write_jsonl(subagents / f"agent-{name}.jsonl", [user(f"Brief {name}")])
    for log in subagents.glob("agent-*.jsonl"):
        os.utime(log, (1_700_000_000, 1_700_000_000))
    headers = [h.split()[0] for h in sections(digest(config)) if h != "main"]
    assert headers == sorted(["agent-abc", *(f"agent-{n}" for n in names)])


def test_a_long_last_line_keeps_its_end(tmp_path: Path) -> None:
    output = "x" * 1000 + " 1 failed, 40 passed"
    out = one_session(tmp_path, call_with_output(output), "--tails")
    assert out[-1] == f"    | …{output[-300:]}"


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("", id="empty"),
        pytest.param("\n\n", id="blank"),
        pytest.param([{"type": "image", "source": {"data": ""}}], id="images only"),
    ],
)
def test_an_output_with_no_text_has_no_tail(tmp_path: Path, output: str | list) -> None:
    out = one_session(tmp_path, call_with_output(output), "--tails")
    assert out[-1].startswith("    = ")


def test_a_failed_call_gets_its_error_not_a_tail(tmp_path: Path) -> None:
    records = [
        user("Go"),
        assistant("r1", tool_use("t1", "Bash", command="pytest")),
        user([result("t1", "boom\n1 failed", error=True)]),
    ]
    out = one_session(tmp_path, records, "--tails")
    assert out[-1] == "    ERROR boom 1 failed"


@pytest.mark.parametrize("flag", ["--whole-text", "--tails"])
def test_whole_text_and_tails_do_not_go_with_errors(config: Path, flag: str) -> None:
    proc = subprocess.run(
        [str(SCRIPT), "--cwd", "/work/repo", "--errors", flag],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)},
    )
    assert proc.returncode == 2


SIDE_NOTE_TURN = (
    "Here is a note offered by a side agent:\n\n> Heads up · A force-push keeps the"
    " old commits on GitHub.\n\nOk, what now?"
)


@pytest.fixture
def side_note_config(tmp_path: Path) -> Path:
    project = tmp_path / "claude" / "projects" / "-work-repo"
    write_jsonl(
        project / "sess1.jsonl",
        [
            user("Fix the flaky test"),
            assistant("r1"),
            user(SIDE_NOTE_TURN),
            assistant("r2"),
        ],
    )
    return tmp_path / "claude"


@pytest.mark.parametrize("args", [(), ("--errors",)])
def test_a_side_agents_note_is_labelled_as_one(side_note_config: Path, args) -> None:
    out = digest(side_note_config, *args)
    assert "USER (side-agent note) > Heads up · A force-push keeps" in out
    assert "USER Fix the flaky test" in out
    assert "USER (side-agent note) Fix" not in out
    assert "Here is a note offered" not in out


def reply(
    request: str, *blocks: dict, stop: str = "end_turn", context: int = 0
) -> dict:
    """An assistant record that ends with stop_reason ``stop``; ``context`` tokens
    read from the cache when given."""
    record = assistant(request, *blocks)
    record["message"]["stop_reason"] = stop
    if context:
        record["message"]["usage"]["cache_read_input_tokens"] = context
    return record


def text(words: str) -> dict:
    return {"type": "text", "text": words}


def summary_rows(out: str) -> dict[str, list[str]]:
    table = out.split("\n\n")[0].splitlines()
    return {line.split()[0]: line.split() for line in table[1:]}


def test_summary_shows_each_agents_peak_context_and_messages(tmp_path: Path) -> None:
    """peak-in is the largest single request's context; msgs counts what another agent
    sent (coordinator or peer), whether it arrived mid-work or after a stop."""
    records = [
        user("Build it"),
        reply("r1", text("Started."), context=40_000),
        queued(None, "Scope change", isMeta=True, origin={"kind": "coordinator"}),
        reply("r2", text("Adjusted."), context=250_000),
        user("A follow-up", isMeta=True, origin={"kind": "coordinator"}),
        user("From a peer", isMeta=True, origin={"kind": "peer"}),
        user("Hi", origin={"kind": "human"}),
        reply("r3", text("Done."), context=120_000),
    ]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    out = digest(tmp_path)
    assert summary_rows(out)["agent"][-3:] == [
        "peak-in",
        "msgs",
        "wakes",
    ]
    # 5 input + 100 written + the cache read
    assert summary_rows(out)["main"][-3:] == ["250.1k", "3", "0"]


def test_wakes_count_notification_turns_that_made_no_tool_call(tmp_path: Path) -> None:
    """A notification that starts a turn which only talks is a status-only turn; one
    the agent acts on, or one that arrives while it works, isn't."""
    notice = notice_text("b1", "completed", "Agent finished")
    records = [
        user("Go"),
        reply("r1", text("Waiting.")),
        user(notice, origin={"kind": "task-notification"}),
        reply("r2", text("Still waiting on the other one.")),
        user(notice, origin={"kind": "task-notification"}),
        reply("r3", text("Noted.")),
        user(notice, origin={"kind": "task-notification"}),
        reply("r4", tool_use("t1", "Bash", command="cat log"), stop="tool_use"),
        user([result("t1", "ok")]),
        queued("task-notification", notice, origin={"kind": "task-notification"}),
        reply("r5", text("Done.")),
    ]
    write_jsonl(tmp_path / "projects" / "-work-repo" / "s.jsonl", records)
    assert summary_rows(digest(tmp_path))["main"][-1] == "2"


def big_session(tmp_path: Path, peak: int, msgs: int, wakes: int = 0) -> str:
    """A main session and one subagent with these figures; the digest's summary."""
    project = tmp_path / "projects" / "-work-repo"
    notice = notice_text("a", "completed", "Agent finished")
    main = [user("Go"), reply("m1", text("Waiting."))]
    for n in range(wakes):
        main += [user(notice, origin={"kind": "task-notification"})]
        main += [reply(f"m{n + 2}", text("Still waiting."))]
    write_jsonl(project / "s.jsonl", main)
    sub = [user("Brief"), reply("s1", text("Done."), context=peak)]
    for n in range(msgs):
        sub += [user("More work", isMeta=True, origin={"kind": "coordinator"})]
        sub += [reply(f"s{n + 2}", text("Done."), context=peak)]
    write_jsonl(project / "s" / "subagents" / "agent-big.jsonl", sub)
    return digest(tmp_path).split("\n== ")[0]


def section(summary: str, title: str) -> list[str]:
    """A summary section's rows, after its title and legend; [] when it's absent."""
    if f"\n{title}\n" not in summary:
        return []
    return summary.split(f"\n{title}\n")[1].split("\n\n")[0].splitlines()[1:]


@pytest.mark.parametrize(
    ("peak", "msgs", "listed"),
    [
        (200_000, 0, True),
        (150_000, 2, True),
        (199_000, 1, False),
        (90_000, 3, False),
    ],
    ids=["peak", "resumed", "small", "resumed-but-small"],
)
def test_summary_lists_big_subagents(
    tmp_path: Path, peak: int, msgs: int, listed: bool
) -> None:
    rows = section(big_session(tmp_path, peak, msgs), "big subagents")
    if listed:
        assert [row.split() for row in rows] == [
            [human_k(peak + 105), str(msgs), "msgs", "agent-big"]
        ]
    else:
        assert rows == []


def human_k(n: int) -> str:
    return f"{n / 1000:.1f}k"


def test_summary_lists_agents_woken_often_for_nothing(tmp_path: Path) -> None:
    assert [
        r.split() for r in section(big_session(tmp_path, 1, 0, 5), "idle wakes")
    ] == [["5", "main"]]
    assert section(big_session(tmp_path / "few", 1, 0, 4), "idle wakes") == []
