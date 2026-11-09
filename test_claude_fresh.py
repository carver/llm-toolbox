"""claude-fresh runs one locked-down headless `claude -p` call and prints its answer,
as JSON (--json SCHEMA) or text (--text). claude-json is a link to it that keeps the
old `-s SCHEMA` interface.

A fake `claude` on PATH records the argv, environment, working directory and
stdin it was given, makes the project's memory folder as Claude Code does at
startup (whatever CLAUDE_CODE_DISABLE_AUTO_MEMORY says, as a Claude Code that
ignores it would, so the cleanup claude-fresh falls back on gets tested), then
replies the way the real CLI does. Every test runs with it, and
with HOME and TMPDIR under its tmp_path, so none can reach the real CLI or the
real ~/.claude.
"""

import inspect
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from conftest import wait_until

SCRIPT = Path(__file__).resolve().parent / "bin" / "claude-json"
FRESH = Path(__file__).resolve().parent / "bin" / "claude-fresh"
SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


def claude_code_project_name(path: str) -> str:
    """The folder Claude Code 2.1 keeps under projects/ for a project at `path`,
    transcribed from its source (`VL`): each character but an ASCII letter or digit
    made "-"; past 200 characters, the first 200, "-" and
    `Math.abs(hash).toString(36)`, the hash being `e=(e<<5)-e+charCode|0` over the
    path. JavaScript counts UTF-16 code units, which this matches for paths without
    characters beyond the BMP."""
    name = "".join(c if c.isascii() and c.isalnum() else "-" for c in path)
    if len(name) <= 200:
        return name
    e = 0
    for c in path:
        e = (e << 5) - e + ord(c)
        e = (e + 2**31) % 2**32 - 2**31  # `|0`: wrap to a signed 32-bit int
    e = abs(e)
    digits = ""
    while True:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[e % 36] + digits
        e //= 36
        if e == 0:
            return f"{name[:200]}-{digits}"


FAKE_CLAUDE = (
    """#!/usr/bin/env python3
import json, os, signal, sys, time
"""
    + inspect.getsource(claude_code_project_name)
    + """
if os.environ.get("FAKE_IGNORE_SIGINT"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
# What Claude Code does at startup: the project's memory folder, named for the cwd.
config = os.environ["CLAUDE_CONFIG_DIR"] if "CLAUDE_CONFIG_DIR" in os.environ else os.path.join(os.environ["HOME"], ".claude")
project = os.path.join(config, "projects", claude_code_project_name(os.getcwd()))
memory = os.path.join(project, "memory")
if os.environ.get("FAKE_PROJECT_LINK"):
    os.makedirs(os.path.dirname(project), exist_ok=True)
    os.symlink(os.environ["FAKE_PROJECT_LINK"], project)
os.makedirs(project, exist_ok=True)
if os.environ.get("FAKE_MEMORY_LINK"):
    os.symlink(os.environ["FAKE_MEMORY_LINK"], memory)
else:
    os.mkdir(memory)
if os.environ.get("FAKE_MEMORY_FILE"):
    with open(os.path.join(memory, os.environ["FAKE_MEMORY_FILE"]), "w") as f:
        f.write("a memory")
if os.environ.get("FAKE_REPOINT_TMPDIR"):
    os.symlink(os.environ["FAKE_REPOINT_TMPDIR"], os.environ["TMPDIR"] + ".new")
    os.replace(os.environ["TMPDIR"] + ".new", os.environ["TMPDIR"])
if os.environ.get("FAKE_TRANSCRIPT"):
    with open(os.path.join(project, "transcript.jsonl"), "w") as f:
        f.write("{}")
record = {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "stdin": sys.stdin.read(), "project": project}
with open(os.environ["FAKE_RECORD"] + ".part", "w") as f:
    json.dump(record, f)
os.replace(os.environ["FAKE_RECORD"] + ".part", os.environ["FAKE_RECORD"])
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""
)


def reply(**fields) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, **fields}
    )


OK_REPLY = reply(structured_output={"name": "x"})
REFUSAL = "API Error: Sonnet 5.5's safeguards flagged this message. Details: `[reasoning_extraction]`"


def make_runner(tmp_path, script, *preset):
    """Runs `script` with `preset` and then the call's own arguments, against the fake
    claude, with HOME and TMPDIR under tmp_path and no CLAUDE_CONFIG_DIR. `env` adds
    to or replaces the environment; `run.start` starts the same call without waiting
    for it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    record = tmp_path / "record.json"
    (tmp_path / "tmp").mkdir()

    def environment(fake_stdout, fake_env, env):
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        real_home = Path.home()
        return {
            **base,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            # uv, which runs claude-fresh, keeps its cache and Pythons under HOME:
            # keep the real ones, so the scratch HOME costs no build or download.
            "XDG_CACHE_HOME": base.get("XDG_CACHE_HOME", str(real_home / ".cache")),
            "XDG_DATA_HOME": base.get(
                "XDG_DATA_HOME", str(real_home / ".local" / "share")
            ),
            "TMPDIR": str(tmp_path / "tmp"),
            "FAKE_RECORD": str(record),
            "FAKE_STDOUT": fake_stdout,
            "ANTHROPIC_API_KEY": "sk-ant-api-fake",
            **{f"FAKE_{k.upper()}": str(v) for k, v in fake_env.items()},
            **(env or {}),
        }

    def read_record():
        return json.loads(record.read_text()) if record.exists() else None

    def run(*args, prompt="the prompt", fake_stdout=OK_REPLY, env=None, **fake_env):
        proc = subprocess.run(
            [str(script), *preset, *args],
            input=prompt,
            text=True,
            capture_output=True,
            env=environment(fake_stdout, fake_env, env),
            timeout=30,
            check=False,
        )
        return proc, read_record()

    def start(*args, fake_stdout=OK_REPLY, env=None, **fake_env):
        """The call, in a process group of its own, and a function that returns the
        fake's record once the fake has written it."""
        proc = subprocess.Popen(
            [str(script), *preset, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment(fake_stdout, fake_env, env),
            start_new_session=True,
        )
        proc.stdin.write("the prompt")
        proc.stdin.close()
        return proc, read_record

    run.start = start
    return run


@pytest.fixture
def run(tmp_path):
    """claude-json with a schema, the way its existing callers use it."""
    return make_runner(tmp_path, SCRIPT, "--schema", json.dumps(SCHEMA))


@pytest.fixture
def bare_json(tmp_path):
    """claude-json with only the arguments a test gives."""
    return make_runner(tmp_path, SCRIPT)


@pytest.fixture
def fresh(tmp_path):
    return make_runner(tmp_path, FRESH)


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


def test_a_bad_schema_is_a_usage_error(bare_json):
    proc, sent = bare_json("--schema", "{not json")
    assert proc.returncode == 2
    assert sent is None


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


def test_claude_json_still_requires_a_schema(bare_json):
    proc, sent = bare_json()
    assert proc.returncode == 2
    assert sent is None


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


def test_claude_json_keeps_its_old_interface(bare_json):
    proc, sent = bare_json("--text")
    assert proc.returncode == 2
    assert sent is None
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


# Claude Code makes <config>/projects/<name>/memory for each call's own temporary
# directory, and cron calls left thousands of them, empty.


def test_the_fakes_folder_names_are_claude_codes():
    # Folders Claude Code 2.1.321 made, run offline in these folders.
    assert claude_code_project_name("/tmp/claude-fresh-_1_xgpm1") == (
        "-tmp-claude-fresh--1-xgpm1"
    )
    long = "/tmp/claude-fresh-truth/a_b.c"
    first_200 = "-tmp-claude-fresh-truth-a-b-c" + "x" * 171
    assert claude_code_project_name(long + "x" * 200) == f"{first_200}-i1fgk2"
    assert claude_code_project_name(long + "x" * 201) == f"{first_200}-90l6mi"


@pytest.mark.parametrize(
    "args, fake",
    [
        ([], {"fake_stdout": reply(result="Hello.")}),
        ([], {"exit": 1, "stderr": "Invalid API key"}),
        (["--timeout", "2"], {"sleep": 30}),
    ],
    ids=["success", "failed call", "timeout"],
)
def test_removes_the_calls_empty_memory_and_project_folders(fresh, args, fake):
    proc, sent = fresh("--text", *args, **fake)
    if sent is None and "--timeout" in args:
        pytest.skip("the call timed out before the fake started, on a busy machine")
    assert proc.returncode == (0 if "fake_stdout" in fake else 1), proc.stderr
    project = Path(sent["project"])
    assert not project.exists(), proc.stderr
    assert project.parent.is_dir()


def test_turns_off_auto_memory_for_the_child(fresh):
    _, sent = fresh("--text", env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"})
    assert sent["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_keeps_a_project_folder_that_holds_a_transcript(fresh):
    _, sent = fresh("--text", transcript=1)
    project = Path(sent["project"])
    assert not (project / "memory").exists()
    assert (project / "transcript.jsonl").is_file()


def test_a_ctrl_c_mid_call_removes_them_too(fresh):
    # The fake ignores the SIGINT, so claude-fresh is interrupted while it waits.
    proc, read_record = fresh.start("--text", sleep=60, ignore_sigint=1)
    try:
        wait_until(read_record, "the fake made the memory folder")
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=30)
        project = Path(read_record()["project"])
        wait_until(lambda: not project.exists(), "the project folder went")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def test_keeps_a_memory_folder_with_a_file_in_it(fresh):
    _, sent = fresh("--text", memory_file="note.md")
    assert (Path(sent["project"]) / "memory" / "note.md").is_file()


def test_keeps_a_memory_link(fresh, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    _, sent = fresh("--text", memory_link=target)
    assert (Path(sent["project"]) / "memory").is_symlink()
    assert target.is_dir()


def test_never_goes_through_a_project_folder_that_is_a_link(fresh, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    _, sent = fresh("--text", project_link=target)
    assert Path(sent["project"]).is_symlink()
    assert (target / "memory").is_dir()


def test_follows_claude_config_dir(fresh, tmp_path):
    config = tmp_path / "config"
    _, sent = fresh("--text", env={"CLAUDE_CONFIG_DIR": str(config)})
    project = Path(sent["project"])
    assert project.parent == config / "projects"
    assert project.parent.is_dir()
    assert not project.exists()


def test_names_the_folder_for_the_real_path_of_a_linked_tmpdir(fresh, tmp_path):
    real = tmp_path / "real-tmp"
    real.mkdir()
    (tmp_path / "linked-tmp").symlink_to(real)
    _, sent = fresh("--text", env={"TMPDIR": str(tmp_path / "linked-tmp")})
    assert sent["cwd"].startswith(os.path.realpath(real))
    assert not Path(sent["project"]).exists()


def test_names_the_folder_for_where_the_call_started(fresh, tmp_path):
    # TMPDIR's link moves mid-call: the folder is still the one Claude Code named.
    first, later = tmp_path / "first", tmp_path / "later"
    first.mkdir()
    later.mkdir()
    (tmp_path / "linked-tmp").symlink_to(first)
    _, sent = fresh(
        "--text", env={"TMPDIR": str(tmp_path / "linked-tmp")}, repoint_tmpdir=later
    )
    assert not Path(sent["project"]).exists()


def test_removes_it_under_a_shortened_name(fresh, tmp_path):
    deep = tmp_path / ("d" * 120) / ("e" * 120)
    deep.mkdir(parents=True)
    # Each call's directory has a random name, whose hash comes out negative about
    # half the time, and the name holds its absolute value: eight calls see both.
    for _ in range(8):
        _, sent = fresh("--text", env={"TMPDIR": str(deep)})
        project = Path(sent["project"])
        assert len(project.name) > 200
        assert not project.exists()


def test_leaves_it_when_the_path_is_not_ascii(fresh, tmp_path):
    accented = tmp_path / "café"
    accented.mkdir()
    _, sent = fresh("--text", env={"TMPDIR": str(accented)})
    assert (Path(sent["project"]) / "memory").is_dir()
