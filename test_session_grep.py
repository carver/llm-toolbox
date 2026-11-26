"""session-grep searches the commands sessions ran, never their outputs."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "session-grep"
SESSION = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
PROJECT = "-work-repo"
RESET = "git reset --hard HEAD~1"


def call(
    command: str,
    when: str = "2026-10-02T12:00:00.000Z",
    cwd: str | None = "/work/repo",
    name: str = "Bash",
    **tool_input: object,
) -> dict:
    record = {
        "type": "assistant",
        "timestamp": when,
        "sessionId": SESSION,
        "message": {
            "content": [
                {"type": "text", "text": "Running it."},
                {
                    "type": "tool_use",
                    "id": f"t-{when}",
                    "name": name,
                    "input": {"command": command, **tool_input}
                    if name == "Bash"
                    else tool_input,
                },
            ]
        },
    }
    if cwd is not None:
        record["cwd"] = cwd
    return record


def output(
    text: str, when: str = "2026-10-02T12:00:01.000Z", call_id: str = "t"
) -> dict:
    return {
        "type": "user",
        "timestamp": when,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": text}
            ]
        },
    }


def said(role: str, text: str) -> dict:
    return {
        "type": role,
        "timestamp": "2026-10-02T12:00:00.000Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def write_log(path: Path, *records: dict | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("".join(line + "\n" for line in lines))
    return path


def session_log(
    config: Path, *records: dict | str, project=PROJECT, session=SESSION
) -> Path:
    return write_log(config / "projects" / project / f"{session}.jsonl", *records)


def subagent_log(
    config: Path,
    name: str,
    description: str,
    *records: dict,
    project: str = PROJECT,
    session: str = SESSION,
    run: str | None = None,
) -> Path:
    folder = config / "projects" / project / session / "subagents"
    if run:
        folder = folder / "workflows" / run
    (folder).mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.meta.json").write_text(json.dumps({"description": description}))
    return write_log(folder / f"{name}.jsonl", *records)


def grep(config: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config), "TZ": "Asia/Tokyo"},
        timeout=60,
    )


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "claude"


def lines(proc: subprocess.CompletedProcess) -> list[str]:
    return proc.stdout.splitlines()


def test_a_match_prints_time_session_agent_cwd_and_command(config: Path) -> None:
    session_log(config, call(RESET), call("git status"))
    proc = grep(config, "reset --hard")
    assert proc.returncode == 0, proc.stderr
    # The log's UTC noon is 21:00 in Tokyo.
    assert lines(proc) == [f"2026-10-02 21:00:00  11111111  main  /work/repo  {RESET}"]


def test_full_prints_the_project_and_whole_ids(config: Path) -> None:
    subagent_log(config, "agent-0123456789abcdef", "Fix the bug", call(RESET))
    short = "agent-01234567 (Fix the bug)  /work/repo"
    assert lines(grep(config, "reset")) == [
        f"2026-10-02 21:00:00  11111111  {short}  {RESET}"
    ]
    long = "agent-0123456789abcdef (Fix the bug)  /work/repo"
    assert lines(grep(config, "--full", "reset")) == [
        f"2026-10-02 21:00:00  {PROJECT}  {SESSION}  {long}",
        f"    {RESET}",
    ]


def test_a_cwd_under_home_starts_with_a_tilde(config: Path, tmp_path: Path) -> None:
    session_log(config, call("echo x", cwd=str(tmp_path / "home" / "repo")))
    proc = subprocess.run(
        [str(SCRIPT), "echo"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(config),
            "HOME": str(tmp_path / "home"),
        },
        timeout=60,
    )
    assert "  main  ~/repo  echo x" in proc.stdout


def test_the_pattern_matches_the_command_as_typed_not_its_json(config: Path) -> None:
    command = 'printf "%s\\n" "a b" | grep -c \'"quoted"\''
    session_log(config, call(command))
    proc = grep(config, "-F", '"%s\\n" "a b"')
    assert proc.returncode == 0, proc.stderr
    assert lines(proc)[0].endswith(command)
    assert grep(config, "-F", '"quoted"').returncode == 0


def test_a_command_of_several_lines_prints_on_one(config: Path) -> None:
    session_log(config, call("cd /x\nmake test"))
    assert lines(grep(config, "make"))[0].endswith("cd /x ⏎ make test")


def test_outputs_and_message_text_are_never_searched(config: Path) -> None:
    session_log(
        config,
        call("ls"),
        output("hunter2 in the output"),
        said("user", "hunter2 in a user turn"),
        said("assistant", "hunter2 in the assistant's text"),
    )
    proc = grep(config, "hunter2")
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_a_tool_output_that_quotes_a_tool_call_is_not_a_call(config: Path) -> None:
    quoted = json.dumps(call("hunter2 --quoted"))
    session_log(config, output(quoted))
    assert grep(config, "hunter2").returncode == 1


def test_subagents_and_workflow_agents_are_searched_and_named(config: Path) -> None:
    session_log(config, call("echo main", when="2026-10-02T12:00:00Z"))
    subagent_log(
        config,
        "agent-abc",
        "Fix the bug",
        call("echo sub", when="2026-10-02T12:01:00Z"),
    )
    subagent_log(
        config,
        "agent-def",
        "Review",
        call("echo flow", when="2026-10-02T12:02:00Z"),
        run="wf_run1",
    )
    found = lines(grep(config, "echo"))
    assert [line.split("  ")[2] for line in found] == [
        "main",
        "agent-abc (Fix the bug)",
        "run1:Review",
    ]
    assert all(line.split("  ")[1] == SESSION[:8] for line in found)


def test_a_subagent_without_its_main_log_is_still_searched(config: Path) -> None:
    subagent_log(config, "agent-abc", "Fix", call("echo orphan"))
    assert "agent-abc (Fix)" in grep(config, "orphan").stdout


def test_matches_from_every_project_print_in_time_order(config: Path) -> None:
    session_log(config, call("echo late", when="2026-10-02T13:00:00Z"))
    session_log(
        config,
        call("echo early", when="2026-10-02T11:00:00Z"),
        project="-work-other",
        session=OTHER,
    )
    found = lines(grep(config, "echo"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["early", "late"]


def test_tool_picks_another_tool_and_its_inputs_are_searched(config: Path) -> None:
    session_log(
        config,
        call("cat notes.txt"),
        call("", name="Read", file_path="/work/repo/notes.txt", offset=10),
    )
    assert lines(grep(config, "notes"))[0].endswith("cat notes.txt")
    proc = grep(config, "--tool", "Read", "notes")
    read = "Read: file_path=/work/repo/notes.txt offset=10"
    assert lines(proc) == [f"2026-10-02 21:00:00  11111111  main  /work/repo  {read}"]


def test_tool_takes_a_glob(config: Path) -> None:
    session_log(
        config,
        call("", name="mcp__docs__read", path="a.md"),
        call("", name="Write", file_path="a.md", content="x"),
    )
    found = lines(grep(config, "--tool", "mcp__*", "a.md"))
    assert len(found) == 1
    assert "mcp__docs__read: path=a.md" in found[0]


def test_since_and_until_take_times_in_local_time(config: Path) -> None:
    session_log(
        config,
        call("echo one", when="2026-10-02T11:00:00Z"),
        call("echo two", when="2026-10-02T12:00:00Z"),
        call("echo three", when="2026-10-02T13:00:00Z"),
    )
    # 21:00 in Tokyo is noon UTC.
    found = lines(grep(config, "echo", "--since", "2026-10-02 20:30"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["two", "three"]
    found = lines(
        grep(
            config, "echo", "--since", "2026-10-02 20:30", "--until", "2026-10-02T21:30"
        )
    )
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["two"]


def test_since_takes_a_duration(config: Path) -> None:
    now = datetime.now(UTC)

    def ago(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    session_log(config, call("echo old", when=ago(5)), call("echo new", when=ago(0.5)))
    found = lines(grep(config, "echo", "--since", "2h"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["new"]
    found = lines(grep(config, "echo", "--until", "90m"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["old"]


def test_since_skips_a_log_last_written_before_it(config: Path) -> None:
    # A log can't hold a record newer than its last write, so it isn't read at all:
    # this one's record claims to be new, but the file is a year old.
    log = session_log(config, call("echo stale", when=datetime.now(UTC).isoformat()))
    year_ago = (datetime.now(UTC) - timedelta(days=365)).timestamp()
    os.utime(log, (year_ago, year_ago))
    assert grep(config, "stale", "--since", "1d").returncode == 1


def test_a_clock_time_means_today(config: Path) -> None:
    now = datetime.now(UTC)
    session_log(
        config,
        call("echo yesterday", when=(now - timedelta(days=1)).isoformat()),
        call("echo today", when=now.isoformat()),
    )
    found = lines(grep(config, "echo", "--since", "0:00"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["today"]


def test_a_clock_time_takes_seconds_and_one_digit_hours(config: Path) -> None:
    session_log(config, call("echo x", when=datetime.now(UTC).isoformat()))
    for clock in ("0:00", "0:00:00", "00:00:00"):
        assert grep(config, "echo", "--since", clock).returncode == 0, clock


def test_until_a_date_means_the_end_of_that_day(config: Path) -> None:
    session_log(
        config,
        # 23:30 on October 2nd in Tokyo, then 00:30 on the 3rd.
        call("echo late", when="2026-10-02T14:30:00Z"),
        call("echo next", when="2026-10-02T15:30:00Z"),
    )
    found = lines(grep(config, "echo", "--until", "2026-10-02"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["late"]


def test_the_running_search_does_not_find_itself(config: Path) -> None:
    session_log(
        config,
        call("session-grep needle --since 1h", when="2026-10-02T12:00:00Z"),
        output("needle found", call_id="t-2026-10-02T12:00:00Z"),
        call("cd /x && session-grep needle", when="2026-10-02T12:05:00Z"),
    )
    # The first call has its result, so it ran and is found; the second, with none
    # yet, is the search running now.
    found = lines(grep(config, "needle"))
    assert [line.split("  ", 4)[4] for line in found] == [
        "session-grep needle --since 1h"
    ]
    session_log(config, call("session-grep needle"))
    assert grep(config, "needle").returncode == 1


def test_a_vanished_subagent_log_is_skipped(config: Path) -> None:
    subagent_log(config, "agent-abc", "Fix", call("echo here"))
    folder = config / "projects" / PROJECT / SESSION / "subagents"
    # A link to nothing: listed by the folder's glob, gone when it is read.
    (folder / "agent-gone.jsonl").symlink_to(folder / "nowhere.jsonl")
    proc = grep(config, "echo")
    assert proc.returncode == 0, proc.stderr
    assert "echo here" in proc.stdout


def test_a_bad_time_is_a_usage_error(config: Path) -> None:
    session_log(config, call("echo x"))
    proc = grep(config, "x", "--since", "yesterday-ish")
    assert proc.returncode == 2
    assert "--since" in proc.stderr


def test_project_takes_a_glob(config: Path) -> None:
    session_log(config, call("echo here"))
    session_log(config, call("echo there"), project="-work-other", session=OTHER)
    found = lines(grep(config, "echo", "--project", "*other"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["there"]


def test_cwd_takes_a_folder_and_what_is_under_it(config: Path) -> None:
    session_log(
        config,
        call("echo top", cwd="/work/repo"),
        call("echo deep", cwd="/work/repo/src"),
        call("echo sibling", cwd="/work/repo2"),
        call("echo nowhere", cwd=None),
    )
    found = lines(grep(config, "echo", "--cwd", "/work/repo"))
    assert [line.rsplit(" ", 1)[-1] for line in found] == ["top", "deep"]


def test_a_call_with_no_cwd_shows_a_dash(config: Path) -> None:
    session_log(config, call("echo x", cwd=None))
    assert f"  main  {PROJECT}  echo x" in grep(config, "echo").stdout


def test_a_long_command_is_cut_around_its_match(config: Path) -> None:
    head = "echo " + "a" * 300
    session_log(config, call(f"{head} needle " + "b" * 300))
    line = lines(grep(config, "needle"))[0]
    command = line.split("  ", 4)[4]
    assert "needle" in command
    assert command.startswith("…") and command.endswith("…")
    assert len(command) <= 160


def test_a_long_command_matched_early_is_cut_at_its_end(config: Path) -> None:
    session_log(config, call("needle " + "b" * 300))
    command = lines(grep(config, "needle"))[0].split("  ", 4)[4]
    assert command.startswith("needle b")
    assert command.endswith("…")
    assert len(command) <= 160


def test_full_prints_the_whole_command_under_its_line(config: Path) -> None:
    command = "needle " + "b" * 300 + "\nsecond line"
    session_log(config, call(command))
    out = lines(grep(config, "--full", "needle"))
    assert out == [
        f"2026-10-02 21:00:00  {PROJECT}  {SESSION}  main  /work/repo",
        "    needle " + "b" * 300,
        "    second line",
    ]


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        ("GH_TOKEN=s3cr3tvalue gh api user", "s3cr3tvalue"),
        ("export API_KEY='s3cr3t value'", "s3cr3t value"),
        ('curl -H "Authorization: Bearer s3cr3tvalue" https://x', "s3cr3tvalue"),
        ("curl -H 'x-api-key: s3cr3tvalue' https://x", "s3cr3tvalue"),
        ("tool --password s3cr3tvalue --other", "s3cr3tvalue"),
        ("tool --client-secret=s3cr3tvalue", "s3cr3tvalue"),
        ("git clone https://me:s3cr3tvalue@example.com/r.git", "s3cr3tvalue"),
        ("echo ghp_" + "A" * 36, "ghp_" + "A" * 36),
        ("echo sk-ant-" + "b" * 30, "sk-ant-" + "b" * 30),
        ("echo AKIA" + "C" * 16, "AKIA" + "C" * 16),
        ("STRIPE_KEY=s3cr3tvalue node app.js", "s3cr3tvalue"),
        (
            """curl -d '{"Authorization": "Bearer s3cr3tvalue"}' https://x""",
            "s3cr3tvalue",
        ),
        ("echo sk_live_" + "d" * 24, "sk_live_" + "d" * 24),
        ("echo hf_" + "e" * 30, "hf_" + "e" * 30),
        ("echo glpat-" + "f" * 20, "glpat-" + "f" * 20),
        ("echo AIza" + "g" * 35, "AIza" + "g" * 35),
        ("echo eyJhbGciOiJI.eyJzdWIiOjEy.c2lnbmF0dXJl", "eyJhbGciOiJI"),
    ],
)
def test_secret_shaped_values_are_masked_in_every_form(
    config: Path, command: str, secret: str
) -> None:
    session_log(config, call(f"{command}  # marker"))
    for args in (["marker"], ["--full", "marker"]):
        proc = grep(config, *args)
        assert proc.returncode == 0, proc.stderr
        assert secret not in proc.stdout
        assert "***" in proc.stdout


def test_a_pattern_inside_a_masked_value_still_matches(config: Path) -> None:
    session_log(config, call("GH_TOKEN=s3cr3tvalue gh api user"))
    proc = grep(config, "s3cr3t")
    assert proc.returncode == 0
    assert "GH_TOKEN=*** gh api user" in proc.stdout
    assert "s3cr3t" not in proc.stdout


@pytest.mark.parametrize(
    "command",
    [
        'GH_TOKEN="$(cat ~/.token)" gh api user',
        "git commit --author='A B <a@b.c>' -m x",
        "tool --token-file /path/to/file",
        "grep -rn password src/",
        "rg token lib/",
        "gh secret set FOO",
        "sort --key=2 data.txt",
    ],
)
def test_a_reference_to_a_secret_is_left_as_typed(config: Path, command: str) -> None:
    session_log(config, call(command))
    assert lines(grep(config, "-F", command[:8]))[0].endswith(command)


def test_max_keeps_the_newest_matches_and_says_how_many_it_left_out(
    config: Path,
) -> None:
    session_log(
        config,
        *(call(f"echo {n}", when=f"2026-10-02T12:00:0{n}Z") for n in range(5)),
    )
    proc = grep(config, "echo", "--max", "2")
    assert [line.rsplit(" ", 1)[-1] for line in lines(proc)] == ["3", "4"]
    assert "3 older matches left out" in proc.stderr


def test_ignore_case(config: Path) -> None:
    session_log(config, call("Make Test"))
    assert grep(config, "make test").returncode == 1
    assert grep(config, "-i", "make test").returncode == 0


def test_a_bad_pattern_is_a_usage_error(config: Path) -> None:
    session_log(config, call("echo x"))
    proc = grep(config, "(")
    assert proc.returncode == 2
    assert "session-grep:" in proc.stderr


def test_a_broken_line_is_skipped(config: Path) -> None:
    session_log(
        config,
        '{"type": "assistant", "message": {"content": [{"type": "tool_use"',
        call("echo ok"),
    )
    assert grep(config, "ok").returncode == 0


def test_no_projects_folder_is_no_match(config: Path) -> None:
    proc = grep(config, "x")
    assert proc.returncode == 1
    assert "no session logs" in proc.stderr


# 21:10 in Tokyo, which is 12:10 UTC.
AT = "2026-10-02 21:10"
EXACTLY = "2026-10-02T12:10:00Z"
BEFORE = "2026-10-02T12:06:00Z"
AFTER = "2026-10-02T12:20:00Z"
DONE = [
    call("echo done", when="2026-10-02T12:01:00Z"),
    output("", when="2026-10-02T12:01:05Z", call_id="t-2026-10-02T12:01:00Z"),
]


def started(command: str, minute: int, **tool_input: object) -> dict:
    """A call started at 12:<minute> UTC, whose id is c<minute>."""
    record = call(command, when=f"2026-10-02T12:{minute:02d}:00Z", **tool_input)
    record["message"]["content"][1]["id"] = f"c{minute}"
    return record


def launched(minute: int, task: str, when: str = BEFORE) -> dict:
    """The result of call c<minute> that says it now runs in the background."""
    record = output(f"hunter2 says {task} runs", when=when, call_id=f"c{minute}")
    record["toolUseResult"] = {"stdout": "", "backgroundTaskId": task}
    return record


def notified(task: str, when: str, form: str = "user") -> dict:
    """A record that says a background task ended, in each form the logs hold."""
    text = (
        f"<task-notification>\n<task-id>{task}</task-id>\n"
        "<summary>ended, after <task-id>quoted</task-id></summary>\n"
        "</task-notification>"
    )
    if form == "user":
        return {"type": "user", "timestamp": when, "message": {"content": text}}
    if form == "queue-operation":
        return {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": when,
            "content": text,
        }
    return {
        "type": "attachment",
        "timestamp": when,
        "attachment": {"type": "queued_command", "prompt": text},
    }


def unfinished(config: Path, *args: str) -> list[str]:
    proc = grep(config, ".", "--unfinished-at", AT, *args)
    assert proc.returncode in (0, 1), proc.stderr
    return [line.split("  ", 4)[4] for line in lines(proc)]


def test_unfinished_at_lists_a_call_with_no_result_and_not_a_finished_one(
    config: Path,
) -> None:
    session_log(config, *DONE, started("sleep 900", 5))
    proc = grep(config, ".", "--unfinished-at", AT)
    assert lines(proc) == ["2026-10-02 21:05:00  11111111  main  /work/repo  sleep 900"]
    session_log(config, *DONE)
    assert grep(config, ".", "--unfinished-at", AT).returncode == 1


def test_a_result_logged_after_the_time_leaves_the_call_unfinished(
    config: Path,
) -> None:
    session_log(
        config,
        *DONE,
        started("make slow", 5),
        started("make quick", 6),
        output("", when="2026-10-02T12:09:59Z", call_id="c6"),
        output("", when=AFTER, call_id="c5"),
    )
    assert unfinished(config) == ["make slow"]


def test_a_result_with_no_time_counts_as_not_logged(config: Path) -> None:
    result = output("", call_id="c5")
    del result["timestamp"]
    session_log(config, *DONE, started("make slow", 5), result)
    assert unfinished(config) == ["make slow"]


def test_a_call_started_after_the_time_is_left_out(config: Path) -> None:
    session_log(config, started("sleep 900", 5), started("sleep 800", 15))
    assert unfinished(config) == ["sleep 900"]


def test_a_call_started_exactly_at_the_time_is_listed(config: Path) -> None:
    session_log(config, *DONE, started("sleep 900", 10))
    assert unfinished(config) == ["sleep 900"]


def test_a_result_logged_exactly_at_the_time_finishes_the_call(config: Path) -> None:
    session_log(
        config,
        started("sleep 900", 4),
        started("make quick", 5),
        output("", when=EXACTLY, call_id="c5"),
    )
    assert unfinished(config) == ["sleep 900"]


def test_a_notification_logged_exactly_at_the_time_ends_the_task(config: Path) -> None:
    session_log(
        config,
        started("sleep 900", 4),
        started("sleep 800", 5, run_in_background=True),
        launched(5, "bt1"),
        notified("bt1", EXACTLY),
    )
    assert unfinished(config) == ["sleep 900"]


def test_unfinished_at_a_date_means_the_start_of_that_day(config: Path) -> None:
    session_log(
        config,
        # 23:59 on October 1st in Tokyo, then 00:30 on the 2nd.
        call("sleep 900", when="2026-10-01T14:59:00Z"),
        call("sleep 800", when="2026-10-01T15:30:00Z"),
    )
    proc = grep(config, ".", "--unfinished-at", "2026-10-02")
    assert [line.split("  ", 4)[4] for line in lines(proc)] == ["sleep 900"]


def test_a_background_call_with_no_notification_is_marked(config: Path) -> None:
    session_log(
        config,
        *DONE,
        started("sleep 900", 5, run_in_background=True),
        launched(5, "bt1"),
        # Cut off before its result.
        started("sleep 700", 7, run_in_background=True),
        # Another task's end.
        notified("bt2", BEFORE),
    )
    assert unfinished(config) == ["[background] sleep 900", "[background] sleep 700"]


@pytest.mark.parametrize("form", ["user", "queue-operation", "attachment"])
def test_a_background_call_notified_before_the_time_is_left_out(
    config: Path, form: str
) -> None:
    session_log(
        config,
        started("sleep 900", 4, run_in_background=True),
        launched(4, "bt1"),
        started("sleep 800", 5, run_in_background=True),
        launched(5, "bt2"),
        notified("bt2", "2026-10-02T12:08:00Z", form),
        # A notification's text can quote another task's id, which ends nothing.
        started("sleep 700", 6, run_in_background=True),
        launched(6, "quoted"),
    )
    assert unfinished(config) == ["[background] sleep 900", "[background] sleep 700"]


def test_a_notification_logged_before_the_launch_result_counts(config: Path) -> None:
    session_log(
        config,
        started("sleep 900", 4),
        started("true", 5, run_in_background=True),
        notified("bt1", "2026-10-02T12:05:01Z", "queue-operation"),
        launched(5, "bt1", when="2026-10-02T12:05:02Z"),
    )
    assert unfinished(config) == ["sleep 900"]


def test_a_background_call_notified_after_the_time_is_listed(config: Path) -> None:
    session_log(
        config,
        *DONE,
        started("sleep 900", 5, run_in_background=True),
        launched(5, "bt1"),
        notified("bt1", AFTER),
    )
    assert unfinished(config) == ["[background] sleep 900"]


def test_a_call_sent_to_the_background_later_is_a_background_call(
    config: Path,
) -> None:
    # No run_in_background: the call outlasted its time limit and went on running.
    session_log(config, *DONE, started("make slow", 5), launched(5, "bt1"))
    assert unfinished(config) == ["[background] make slow"]


def test_a_background_launch_that_failed_is_finished(config: Path) -> None:
    session_log(
        config,
        started("sleep 900", 4),
        started("slepe 900", 5, run_in_background=True),
        output("refused", when=BEFORE, call_id="c5"),
    )
    assert unfinished(config) == ["sleep 900"]


def test_a_subagents_unfinished_call_is_listed_under_its_agent(config: Path) -> None:
    session_log(config, *DONE)
    subagent_log(
        config,
        "agent-abc",
        "Fix the bug",
        *DONE,
        started("make slow", 5),
        started("sleep 900", 6, run_in_background=True),
        launched(6, "bt1"),
    )
    proc = grep(config, ".", "--unfinished-at", AT)
    assert [line.split("  ", 4)[2::2] for line in lines(proc)] == [
        ["agent-abc (Fix the bug)", "make slow"],
        ["agent-abc (Fix the bug)", "[background] sleep 900"],
    ]


def test_since_still_bounds_the_start_of_an_unfinished_call(config: Path) -> None:
    session_log(
        config,
        call("sleep 950", when="2026-10-02T11:00:00Z"),
        *DONE,
        started("sleep 900", 5),
    )
    assert unfinished(config, "--since", "2026-10-02 20:30") == ["sleep 900"]
    assert unfinished(config) == ["sleep 950", "sleep 900"]


def test_unfinished_at_with_until_is_a_usage_error(config: Path) -> None:
    session_log(config, started("sleep 900", 5))
    proc = grep(config, ".", "--unfinished-at", AT, "--until", "2026-10-02 21:05")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "--until" in proc.stderr and "--unfinished-at" in proc.stderr


def test_a_bad_unfinished_at_time_is_a_usage_error(config: Path) -> None:
    session_log(config, started("sleep 900", 5))
    proc = grep(config, ".", "--unfinished-at", "crash-ish")
    assert proc.returncode == 2
    assert "--unfinished-at" in proc.stderr


def test_a_log_last_written_before_the_time_is_still_read(config: Path) -> None:
    # A session that died at the time wrote its log for the last time before it.
    log = session_log(config, *DONE, started("sleep 900", 5))
    written = datetime(2026, 10, 2, 12, 5, 30, tzinfo=UTC).timestamp()
    os.utime(log, (written, written))
    assert unfinished(config, "--since", "2026-10-02 21:00") == ["sleep 900"]


def test_unfinished_calls_are_masked_and_filtered_like_any_search(
    config: Path,
) -> None:
    session_log(
        config,
        *DONE,
        started("GH_TOKEN=s3cr3tvalue sleep 900", 5),
        started("make slow", 6),
        started("", 7, name="Read", file_path="/work/slow.txt"),
    )
    assert unfinished(config) == ["GH_TOKEN=*** sleep 900", "make slow"]
    proc = grep(config, "sl[eo]", "--unfinished-at", AT, "--tool", "*", "--max", "2")
    assert [line.split("  ", 4)[4] for line in lines(proc)] == [
        "make slow",
        "Read: file_path=/work/slow.txt",
    ]


def test_a_secret_in_a_background_calls_command_is_masked(config: Path) -> None:
    session_log(
        config,
        *DONE,
        started("GH_TOKEN=s3cr3tvalue sleep 900", 5, run_in_background=True),
        launched(5, "bt1"),
    )
    assert unfinished(config) == ["[background] GH_TOKEN=*** sleep 900"]


def test_unfinished_at_never_searches_or_prints_a_result(config: Path) -> None:
    session_log(
        config,
        started("make slow", 5),
        output("hunter2 printed late", when=AFTER, call_id="c5"),
        started("sleep 900", 6, run_in_background=True),
        launched(6, "bt1"),
    )
    proc = grep(config, ".", "--unfinished-at", AT, "--full")
    head = f"  {PROJECT}  {SESSION}  main  /work/repo"
    assert lines(proc) == [
        f"2026-10-02 21:05:00{head}",
        "    make slow",
        f"2026-10-02 21:06:00{head}",
        "    [background] sleep 900",
    ]
    assert grep(config, "hunter2", "--unfinished-at", AT).returncode == 1


def test_unfinished_at_treats_a_session_grep_call_like_any_other(
    config: Path,
) -> None:
    session_log(config, *DONE, started("session-grep needle", 5))
    assert unfinished(config) == ["session-grep needle"]
