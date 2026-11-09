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
