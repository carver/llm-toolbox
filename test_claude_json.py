"""claude-json runs one headless `claude -p` call and prints its structured output.

A fake `claude` on PATH records the argv, environment, working directory and
stdin it was given, then replies the way the real CLI does.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "claude-json"
SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
record = {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "stdin": sys.stdin.read()}
with open(os.environ["FAKE_RECORD"], "w") as f:
    json.dump(record, f)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def reply(**fields) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, **fields}
    )


OK_REPLY = reply(structured_output={"name": "x"})


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    record = tmp_path / "record.json"

    def run(*args, prompt="the prompt", fake_stdout=OK_REPLY, **fake_env):
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_RECORD": str(record),
            "FAKE_STDOUT": fake_stdout,
            "ANTHROPIC_API_KEY": "sk-ant-api-fake",
            **{f"FAKE_{k.upper()}": str(v) for k, v in fake_env.items()},
        }
        proc = subprocess.run(
            [str(SCRIPT), "--schema", json.dumps(SCHEMA), *args],
            input=prompt,
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        sent = json.loads(record.read_text()) if record.exists() else None
        return proc, sent

    return run


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_prints_the_structured_output(run):
    proc, _ = run(
        fake_stdout=reply(structured_output={"name": "Ada"}, result='{"name":"Ada"}')
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"name": "Ada"}


def test_prompt_goes_on_stdin_and_schema_as_a_flag(run):
    _, sent = run(prompt="classify this")
    assert sent["stdin"] == "classify this"
    assert json.loads(flag(sent["argv"], "--json-schema")) == SCHEMA
    assert flag(sent["argv"], "--output-format") == "json"


def test_schema_can_come_from_a_file(run, tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    proc, sent = run("--schema", str(path))
    assert proc.returncode == 0
    assert json.loads(flag(sent["argv"], "--json-schema")) == SCHEMA


def test_never_bills_an_api_key(run):
    _, sent = run()
    assert "ANTHROPIC_API_KEY" not in sent["env"]


def test_defaults_to_no_tools_settings_mcp_or_session_file(run):
    _, sent = run()
    argv = sent["argv"]
    assert flag(argv, "--tools") == ""
    assert "--allowedTools" not in argv
    assert flag(argv, "--setting-sources") == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--system-prompt" not in argv


def test_runs_in_an_empty_directory_so_no_claude_md_loads(run):
    _, sent = run()
    assert not os.path.exists(sent["cwd"]) or os.listdir(sent["cwd"]) == []
    assert sent["cwd"] != os.getcwd()


def test_named_tools_are_enabled_and_preapproved(run):
    _, sent = run("--tools", "WebFetch,WebSearch")
    assert flag(sent["argv"], "--tools") == "WebFetch,WebSearch"
    assert flag(sent["argv"], "--allowedTools") == "WebFetch,WebSearch"


def test_passes_model_system_prompt_and_budget(run):
    _, sent = run(
        "--model", "haiku", "--system-prompt", "Be terse.", "--max-budget-usd", "3"
    )
    assert flag(sent["argv"], "--model") == "haiku"
    assert flag(sent["argv"], "--system-prompt") == "Be terse."
    assert flag(sent["argv"], "--max-budget-usd") == "3"


@pytest.mark.parametrize(
    "fake",
    [
        {"exit": 1, "stderr": "Invalid API key"},
        {"fake_stdout": "not json"},
        {"fake_stdout": reply(is_error=True, subtype="error_max_budget_usd")},
        {"fake_stdout": reply(result="plain text, no structured output")},
        {"fake_stdout": reply(structured_output="a string, not an object")},
    ],
)
def test_a_failed_call_exits_1_with_the_reason_on_stderr(run, fake):
    proc, _ = run(**fake)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.strip()


def test_times_out_with_exit_1(run):
    proc, _ = run("--timeout", "0.5", sleep=10)
    assert proc.returncode == 1
    assert "timed out" in proc.stderr


def test_an_empty_prompt_is_a_usage_error(run):
    proc, sent = run(prompt="  \n")
    assert proc.returncode == 2
    assert sent is None


def test_a_bad_schema_is_a_usage_error(run):
    proc = subprocess.run(
        [str(SCRIPT), "--schema", "{not json"],
        input="p",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2


# claude-fresh is the same tool under its new name, with a --text mode beside --json.
# claude-json is a link to it and keeps the JSON mode it always had.

FRESH = Path(__file__).resolve().parent / "bin" / "claude-fresh"
REFUSAL = "API Error: Sonnet 5.5's safeguards flagged this message. Details: `[reasoning_extraction]`"


@pytest.fixture
def fresh(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    record = tmp_path / "record.json"

    def fresh(*args, prompt="the prompt", fake_stdout=OK_REPLY, **fake_env):
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_RECORD": str(record),
            "FAKE_STDOUT": fake_stdout,
            **{f"FAKE_{k.upper()}": str(v) for k, v in fake_env.items()},
        }
        proc = subprocess.run(
            [str(FRESH), *args],
            input=prompt,
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        sent = json.loads(record.read_text()) if record.exists() else None
        return proc, sent

    return fresh


def test_claude_json_is_a_link_to_claude_fresh():
    assert SCRIPT.is_symlink()
    assert SCRIPT.resolve() == FRESH.resolve()


def test_text_mode_prints_the_reply_text_and_sends_no_schema(fresh):
    proc, sent = fresh("--text", fake_stdout=reply(result="Hello there."))
    assert (proc.returncode, proc.stdout) == (0, "Hello there.\n"), proc.stderr
    assert "--json-schema" not in sent["argv"]
    assert flag(sent["argv"], "--tools") == ""
    assert "--strict-mcp-config" in sent["argv"]
    assert "--no-session-persistence" in sent["argv"]


def test_json_mode_prints_the_structured_output(fresh):
    proc, sent = fresh("--json", json.dumps(SCHEMA))
    assert (proc.returncode, json.loads(proc.stdout)) == (0, {"name": "x"}), proc.stderr
    assert json.loads(flag(sent["argv"], "--json-schema")) == SCHEMA


@pytest.mark.parametrize("args", [[], ["--text", "--json", json.dumps(SCHEMA)]])
def test_exactly_one_mode_is_required(fresh, args):
    proc, sent = fresh(*args)
    assert proc.returncode == 2
    assert sent is None


def test_claude_json_still_requires_a_schema(run):
    proc = subprocess.run(
        [str(SCRIPT)],
        input="p",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2


@pytest.mark.parametrize("mode", [["--text"], ["--json", json.dumps(SCHEMA)]])
def test_a_refusal_names_its_reason(fresh, mode):
    # claude -p exits 1 with an empty stderr; the reason is only in stdout's JSON.
    refused = reply(is_error=True, result=REFUSAL)
    proc, _ = fresh(*mode, fake_stdout=refused, exit=1)
    assert proc.returncode == 1
    assert "reasoning_extraction" in proc.stderr


def test_an_error_reply_names_its_result_not_just_its_subtype(fresh):
    proc, _ = fresh("--text", fake_stdout=reply(is_error=True, result=REFUSAL))
    assert proc.returncode == 1
    assert "reasoning_extraction" in proc.stderr


def test_text_mode_with_no_text_fails(fresh):
    proc, _ = fresh("--text", fake_stdout=reply(result=""))
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_claude_json_keeps_its_old_interface(fresh):
    proc = subprocess.run(
        [str(SCRIPT), "--text"],
        input="p",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "--text" not in proc.stderr.splitlines()[0]  # its usage line names only -s


@pytest.mark.parametrize("schema", ["null", "5", "[]", '"a string"'])
def test_a_schema_must_be_a_json_object(fresh, schema):
    proc, sent = fresh("--json", schema)
    assert proc.returncode == 2
    assert sent is None


def test_a_json_reply_of_the_wrong_shape_is_not_called_non_json(fresh):
    proc, _ = fresh("--text", fake_stdout="[1, 2]")
    assert proc.returncode == 1
    assert "non-JSON" not in proc.stderr
