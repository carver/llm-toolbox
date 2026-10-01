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
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

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
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, **fields})


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
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_RECORD": str(record),
               "FAKE_STDOUT": fake_stdout, "ANTHROPIC_API_KEY": "sk-ant-api-fake",
               **{f"FAKE_{k.upper()}": str(v) for k, v in fake_env.items()}}
        proc = subprocess.run([str(SCRIPT), "--schema", json.dumps(SCHEMA), *args], input=prompt,
                              text=True, capture_output=True, env=env, timeout=30, check=False)
        sent = json.loads(record.read_text()) if record.exists() else None
        return proc, sent
    return run


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_prints_the_structured_output(run):
    proc, _ = run(fake_stdout=reply(structured_output={"name": "Ada"}, result='{"name":"Ada"}'))
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
    _, sent = run("--model", "haiku", "--system-prompt", "Be terse.", "--max-budget-usd", "3")
    assert flag(sent["argv"], "--model") == "haiku"
    assert flag(sent["argv"], "--system-prompt") == "Be terse."
    assert flag(sent["argv"], "--max-budget-usd") == "3"


@pytest.mark.parametrize("fake", [
    {"exit": 1, "stderr": "Invalid API key"},
    {"fake_stdout": "not json"},
    {"fake_stdout": reply(is_error=True, subtype="error_max_budget_usd")},
    {"fake_stdout": reply(result="plain text, no structured output")},
    {"fake_stdout": reply(structured_output="a string, not an object")},
])
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
    proc = subprocess.run([str(SCRIPT), "--schema", "{not json"], input="p", text=True,
                          capture_output=True, timeout=30, check=False)
    assert proc.returncode == 2
