"""drive-agent runs one scripted session of a coding agent's own TUI (Claude Code or
Codex) in tmux, typing a scenario's human turns once each agent turn ends, with a
simulated human for questions the script leaves open.

The tests run it as an agent would, as a subprocess, with fake `claude`, `codex` and
`claude-fresh` first on PATH and DRIVE_AGENT_ISOLATION=none, so no system user,
bwrap or iptables rule is made and no real harness or model is called. The fakes are
scripted TUIs: each prints a ready marker, reads the lines tmux types, answers from
the rules below, writes a transcript in its harness's format and fires the harness's
turn-end hook (Claude Code's Stop hook from settings.json, Codex's notify program).
What a fake saw (its user, environment, PATH) it writes to a file in its home, which
the run's `files/` collects. One test runs the real isolation and skips unless
passwordless sudo and bwrap are there. The proxy's tests with no upstream proxy load
the tool as a module instead (see `load_drive_agent`).
"""

import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent / "bin" / "drive-agent"

# A fake agent's reply to a typed line: "ask" makes it ask to post, "fetch HOST" makes
# it CONNECT to HOST through $HTTPS_PROXY and report the status line, anything else
# gets "done: <line>". Every turn writes ~/seen.json: user, env, cwd.
# The fake Claude Code also asks in a question dialog, as AskUserQuestion does:
# "dialog" asks one question, "dialog2" two in one dialog (tabs), "dialogm" one
# multi-select question (a tab line and checkboxes, as Claude Code 2.1.332 draws it),
# "deaf" one it never takes an answer to, "nochat" two with no "Chat about this";
# "late", "late2" and "latem" are "dialog", "dialog2" and "dialogm" whose tool_use
# reaches the transcript only with its answer, as Claude Code 2.1.325 sometimes
# writes it. The dialog writes its tool_use to the transcript and fires no
# Stop hook, and reads keys as they come, as Claude Code does. Key 3, its free-text
# option, takes a typed line: for a single question it writes the tool_result, works
# a while with the answered dialog still drawn, replies "done: answer <text>" and
# fires Stop; with several it moves on to the next tab. Key 4, "Chat about this",
# rejects the call, as Claude Code does, asks what to clarify and fires Stop.
# The fake Codex asks through request_user_input_async, as Codex 0.164 does, writing
# the call and its {"accepted":true} output, printing the question and saying it
# works on meanwhile: "async" then takes a line typed within FAKE_ASYNC_WAIT seconds
# (default 10, given with --agent-env) as the answer, which it writes as a user
# message in the same turn ("done: answer <text>"), else goes on without one;
# "asyncend" ends its turn after asking; "asyncdie" notifies on the answer but exits
# before its task_complete. Every Codex turn ends in a task_complete record before
# its notify.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, select, socket, subprocess, sys, termios, time, tty, uuid
HARNESS = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (fake " + HARNESS + ")")
    sys.exit()

def reply(line):
    if line == "ask":
        return "Shall I post the result to File Drawer?"
    if line.startswith("direct "):
        host, port = line.split()[1:]
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
            return "direct: connected"
        except OSError as error:
            return "direct: " + type(error).__name__
    if line == "selfserve":
        # A server of the agent's own on loopback, fetched as a tool would, with
        # the proxy variables honoured (Codex's built-in MCP server).
        import http.server, threading, urllib.request
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/" % server.server_address[1]
            return "selfserve: %d" % urllib.request.urlopen(url, timeout=5).status
        except Exception as error:
            return "selfserve: " + str(getattr(error, "code", type(error).__name__))
    if line.startswith("fetch "):
        host = line.split()[1]
        proxy = os.environ.get("HTTPS_PROXY", "")
        hp = proxy.split("//")[-1].rstrip("/")
        h, p = hp.rsplit(":", 1)
        s = socket.create_connection((h, int(p)), timeout=5)
        s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        return "fetched: " + s.recv(200).decode().splitlines()[0]
    return "done: " + line

def seen():
    with open(os.path.join(os.environ["HOME"], "seen.json"), "w") as f:
        cmdlines = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                cmdlines.append(open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace"))
            except OSError:
                pass
        json.dump({"user": os.environ.get("USER"), "env": dict(os.environ),
                   "cwd": os.getcwd(), "argv": sys.argv, "homes": sorted(os.listdir("/home")),
                   "cmdlines": cmdlines, "uid": os.getuid(),
                   "mounts": open("/proc/self/mountinfo").read(),
                   "hidden": open(os.environ["FAKE_HIDDEN"]).read() if "FAKE_HIDDEN" in os.environ else None}, f)

QUESTIONS = {
    "colour": {"question": "Which colour do you prefer?", "header": "Colour",
               "multiSelect": False,
               "options": [{"label": "Red", "description": "warm"},
                           {"label": "Blue", "description": "cool"}]},
    "animal": {"question": "Which animal do you prefer?", "header": "Animal",
               "multiSelect": False,
               "options": [{"label": "Cat", "description": ""},
                           {"label": "Dog", "description": ""}]},
}
DIALOGS = {"dialog": ["colour"], "dialog2": ["colour", "animal"], "deaf": ["colour"],
           "dialogm": ["colour"], "nochat": ["colour", "animal"],
           "late": ["colour"], "late2": ["colour", "animal"], "latem": ["colour"]}

def draw(questions, chat=True, boxed=False):
    # boxed: the question in a box's left edge, with each option described, as
    # Claude Code 2.1.325 draws it.
    first = questions[0]
    print("─" * 40)
    if first["multiSelect"]:
        print("←  ☐ " + first["header"] + "  ✔ Submit  →")
        print("\n" + first["question"] + "\n")
        for n, option in enumerate(first["options"], 1):
            print(("❯ " if n == 1 else "  ") + f"{n}. [ ] {option['label']}")
        print("  3. [ ] Type something\n     Submit")
        print("─" * 40)
        print("  4. Chat about this")
        print("Enter to select · ↑/↓ to navigate · Esc to cancel", flush=True)
        return
    if len(questions) > 1:
        print("←  " + "  ".join("☐ " + q["header"] for q in questions) + "  ✔ Submit  →")
    else:
        print(" ☐ " + first["header"])
    print(("│ " if boxed else "") + first["question"])
    for n, option in enumerate(first["options"], 1):
        print(("❯ " if n == 1 else "  ") + f"{n}. {option['label']}")
        if boxed:
            print("     Picks " + option["label"].lower() + ".")
    print("  3. Type something.")
    print("─" * 40)
    if chat:
        print("  4. Chat about this")
    nav = "Tab/Arrow keys" if len(questions) > 1 else "↑/↓"
    print(f"Enter to select · {nav} to navigate · Esc to cancel", flush=True)

session = str(uuid.uuid4())
if HARNESS == "claude":
    proj = os.path.join(os.environ["HOME"], ".claude", "projects", "p")
    os.makedirs(proj, exist_ok=True)
    transcript = os.path.join(proj, session + ".jsonl")
    def record(line, text):
        with open(transcript, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": line}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")
    def write(*records):
        with open(transcript, "a") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
    def ask(line):
        questions = [dict(QUESTIONS[k], multiSelect=line in ("dialogm", "latem"))
                     for k in DIALOGS[line]]
        tool_id = "toolu_" + uuid.uuid4().hex
        asked = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
             "input": {"questions": questions}}]}}
        write({"type": "user", "message": {"content": line}})
        if line.startswith("late"):
            unwritten.append(asked)
        else:
            write(asked)
        # A subagent's transcript, written after the question, is the newest file.
        sub = os.path.join(proj, session, "subagents")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "agent-1.jsonl"), "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "subagent at work"}]}}) + "\n")
        draw(questions, chat=line != "nochat", boxed=line.startswith("late"))
        return tool_id, questions
    unwritten = []
    def answer(tool_id, questions, text):
        write(*unwritten)
        unwritten.clear()
        write({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": f'User has answered your questions: "{questions[0]["question"]}"="{text}".'}]}})
    def key():
        return os.read(0, 1).decode(errors="replace")
    def dialog(line):
        # Keys as they come from the moment the dialog shows, keeping any typed early.
        saved = termios.tcgetattr(0)
        tty.setcbreak(0, termios.TCSANOW)
        tool_id, questions = ask(line)
        try:
            while True:
                pressed = key()
                if not pressed:
                    sys.exit()
                if line == "deaf":
                    continue
                if pressed == "3":
                    given = ""
                    while (c := key()) not in ("\r", "\n", ""):
                        given += c
                    if len(questions) > 1:
                        draw(questions[1:])
                        continue
                    answer(tool_id, questions, given)
                    # fixed sleep: the agent works on while the answered dialog is still drawn
                    time.sleep(1.5)
                    text = "done: answer " + given
                elif pressed == "4" and line != "nochat":
                    write(*unwritten)
                    unwritten.clear()
                    write({"type": "user", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                         "content": "The user doesn't want to proceed with this tool use. "
                                    "The user wants to clarify these questions."}]}})
                    print("User declined to answer questions")
                    text = "What would you like to clarify?"
                else:
                    continue
                write({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": text}]}})
                print(text)
                turn_end(line, text)
                print("❯ ", end="", flush=True)
                return
        finally:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
    def turn_end(line, text):
        settings = json.load(open(os.path.join(os.environ["HOME"], ".claude", "settings.json")))
        for hook in settings["hooks"]["Stop"][0]["hooks"]:
            subprocess.run(hook["command"], shell=True)
else:
    sessions = os.path.join(os.environ["CODEX_HOME"], "sessions")
    os.makedirs(sessions, exist_ok=True)
    transcript = os.path.join(sessions, "rollout-" + session + ".jsonl")
    notify = [json.loads(a.split("=", 1)[1]) for a in args if a.startswith("notify=")]
    def message(role, t):
        write({"type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text",
                         "text": t}]}})
    def write(*records):
        with open(transcript, "a") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
    def record(line, text):
        message("user", line)
        message("assistant", text)
    def ask_async(line):
        message("user", line)
        message("assistant", "Before I go on, one question.")
        call_id = "call_" + uuid.uuid4().hex
        write({"type": "response_item", "payload": {
                   "type": "function_call", "name": "request_user_input_async",
                   "call_id": call_id, "arguments": json.dumps({"questions": [
                       {"title": "Which colour do you prefer?",
                        "options": ["Red", "Blue"]}]})}},
              {"type": "response_item", "payload": {
                   "type": "function_call_output", "call_id": call_id,
                   "output": '{"accepted":true}'}})
        print("Which colour do you prefer?\n- Red\n- Blue", flush=True)
        message("assistant", "I'll work on meanwhile.")
        if line == "asyncend":
            text = "Tell me when you've chosen."
        elif line == "asyncdie":
            given = sys.stdin.readline().strip()
            message("user", given)
            subprocess.run([*notify[0], json.dumps({"input-messages": [given],
                                                  "last-assistant-message": "x"})])
            sys.exit(1)
        elif select.select([sys.stdin], [], [], float(os.environ.get("FAKE_ASYNC_WAIT", 10)))[0]:
            given = sys.stdin.readline().strip()
            message("user", given)
            text = "done: answer " + given
        else:
            text = "went on without an answer"
        message("assistant", text)
        print(text)
        turn_end(line, text)
        print("❯ ", end="", flush=True)
    def turn_end(line, text):
        write({"type": "event_msg", "payload": {"type": "task_complete",
                                                "last_agent_message": text}})
        # Codex also notifies for the side thread that names the session.
        title = {"input-messages": ["Generate a concise, single-line task title"],
                 "last-assistant-message": "Title"}
        subprocess.run([*notify[0], json.dumps(title)])
        subprocess.run([*notify[0], json.dumps({"input-messages": [line],
                                              "last-assistant-message": text})])

if "-p" in args or "exec" in args:
    line = args[-1]
    text = reply(line)
    seen()
    record(line, text)
    print(json.dumps({"type": "result", "result": text}))
    sys.exit()

if os.environ.get("FAKE_NEVER_READY") or os.path.exists(os.path.join(os.environ["HOME"], "never-ready")):
    print("Something unexpected")
    time.sleep(60)
print("? for shortcuts")
print("❯ ", end="", flush=True)
if os.path.exists(os.path.join(os.environ["HOME"], "trust-late")):
    # As Codex in a git project: the prompt shows, then the folder-trust dialog a
    # moment later, which drops typed text until Enter alone answers it.
    time.sleep(1)  # fixed sleep: the dialog must come after the prompt
    print("\nTrust this folder? Codex can read, edit, and run files here.")
    print("› 1. Trust and continue\n  2. Quit\n  enter continue · esc quit", flush=True)
    for line in sys.stdin:
        if not line.strip():
            break
        print("dropped: " + line.strip(), flush=True)
    print("? for shortcuts")
    print("❯ ", end="", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    if HARNESS == "claude" and line in DIALOGS:
        dialog(line)
        continue
    if HARNESS == "codex" and line in ("async", "asyncend", "asyncdie"):
        ask_async(line)
        continue
    if HARNESS == "claude" and line == "permission":
        # A permission prompt: the question dialog's footer, and no question.
        print("─" * 40)
        print("Do you want to proceed?")
        print("❯ 1. Yes")
        print("  2. No")
        print("Enter to select · ↑/↓ to navigate · Esc to cancel", flush=True)
        while os.read(0, 1):
            pass
        sys.exit()
    text = reply(line)
    seen()
    record(line, text)
    print(text)
    turn_end(line, text)
    print("❯ ", end="", flush=True)
"""

# The simulated human: echoes the scripted message, or, when the prompt holds a
# condition, says it holds only when the agent's last message asks to post. Every
# prompt is appended to $FAKE_HUMAN_LOG. FAKE_HUMAN=fail makes it fail.
FAKE_HUMAN = r"""#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["FAKE_HUMAN_LOG"], "a") as f:
    f.write(json.dumps(prompt) + "\n")
if os.environ.get("FAKE_HUMAN") == "fail":
    print("overloaded", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAKE_HUMAN") == "empty":
    print(json.dumps({"condition_met": True, "message": ""}))
    sys.exit()
scripted = prompt.rsplit("The scripted next message:\n<<<\n", 1)[1].split("\n>>>", 1)[0]
last = prompt.rsplit("The agent's last message:\n<<<\n", 1)[1].split("\n>>>", 1)[0]
if "judge this condition" in prompt:
    met = "Shall I post" in last
    print(json.dumps({"condition_met": met, "message": scripted if met else ""}))
else:
    print(json.dumps({"condition_met": True, "message": scripted}))
"""


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    yield e
    remove_tmux_folder(e["TMUX_TMPDIR"])


def remove_tmux_folder(folder: str) -> None:
    """Ends each tmux server left in `folder`, as a drive-agent killed before its
    cleanup leaves one, then removes it: with its socket gone it could be reached
    no more."""
    for sock in Path(folder).glob("tmux-*/*"):
        subprocess.run(
            ["tmux", "-S", str(sock), "kill-server"],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            capture_output=True,
            timeout=10,
            check=False,
        )
    shutil.rmtree(folder, ignore_errors=True)


def make_env(tmp_path):
    """The driver's environment: fakes first on PATH, no isolation, fast waits, and a
    tmux socket folder of its own, which the caller removes (TMUX_TMPDIR)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(FAKE_AGENT)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "claude-fresh").write_text(FAKE_HUMAN)
    (bin_dir / "claude-fresh").chmod(0o755)
    home = tmp_path / "driver-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "oauth-access",
                    "refreshToken": "oauth-refresh",
                    "expiresAt": 4102444800000,
                }
            }
        )
    )
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "codex-access",
                    "refresh_token": "codex-refresh",
                    "account_id": "acct",
                },
            }
        )
    )
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-x"\n[mcp_servers.secret]\ncommand = "leak"\n'
    )
    roots = tmp_path / "homes"
    roots.mkdir()
    e = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    }
    e.update(
        PATH=f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        HOME=str(home),
        DRIVE_AGENT_ISOLATION="none",
        DRIVE_AGENT_HOME_ROOT=str(roots),
        DRIVE_AGENT_SETTLE="0.3",
        DRIVE_AGENT_TURN_LIMIT="20",
        DRIVE_AGENT_LAUNCH_LIMIT="10",
        DRIVE_AGENT_TRUST_GRACE="0.5",
        FAKE_HUMAN_LOG=str(tmp_path / "human.log"),
        # tmux's socket folder is one per user, shared by every test and session, so
        # two runs with one name took each other's server. Under /tmp, since a
        # socket's path holds at most about 100 bytes and tmp_path's runs longer.
        TMUX_TMPDIR=tempfile.mkdtemp(prefix="drive-agent-test-", dir="/tmp"),
    )
    return e


def scenario(tmp_path, **keys):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"gated": ["posting"], **keys}))
    return path


def drive(env, *args, check=True, timeout=100, tool=TOOL):
    """Runs drive-agent. Past `timeout`, under pytest's 120 s, it lists drive-agent's
    child processes, asks it where each thread is (SIGUSR1), stops it, and fails with
    both, so a hang says where it hung."""
    process = subprocess.Popen(
        [str(tool), *map(str, args)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        children = subprocess.run(
            ["ps", "-o", "pid,etimes,args", "--ppid", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        process.send_signal(signal.SIGUSR1)
        # SIGTERM after the dump, so drive-agent cleans up as it stops.
        process.terminate()
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
        pytest.fail(
            f"drive-agent ran past {timeout:g} s.\nIts child processes:\n{children}\n"
            f"Its stdout:\n{out}\nIts stderr, each thread's stack included:\n{err}"
        )
    done = subprocess.CompletedProcess(process.args, process.returncode, out, err)
    if check:
        assert done.returncode == 0, done.stderr
    return done


def human_prompts(env):
    path = Path(env["FAKE_HUMAN_LOG"])
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_types_each_turn_after_the_last_ends_and_saves_the_run(tmp_path, env, harness):
    path = scenario(tmp_path, turns=["first", "second"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", harness, "--name", "jason1", "--out", out)
    log = (out / "turns.md").read_text()
    assert "Sent: first" in log and "Sent: second" in log
    assert "NO END SEEN" not in log
    assert (out / "screen-02.txt").read_text().count("done: second") == 1
    transcripts = list(out.glob("transcript-*.jsonl"))
    assert len(transcripts) == 1
    assert "done: second" in transcripts[0].read_text()
    run = json.loads((out / "run.json").read_text())
    assert run["harness"] == harness and run["outcome"] == "done"
    assert run["isolation"] == "none" and "9.9.9" in run["version"]
    assert json.loads((out / "scenario.json").read_text())["turns"] == [
        "first",
        "second",
    ]
    assert not (Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason1").exists()


def test_agent_gets_only_its_own_environment(tmp_path, env):
    path = scenario(tmp_path, turns=["hi"])
    out = tmp_path / "out"
    env = {**env, "SECRET_OF_DRIVER": "s1", "PASSED": "p1"}
    drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason2",
        "--out",
        out,
        "--agent-env",
        "PASSED",
    )
    seen = json.loads((out / "files" / "seen.json").read_text())
    assert seen["user"] == "jason2"
    agent_env = seen["env"]
    assert agent_env["PASSED"] == "p1"
    assert "SECRET_OF_DRIVER" not in agent_env
    # The agent got the token; files/ saves it blanked.
    assert agent_env["CLAUDE_CODE_OAUTH_TOKEN"] == "REDACTED"
    assert agent_env["HOME"].endswith("/jason2")
    assert agent_env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert not any("/home/" in p for p in agent_env["PATH"].split(":")[1:])


def test_codex_gets_its_access_token_and_no_refresh_token_or_driver_config(
    tmp_path, env
):
    path = scenario(tmp_path, turns=["hi"])
    out = tmp_path / "out"
    drive(
        env,
        "run",
        path,
        "--harness",
        "codex",
        "--name",
        "jason3",
        "--out",
        out,
        "--keep-home",
    )
    codex_home = Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason3" / ".codex"
    auth = json.loads((codex_home / "auth.json").read_text())
    assert auth["tokens"]["access_token"] == "codex-access"
    assert not auth["tokens"].get("refresh_token")
    config = (codex_home / "config.toml").read_text()
    assert 'model = "gpt-x"' in config and "leak" not in config


def test_title_notifications_dont_count_as_turns(tmp_path, env):
    # Each fake Codex turn notifies twice, the first for the title side thread; were
    # it counted, the second turn would be typed before the first ended, and the log
    # would show the second turn's reply on the first turn's screen.
    path = scenario(tmp_path, turns=["one", "two", "three"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "codex", "--name", "jason4", "--out", out)
    assert "done: two" not in (out / "screen-01.txt").read_text()
    assert "done: three" in (out / "screen-03.txt").read_text()


def test_vars_fill_placeholders_and_an_unset_one_is_refused(tmp_path, env):
    path = scenario(tmp_path, turns=["key is {KEY}"])
    out = tmp_path / "out"
    drive(
        {**env, "KEY": "k-123"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason5",
        "--out",
        out,
        "--var",
        "KEY",
    )
    assert "done: key is k-123" in (out / "screen-01.txt").read_text()
    # The log keeps the placeholder, so a key never lands in turns.md.
    assert "k-123" not in (out / "turns.md").read_text()
    refused = drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason6",
        "--out",
        tmp_path / "out2",
        "--var",
        "KEY",
        check=False,
    )
    assert refused.returncode == 2 and "KEY" in refused.stderr


def test_conditional_turn_fires_only_when_the_human_says_it_holds(tmp_path, env):
    path = scenario(
        tmp_path,
        turns=[
            "hello",
            {"text": "Yes, post it.", "only_if": "the agent asks to post"},
            "ask",
            {"text": "Yes, post it.", "only_if": "the agent asks to post"},
        ],
    )
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason7", "--out", out)
    log = (out / "turns.md").read_text()
    assert log.count("Sent: Yes, post it.") == 1
    assert "## Turn 2: skipped" in log and "## Turn 4 (" in log
    # The skipped turn approved nothing: the prompt for turn 4 lists only what was
    # sent before it.
    history = (
        human_prompts(env)[-1].split("Scripted messages so far: ")[1].split("\n")[0]
    )
    assert json.loads(history) == ["hello", "ask"]


def test_a_conditional_first_turn_sends_its_else(tmp_path, env):
    path = scenario(
        tmp_path,
        turns=[
            {"text": "Yes, post it.", "only_if": "the agent asks", "else": "Hi there."}
        ],
    )
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason17", "--out", out)
    log = (out / "turns.md").read_text()
    assert "Sent: Hi there." in log and "Sent: Yes, post it." not in log


def test_an_empty_answer_sends_an_unconditional_turn_as_scripted(tmp_path, env):
    path = scenario(tmp_path, turns=["hello", "again"])
    out = tmp_path / "out"
    drive(
        {**env, "FAKE_HUMAN": "empty"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason18",
        "--out",
        out,
    )
    assert "Sent: again" in (out / "turns.md").read_text()


def test_restart_starts_a_new_session_in_the_same_home(tmp_path, env):
    path = scenario(tmp_path, turns=["one", "{RESTART}", "two"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason19", "--out", out)
    log = (out / "turns.md").read_text()
    assert "## Turn 2: new session" in log and "Sent: two" in log
    assert len(list(out.glob("transcript-*.jsonl"))) == 2


def transcript_records(out):
    """Every record of each saved session transcript (not a subagent's), by
    transcript, oldest first."""
    return {
        path.name: [json.loads(line) for line in path.read_text().splitlines()]
        for path in out.glob("transcript-*.jsonl")
        if not path.name.startswith("transcript-agent-")
    }


def dialog_answers(records):
    return [
        part["content"]
        for rec in records
        for part in (rec["message"]["content"] if rec["type"] == "user" else [])
        if isinstance(part, dict) and part.get("type") == "tool_result"
    ]


def typed(records):
    return [
        rec["message"]["content"]
        for rec in records
        if rec["type"] == "user" and isinstance(rec["message"]["content"], str)
    ]


def test_a_question_dialog_ends_the_turn_and_takes_the_next_one_as_its_answer(
    tmp_path, env
):
    # jason86: the answered dialog stays drawn while the agent works on, which the
    # driver took for the end of the turn, typing the next turn mid-work.
    path = scenario(tmp_path, turns=["dialog", "Yes, both.", "after"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason30", "--out", out)
    (records,) = transcript_records(out).values()
    answers = dialog_answers(records)
    assert len(answers) == 1 and '="Yes, both."' in answers[0]
    assert typed(records) == ["dialog", "after"]
    assert "done: answer Yes, both." in (out / "screen-02.txt").read_text()
    log = (out / "turns.md").read_text()
    turn1, turn2 = log.split("## Turn 2")[0], log.split("## Turn 2")[1]
    assert "asks in a dialog" in turn1 and "Which colour do you prefer?" in turn1
    assert "Answered in the question dialog" in turn2
    assert "Which colour do you prefer?" in turn2.split("## Turn 3")[0]
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def test_a_restart_after_a_dialog_shows_the_human_nothing_of_the_old_session(
    tmp_path, env
):
    # jason86: after {RESTART} the simulated human read the old session's dialog
    # and answered it again at the top of the next turn.
    path = scenario(tmp_path, turns=["dialog", "Yes, both.", "{RESTART}", "next"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason31", "--out", out)
    # The new session has said nothing, so there is nothing to ask the human about.
    assert not [p for p in human_prompts(env) if "<<<\nnext\n>>>" in p]
    sessions = transcript_records(out).values()
    assert sorted(typed(records) for records in sessions) == [["dialog"], ["next"]]
    assert sum(len(dialog_answers(records)) for records in sessions) == 1


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_condition_after_a_restart_is_judged_on_nothing_of_the_old_session(
    tmp_path, env, harness
):
    path = scenario(
        tmp_path,
        turns=["ask", "{RESTART}", {"text": "Yes, post it.", "only_if": "it asks"}],
    )
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", harness, "--name", "jason34", "--out", out)
    log = (out / "turns.md").read_text()
    assert "## Turn 3: skipped, no agent message to judge" in log


@pytest.mark.parametrize(
    ("kind", "count", "questions"),
    [
        ("dialog2", 2, ["Which colour do you prefer?", "Which animal do you prefer?"]),
        ("dialogm", 1, ["Which colour do you prefer?"]),
    ],
)
def test_a_dialog_of_several_or_multi_select_questions_is_left_for_the_chat(
    tmp_path, env, kind, count, questions
):
    # jason82 and jason85: the answer went into the first tab and the dialog moved
    # on to the next, which the driver took for a turn end; nothing was submitted.
    # A person answering in their own words picks "Chat about this" and types there.
    path = scenario(tmp_path, turns=[kind, "Yes, both.", "after"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "claude", "--name", "jason32", "--out", out)
    (records,) = transcript_records(out).values()
    answers = dialog_answers(records)
    assert len(answers) == 1 and "clarify" in answers[0]
    assert typed(records) == [kind, "Yes, both.", "after"]
    assert "done: Yes, both." in (out / "screen-02.txt").read_text()
    turn2 = (out / "turns.md").read_text().split("## Turn 2")[1].split("## Turn 3")[0]
    assert f"Left a dialog of {count} question" in turn2
    assert '"Chat about this"' in turn2 and "What would you like to clarify?" in turn2
    for question in questions:
        assert question in turn2
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def test_a_dialog_the_transcript_gets_late_is_read_from_the_screen_and_answered(
    tmp_path, env
):
    # jason121 and jason141: the dialog stood on screen for the whole turn limit
    # while the transcript held no AskUserQuestion call, so the driver never answered.
    path = scenario(tmp_path, turns=["late", "Yes, both.", "after"])
    out = tmp_path / "out"
    drive(
        {**env, "DRIVE_AGENT_DIALOG_GRACE": "5"},
        *("run", path, "--harness", "claude", "--name", "jason36", "--out", out),
    )
    (records,) = transcript_records(out).values()
    answers = dialog_answers(records)
    assert len(answers) == 1 and '="Yes, both."' in answers[0]
    assert typed(records) == ["late", "after"]
    log = (out / "turns.md").read_text()
    turn1, turn2 = log.split("## Turn 2")[0], log.split("## Turn 2")[1]
    assert "asks in a dialog" in turn1 and "read from the screen" in turn1
    assert "Which colour do you prefer?" in turn1
    assert "Answered in the question dialog" in turn2
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


@pytest.mark.parametrize(
    ("kind", "left"),
    [("late2", "Left a dialog of 2 questions"), ("latem", "Left a dialog of 1 ")],
)
def test_a_dialog_of_several_or_multi_select_questions_the_transcript_gets_late_is_left_for_the_chat(
    tmp_path, env, kind, left
):
    # probe5m (Claude Code 2.1.332): a multi-select dialog draws a tab line, as a
    # dialog of several questions does, and its free-text option is a checkbox.
    path = scenario(tmp_path, turns=[kind, "Yes, both.", "after"])
    out = tmp_path / "out"
    drive(
        {**env, "DRIVE_AGENT_DIALOG_GRACE": "5"},
        *("run", path, "--harness", "claude", "--name", "jason37", "--out", out),
    )
    (records,) = transcript_records(out).values()
    answers = dialog_answers(records)
    assert len(answers) == 1 and "clarify" in answers[0]
    assert typed(records) == [kind, "Yes, both.", "after"]
    turn2 = (out / "turns.md").read_text().split("## Turn 2")[1].split("## Turn 3")[0]
    assert left in turn2
    assert "Colour" in turn2
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def codex_human_texts(records):
    return [
        "".join(c["text"] for c in rec["payload"]["content"])
        for rec in records
        if rec["payload"].get("type") == "message" and rec["payload"]["role"] == "user"
    ]


def test_codex_async_question_ends_the_turn_and_takes_the_next_as_its_answer(
    tmp_path, env
):
    # jason97: Codex asked through request_user_input_async, worked on while it
    # waited for an answer, and after 60 s posted without one: its notify comes
    # only when the whole turn ends, so the driver never saw a question.
    path = scenario(tmp_path, turns=["async", "Blue.", "after"])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "codex", "--name", "jason70", "--out", out)
    (records,) = transcript_records(out).values()
    assert codex_human_texts(records) == ["async", "Blue.", "after"]
    assert "done: answer Blue." in (out / "screen-02.txt").read_text()
    log = (out / "turns.md").read_text()
    turn1, rest = log.split("## Turn 2")
    turn2 = rest.split("## Turn 3")[0]
    assert "asks through request_user_input_async" in turn1
    assert "Which colour do you prefer? (Red / Blue)" in turn1
    assert "Answered its request_user_input_async question" in turn2
    assert "Sent: Blue." in turn2
    # The human judged the question, not the last turn's notify message.
    (prompt,) = [p for p in human_prompts(env) if "<<<\nBlue.\n>>>" in p]
    assert "Which colour do you prefer?" in prompt
    # Turn 3 went only once the answered turn ended, not on the earlier question.
    assert "<<<\ndone: answer Blue.\n>>>" in human_prompts(env)[-1]
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def test_codex_async_question_in_a_turn_that_ended_reaches_the_human(tmp_path, env):
    # The notify message is the turn's last text, which may not hold the question.
    path = scenario(tmp_path, turns=["asyncend", "Blue."])
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "codex", "--name", "jason71", "--out", out)
    (prompt,) = [p for p in human_prompts(env) if "<<<\nBlue.\n>>>" in p]
    assert "Which colour do you prefer?" in prompt
    assert "Tell me when you've chosen." in prompt
    log = (out / "turns.md").read_text()
    assert "## Turn 1 (ended" in log
    assert "done: Blue." in (out / "screen-02.txt").read_text()


def codex_run(tmp_path, env, turns, name, wait=None, check=True):
    path = scenario(tmp_path, turns=turns)
    out = tmp_path / "out"
    given = {**env, "FAKE_ASYNC_WAIT": wait} if wait else env
    extra = ("--agent-env", "FAKE_ASYNC_WAIT") if wait else ()
    done = drive(
        given,
        *("run", path, "--harness", "codex", "--name", name, "--out", out, *extra),
        check=check,
    )
    return out, done


def test_codex_question_reaches_the_human_last_so_no_cut_drops_it(tmp_path, env):
    codex_run(tmp_path, env, ["async", "Blue."], "jason74")
    (prompt,) = [p for p in human_prompts(env) if "<<<\nBlue.\n>>>" in p]
    last = prompt.split("The agent's last message:\n<<<\n")[1].split("\n>>>")[0]
    assert last.startswith("Before I go on, one question.")
    assert "I'll work on meanwhile." in last
    assert last.endswith("Which colour do you prefer? (Red / Blue)")


def test_a_turn_skipped_while_a_codex_question_runs_waits_for_its_turn_to_end(
    tmp_path, env
):
    # The jason97 shape: a conditional go-ahead that doesn't hold after Codex asks.
    # The next sent turn must not go into the running turn as the answer.
    out, _ = codex_run(
        tmp_path,
        env,
        ["async", {"text": "Yes, post it.", "only_if": "it asks to post"}, "after"],
        "jason75",
        wait="3",
    )
    (records,) = transcript_records(out).values()
    assert codex_human_texts(records) == ["async", "after"]
    assert "went on without an answer" in (out / "screen-03.txt").read_text()
    log = (out / "turns.md").read_text()
    turn2 = log.split("## Turn 2")[1].split("## Turn 3")[0]
    assert "skipped" in turn2 and "waited for the turn that asked to end" in turn2
    turn3 = log.split("## Turn 3")[1]
    assert (
        "Answered its" not in turn3
        and "done: after" in (out / "screen-03.txt").read_text()
    )


def test_codex_question_whose_turn_ends_before_the_answer_waits_for_the_answers_turn(
    tmp_path, env
):
    # The fake goes on without an answer after 1.6 s. drive-agent sees the question
    # at its first or second poll (0 or 1 s), holds it for SETTLE (1 s), and types
    # the answer for 1.2 s before Enter, so the old turn has ended by then, and the
    # answer starts a new turn, whose end the next turn waits for.
    out, _ = codex_run(
        tmp_path,
        {**env, "DRIVE_AGENT_SETTLE": "1"},
        ["async", "Blue.", "after"],
        "jason76",
        wait="1.6",
    )
    (records,) = transcript_records(out).values()
    assert codex_human_texts(records) == ["async", "Blue.", "after"]
    log = (out / "turns.md").read_text()
    assert "## Turn 1 (asks through request_user_input_async" in log
    assert "done: Blue." in (out / "screen-02.txt").read_text()
    (prompt,) = [p for p in human_prompts(env) if "<<<\nafter\n>>>" in p]
    assert "<<<\ndone: Blue.\n>>>" in prompt


def test_a_codex_answer_that_starts_like_markup_is_still_the_answer(tmp_path, env):
    out, _ = codex_run(tmp_path, env, ["async", "<b>Blue</b>", "after"], "jason77")
    assert "done: answer <b>Blue</b>" in (out / "screen-02.txt").read_text()
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def test_codex_dying_in_the_answered_turn_fails_the_run_at_once(tmp_path, env):
    began = time.time()
    out, done = codex_run(
        tmp_path, env, ["asyncdie", "Blue.", "after"], "jason78", check=False
    )
    assert done.returncode == 1
    # Well under the 20 s turn limit: the dead pane ends the wait.
    assert time.time() - began < 15
    assert "NO END SEEN" in (out / "turns.md").read_text()


@pytest.mark.parametrize(
    ("option", "multi"),
    [("1. [ ] Cheese", True), ("1. [✔] Cheese", True), ("1. [A] Apply", False)],
)
def test_only_checkbox_options_make_a_dialog_read_from_the_screen_multi_select(
    option, multi
):
    screen = "\n".join(
        [
            "─" * 40,
            " ☐ Pick",
            "Which one?",
            f"❯ {option}",
            "  2. Type something",
            "─" * 40,
            "  3. Chat about this",
            "Enter to select · ↑/↓ to navigate · Esc to cancel",
        ]
    )
    (question,) = drive_agent.question_on_screen(screen)["questions"]
    assert question["multiSelect"] is multi


def test_a_dialog_that_is_no_question_is_never_answered(tmp_path, env):
    path = scenario(tmp_path, turns=["permission", "Yes, both."])
    out = tmp_path / "out"
    done = drive(
        {**env, "DRIVE_AGENT_DIALOG_GRACE": "1", "DRIVE_AGENT_TURN_LIMIT": "6"},
        *("run", path, "--harness", "claude", "--name", "jason38", "--out", out),
        check=False,
    )
    assert done.returncode == 1
    assert json.loads((out / "run.json").read_text())["outcome"] == "dialog"
    log = (out / "turns.md").read_text()
    assert "NO END SEEN" in log and "## Turn 2" not in log


def test_a_dialog_of_several_questions_with_no_chat_option_fails_the_run(tmp_path, env):
    path = scenario(tmp_path, turns=["nochat", "Yes, both.", "after"])
    out = tmp_path / "out"
    done = drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason35",
        "--out",
        out,
        check=False,
    )
    assert done.returncode == 1
    assert json.loads((out / "run.json").read_text())["outcome"] == (
        "dialog-unanswered"
    )
    log = (out / "turns.md").read_text()
    assert "Chat about this" in log and "## Turn 3" not in log
    (records,) = transcript_records(out).values()
    assert typed(records) == ["nochat"] and not dialog_answers(records)


def test_a_dialog_that_never_takes_the_answer_fails_the_run(tmp_path, env):
    path = scenario(tmp_path, turns=["deaf", "Yes, both.", "after"])
    out = tmp_path / "out"
    done = drive(
        {**env, "DRIVE_AGENT_ANSWER_LIMIT": "1"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason33",
        "--out",
        out,
        check=False,
    )
    assert done.returncode == 1
    assert json.loads((out / "run.json").read_text())["outcome"] == (
        "dialog-unanswered"
    )
    assert "## Turn 3" not in (out / "turns.md").read_text()


def test_cleanup_refuses_a_user_drive_agent_didnt_make(tmp_path, env):
    for name in ("root", "..", "nobody-made-this"):
        done = drive(env, "cleanup", name, check=False)
        assert done.returncode == 2, (name, done.stderr)


def test_a_failed_human_never_fires_a_conditional_turn(tmp_path, env):
    path = scenario(
        tmp_path,
        turns=[
            "ask",
            {"text": "Yes, post it.", "only_if": "the agent asks to post"},
            "bye",
        ],
    )
    out = tmp_path / "out"
    drive(
        {**env, "FAKE_HUMAN": "fail"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason8",
        "--out",
        out,
    )
    log = (out / "turns.md").read_text()
    assert "Sent: Yes, post it." not in log
    assert "simulated human failed" in log
    assert "Sent: bye" in log


def test_human_prompt_holds_the_persona_and_gated_actions(tmp_path, env):
    persona = tmp_path / "persona.md"
    persona.write_text("You are Ada, a researcher.")
    path = scenario(
        tmp_path,
        turns=["hello", "again"],
        persona="You dislike git.",
        gated=["posting to File Drawer", "upgrading the skills"],
    )
    drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason9",
        "--out",
        tmp_path / "out",
        "--persona",
        persona,
    )
    prompts = human_prompts(env)
    assert len(prompts) == 1  # the first turn has no agent message to answer
    assert "You are Ada, a researcher." in prompts[0]
    assert "You dislike git." in prompts[0]
    assert (
        "posting to File Drawer" in prompts[0] and "upgrading the skills" in prompts[0]
    )
    assert "done: hello" in prompts[0]


def test_projects_are_committed_and_new_files_collected(tmp_path, env):
    projects = tmp_path / "projects"
    (projects / "probe").mkdir(parents=True)
    (projects / "probe" / "NOTES.md").write_text("notes\n")
    path = scenario(tmp_path, turns=["hi"], projects=["probe"])
    out = tmp_path / "out"
    drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason10",
        "--out",
        out,
        "--projects",
        projects,
        "--git-name",
        "Ada Lovelace",
        "--keep-home",
    )
    home = Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason10"
    log = subprocess.run(
        ["git", "-C", str(home / "code" / "probe"), "log", "--format=%an <%ae>"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert log.strip() == "Ada Lovelace <jason10@example.org>"
    seen = json.loads((out / "files" / "seen.json").read_text())
    assert seen["cwd"] == str(home / "code" / "probe")
    # Unchanged project files and harness folders aren't collected.
    assert not (out / "files" / "code" / "probe" / "NOTES.md").exists()
    assert not (out / "files" / ".claude").exists()


def test_setup_runs_before_the_agent_with_its_own_environment(tmp_path, env):
    setup = tmp_path / "setup.py"
    setup.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "s = json.load(open(sys.argv[1]))\n"
        "p = os.path.join(os.environ['HOME'], 'made-by-setup.txt')\n"
        "open(p, 'w').write(s['flavour'] + ' ' + os.environ.get('SETUP_ONLY', '-'))\n"
    )
    path = scenario(tmp_path, turns=["hi"], flavour="mint")
    out = tmp_path / "out"
    drive(
        {**env, "SETUP_ONLY": "s-1"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason11",
        "--out",
        out,
        "--setup",
        setup,
        "--setup-env",
        "SETUP_ONLY",
    )
    assert (out / "files" / "made-by-setup.txt").read_text() == "mint s-1"
    seen = json.loads((out / "files" / "seen.json").read_text())
    assert "SETUP_ONLY" not in seen["env"]


def test_config_is_saved_with_the_given_values_and_keys_blanked(tmp_path, env):
    key = "a1" * 20
    token = "fdt_" + "k" * 52
    setup = tmp_path / "setup.py"
    setup.write_text(
        "#!/usr/bin/env python3\nimport os\n"
        "d = os.path.join(os.environ['HOME'], '.config', 'tool')\n"
        "os.makedirs(d)\n"
        "open(os.path.join(d, 'site.header'), 'w').write("
        f"'X-Staging-Key: ' + os.environ['STAGING'] + '\\nAuthorization: Bearer {token}\\n')\n"
        "open(os.path.join(d, 'upgrade'), 'w').write('manual\\n')\n"
        "open(os.path.join(d, 'note'), 'w').write('pw ' + os.environ['PLAIN'] + ' end')\n"
        "open(os.path.join(os.environ['HOME'], 'copy.txt'), 'w')"
        ".write('copy ' + os.environ['PLAIN'])\n"
    )
    out = tmp_path / "out"
    drive(
        {**env, "STAGING": key, "PLAIN": "open-sesame-42"},
        "run",
        scenario(tmp_path, turns=["hi"]),
        "--harness",
        "claude",
        "--name",
        "jason12",
        "--out",
        out,
        "--setup",
        setup,
        "--setup-env",
        "STAGING",
        "--setup-env",
        "PLAIN",
    )
    saved = out / "files" / ".config" / "tool"
    assert (saved / "upgrade").read_text() == "manual\n"
    header = (saved / "site.header").read_text()
    assert key not in header and token not in header
    assert header.startswith("X-Staging-Key: ") and "Authorization: Bearer " in header
    note = (saved / "note").read_text()
    assert "open-sesame-42" not in note
    assert note.startswith("pw ") and note.endswith(" end")
    # A given value is blanked outside ~/.config too.
    assert (out / "files" / "copy.txt").read_text() == "copy REDACTED"


def test_print_mode_sends_the_first_turn_once(tmp_path, env):
    path = scenario(tmp_path, turns=["just this"], print_mode=True)
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "codex", "--name", "jason43", "--out", out)
    assert "done: just this" in (out / "print-mode.jsonl").read_text()


def test_a_trust_dialog_after_the_prompt_is_answered_before_the_first_turn(
    tmp_path, env
):
    setup = tmp_path / "setup.py"
    setup.write_text('#!/bin/sh\ntouch "$HOME/trust-late"\n')
    path = scenario(tmp_path, turns=["hi"])
    out = tmp_path / "out"
    drive(
        {**env, "DRIVE_AGENT_TRUST_GRACE": "3"},
        "run",
        path,
        "--harness",
        "codex",
        "--name",
        "jason20",
        "--out",
        out,
        "--setup",
        setup,
    )
    screen = (out / "screen-01.txt").read_text()
    assert "done: hi" in screen and "dropped:" not in screen


def test_a_harness_that_never_gets_ready_fails_with_its_screen(tmp_path, env):
    setup = tmp_path / "setup.py"
    setup.write_text('#!/bin/sh\ntouch "$HOME/never-ready"\n')
    path = scenario(tmp_path, turns=["hi"])
    out = tmp_path / "out"
    done = drive(
        {**env, "DRIVE_AGENT_LAUNCH_LIMIT": "3"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason13",
        "--out",
        out,
        "--setup",
        setup,
        check=False,
    )
    assert done.returncode == 1
    assert "Something unexpected" in (out / "screen-launch.txt").read_text()
    assert json.loads((out / "run.json").read_text())["outcome"] == "not-ready"
    assert (
        "Sent:" not in (out / "turns.md").read_text()
        if (out / "turns.md").exists()
        else True
    )
    assert not (Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason13").exists()


def test_existing_out_or_home_refuses(tmp_path, env):
    path = scenario(tmp_path, turns=["hi"])
    out = tmp_path / "out"
    out.mkdir()
    done = drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason14",
        "--out",
        out,
        check=False,
    )
    assert done.returncode == 2 and "exists" in done.stderr
    (Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason15").mkdir()
    done = drive(
        env,
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason15",
        "--out",
        tmp_path / "out2",
        check=False,
    )
    assert done.returncode == 2 and "exists" in done.stderr


class FakeUpstream:
    """An HTTP proxy that records each CONNECT and answers 200, then closes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.seen = []
        threading.Thread(target=self.serve, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = conn.recv(1000).decode()
            self.seen.append(data.splitlines()[0])
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            conn.close()


def test_proxy_refuses_github_and_loopback_and_forwards_the_rest(tmp_path, env):
    upstream = FakeUpstream()
    path = scenario(
        tmp_path,
        turns=[
            "fetch github.com",
            "fetch api.github.com",
            "fetch 127.0.0.1",
            "fetch localhost",
            "fetch gist.github.com",
            "fetch 127.1",
            "fetch 0x7f000001",
            "fetch 0",
            "fetch staging.example.org",
        ],
    )
    out = tmp_path / "out"
    drive(
        {**env, "HTTPS_PROXY": upstream.url},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason16",
        "--out",
        out,
    )
    screens = [(out / f"screen-0{n}.txt").read_text() for n in range(1, 10)]
    for n, screen in enumerate(screens[:8], 1):
        assert "fetched: HTTP/1.1 403" in screen, n
    assert "fetched: HTTP/1.1 200" in screens[8]
    assert upstream.seen == ["CONNECT staging.example.org:443 HTTP/1.1"]


# The proxy with no upstream proxy connects itself, which a run can't show without
# reaching the internet: the deny list refuses every local address. These tests load
# the tool as a module and run its Proxy in the test's process, with 127.0.0.1 let
# through as if it were public.
def load_drive_agent():
    loader = importlib.machinery.SourceFileLoader("drive_agent", str(TOOL))
    spec = importlib.util.spec_from_loader("drive_agent", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


drive_agent = load_drive_agent()
PUBLIC = "127.0.0.1"  # the listener the tests run, which the proxy takes as public


class Service:
    """A server for one connection on `host`: it reads until it has `expect`, answers
    `reply`, and keeps what it read in `got`."""

    def __init__(self, host, expect, reply, port=0):
        self.sock = socket.socket()
        self.sock.bind((host, port))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.expect, self.reply, self.got = expect, reply, b""
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                while not self.got.endswith(self.expect):
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    self.got += chunk
                conn.sendall(self.reply)
                while chunk := conn.recv(4096):
                    self.got += chunk
            except OSError:
                pass


def stop_listening(sock):
    """Closes a listening socket, first waking the thread blocked in its accept(),
    which close() alone leaves blocked on Linux, holding the port."""
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()


@pytest.fixture
def serve():
    """Starts Services as `serve(host, expect, reply[, port])`, closing them after."""
    started = []

    def start(*args, **kw):
        started.append(Service(*args, **kw))
        return started[-1]

    yield start
    for service in started:
        stop_listening(service.sock)


@pytest.fixture
def proxy(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    real_local = drive_agent.local
    monkeypatch.setattr(drive_agent, "local", lambda a: a != PUBLIC and real_local(a))
    p = drive_agent.Proxy([])
    yield p
    stop_listening(p.sock)
    stop_listening(p.unix)
    shutil.rmtree(os.path.dirname(p.path), ignore_errors=True)


def ask(proxy, request, until):
    """Sends `request` to the proxy and returns what comes back, read until it ends
    with `until` or the connection closes."""
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as conn:
        conn.sendall(request)
        got = b""
        while not got.endswith(until):
            chunk = conn.recv(4096)
            if not chunk:
                break
            got += chunk
        return got


def test_proxy_with_no_upstream_answers_connect_and_passes_bytes_both_ways(
    proxy, serve
):
    service = serve(PUBLIC, b"hello", b"pong")
    request = f"CONNECT {PUBLIC}:{service.port} HTTP/1.1\r\nHost: x\r\n\r\n"
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as conn:
        # Bytes sent with the request, before the 200, are the tunnel's first.
        conn.sendall(request.encode() + b"hello")
        want = b"HTTP/1.1 200 Connection established\r\n\r\npong"
        got = b""
        while len(got) < len(want) and (chunk := conn.recv(4096)):
            got += chunk
        assert got == want
        conn.sendall(b" world")
        conn.shutdown(socket.SHUT_WR)
        assert conn.recv(4096) == b""
    deadline = time.monotonic() + 10
    while service.got != b"hello world" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.got == b"hello world"


def test_proxy_with_no_upstream_passes_a_plain_request_on_whole(proxy, serve):
    answer = b"HTTP/1.1 204 No Content\r\n\r\n"
    service = serve(PUBLIC, b"\r\n\r\n", answer)
    head = f"GET http://{PUBLIC}:{service.port}/x HTTP/1.1\r\nHost: {PUBLIC}\r\n\r\n"
    assert ask(proxy, head.encode(), answer) == answer
    assert service.got == head.encode()


@pytest.mark.parametrize(
    "method, target, want",
    [
        ("GET", "http://example.org/x", ("example.org", 80)),
        ("GET", "http://example.org:8080/x", ("example.org", 8080)),
        ("CONNECT", "example.org", ("example.org", 443)),
        ("CONNECT", "example.org:8443", ("example.org", 8443)),
        ("CONNECT", "[::1]:8080", ("::1", 8080)),
        ("CONNECT", "[::1]", ("::1", 443)),
    ],
)
def test_target_of_reads_host_and_port_with_each_methods_default(method, target, want):
    assert drive_agent.target_of(method, target) == want


def outward_address():
    """This machine's address on its default route, which a UDP connect finds without
    sending anything, or None with no route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
        except OSError:
            return None
        return s.getsockname()[0]


def test_proxy_listens_on_loopback_only(proxy):
    address = outward_address()
    if address is None or ipaddress.ip_address(address).is_loopback:
        pytest.skip("no address but loopback")
    socket.create_connection(("127.0.0.1", proxy.port), timeout=10).close()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((address, proxy.port), timeout=10)


@pytest.mark.parametrize("method", ["CONNECT", "GET"])
def test_proxy_connects_to_the_address_it_checked_not_a_new_lookup(
    proxy, serve, monkeypatch, method
):
    # A name whose first lookup is public and any later one loopback, as a DNS
    # rebinding attack answers: a second lookup would reach the loopback service.
    # Each service answers as soon as a connection comes.
    first = serve(PUBLIC, b"", b"first")
    serve("127.0.0.2", b"", b"second", port=first.port)
    real = socket.getaddrinfo
    lookups = []

    def getaddrinfo(host, port, *args, **kw):
        if host != "rebind.test":
            return real(host, port, *args, **kw)
        lookups.append(host)
        address = PUBLIC if len(lookups) == 1 else "127.0.0.2"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    target = f"rebind.test:{first.port}"
    if method == "GET":
        target = f"http://{target}/"
    request = f"{method} {target} HTTP/1.1\r\nHost: rebind.test\r\n\r\n".encode()
    assert ask(proxy, request, b"first").endswith(b"first")
    assert lookups == ["rebind.test"]


def test_digest_shows_turns_and_tools_and_redacts(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    lines = [
        {
            "type": "user",
            "message": {"content": "read https://x.dev/skill.md?key=sekrit"},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Fetching."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "curl -sS x.dev?key=sekrit"},
                    },
                ]
            },
        },
    ]
    (run / "transcript-a.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")
    codex = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"text": "hi codex"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"cmd": "ls -la"}),
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "total 0"},
        },
    ]
    (run / "transcript-rollout-b.jsonl").write_text(
        "\n".join(map(json.dumps, codex)) + "\n"
    )
    out = subprocess.run(
        [str(TOOL), "digest", str(run), "--redact", r"key=[^&\s]+"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "### HUMAN: read https://x.dev/skill.md?" in out
    assert "sekrit" not in out
    assert "AGENT: Fetching." in out and "[Bash] curl -sS" in out
    assert "### HUMAN: hi codex" in out and "[shell] ls -la" in out
    assert "total 0" not in out
    verbose = subprocess.run(
        [str(TOOL), "digest", str(run), "-v"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "-> total 0" in verbose


def claude_record(ts, kind, content):
    return {"type": kind, "timestamp": ts, "message": {"content": content}}


def call(name, **given):
    return [{"type": "tool_use", "name": name, "input": given}]


def write_jsonl(path, records):
    path.write_text("\n".join(map(json.dumps, records)) + "\n")


def test_digest_calls_shows_humans_and_calls_one_line_each_in_time_order(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    # The file named first ran second, as a restarted session's may.
    write_jsonl(
        run / "transcript-a.jsonl",
        [
            {"type": "mode", "mode": "normal"},
            claude_record("2026-11-13T23:25:00Z", "user", "second session"),
            claude_record(
                "2026-11-13T23:25:01Z", "assistant", call("Skill", skill="s")
            ),
        ],
    )
    write_jsonl(
        run / "transcript-b.jsonl",
        [
            claude_record("2026-11-13T23:24:00Z", "user", "first\nsession"),
            claude_record(
                "2026-11-13T23:24:01Z",
                "assistant",
                [{"type": "text", "text": "Sure, fetching it now."}]
                + call("Bash", command="curl -fsSL https://x.dev/skill.md | head"),
            ),
            claude_record(
                "2026-11-13T23:24:02Z",
                "user",
                [{"type": "tool_result", "content": "# Skill text"}],
            ),
            claude_record(
                "2026-11-13T23:24:03Z",
                "assistant",
                call("WebFetch", url="https://x.dev/a"),
            ),
            claude_record(
                "2026-11-13T23:24:04Z",
                "assistant",
                call("AskUserQuestion", questions=[{"question": "Install them?"}]),
            ),
            {
                **claude_record(
                    "2026-11-13T23:24:05Z",
                    "user",
                    [{"type": "tool_result", "content": "The user answered: …"}],
                ),
                "toolUseResult": {"answers": {"Install them?": "Yes, both."}},
            },
        ],
    )
    out = subprocess.run(
        [str(TOOL), "digest", str(run), "--calls", "--width", "30"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines() == [
        "# transcript-b.jsonl",
        "### HUMAN: first session",
        "  [Bash] curl -fsSL https://x.dev/skil…",
        "  [WebFetch] https://x.dev/a",
        "  [AskUserQuestion] Install them?",
        "### HUMAN (in the dialog): Yes, both.",
        "# transcript-a.jsonl",
        "### HUMAN: second session",
        "  [Skill] {'skill': 's'}",
    ]


def test_digest_calls_reads_codex_transcripts(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_jsonl(
        run / "transcript-rollout-x.jsonl",
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "install it"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "Installing."}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sh install.sh auto"}),
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "installed"},
            },
        ],
    )
    out = subprocess.run(
        [str(TOOL), "digest", str(run), "--calls", "-v"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines() == [
        "# transcript-rollout-x.jsonl",
        "### HUMAN: install it",
        "  [exec_command] sh install.sh auto",
    ]


def test_digest_blanks_keys_and_tokens_unless_told_not_to(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    key = "0123456789abcdef" * 4
    token = "fdt_" + "q7" * 26
    commit = "c0ffee" * 6 + "abcd"
    command = (
        f"K={key}; curl -H 'X-Staging-Key: notahexkey' -H 'Authorization: Bearer "
        f"tok-123' 'https://x.dev/a?key=fieldkey&b=1' -d {token} -H 'Authorization: "
        f"Basic dXNlcjpw' && FILEDRAWER_TOKEN=plain-tok git show {commit} && "
        'grep -v "^Authorization:\\"" f'
    )
    write_jsonl(
        run / "transcript-a.jsonl",
        [
            claude_record(
                "2026-11-13T23:24:00Z", "assistant", call("Bash", command=command)
            )
        ],
    )

    def digest(*extra):
        return subprocess.run(
            [str(TOOL), "digest", str(run), "--width", "400", *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    out = digest()
    for secret in (
        key,
        "notahexkey",
        "tok-123",
        "fieldkey",
        token[4:],
        "dXNlcjpw",
        "plain-tok",
    ):
        assert secret not in out
    assert "X-Staging-Key: " in out and "Bearer " in out and "fdt_" in out
    assert "&b=1" in out and commit in out and '"^Authorization:\\""' in out
    # A key cut short by --width is blanked before the cut.
    assert key[:20] not in digest("--width", "30")
    assert "curl" not in digest("--redact", "curl")
    assert key in digest("--no-redact") and token in digest("--no-redact")


def sudo_and_bwrap():
    if subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, check=False
    ).returncode:
        return False
    return (
        subprocess.run(["which", "bwrap"], capture_output=True, check=False).returncode
        == 0
    )


@pytest.mark.skipif(not sudo_and_bwrap(), reason="needs passwordless sudo and bwrap")
def test_real_isolation_hides_the_driver_and_removes_the_user(tmp_path, env):
    name = f"datest{os.getpid() % 100000}"
    # The fake in a folder only the driver can enter, outside the roots the box
    # hides, as tmp_path is under /mnt/pytest-tmp: drive-agent has to give it to the
    # run's ~/.local/bin, as it gives Claude Code's own binary under /home.
    private = Path(f"/opt/drive-agent-test-{name}-bin")
    upstream = FakeUpstream()
    path = scenario(
        tmp_path,
        turns=[
            "fetch github.com",
            "fetch example.org",
            "direct 1.1.1.1 443",
            "direct 127.0.0.1 22",
            "selfserve",
        ],
    )
    out = tmp_path / "out"
    # A file outside /home, as the sandbox's shell setup in /etc is.
    secret = f"/opt/drive-agent-test-{name}.sh"
    e = {
        **env,
        "HTTPS_PROXY": upstream.url,
        "FAKE_HIDDEN": secret,
        "PATH": f"{private}:{env['PATH']}",
    }
    del e["DRIVE_AGENT_ISOLATION"], e["DRIVE_AGENT_HOME_ROOT"]
    try:
        subprocess.run(
            [
                "sudo",
                "-n",
                "install",
                "-d",
                "-m",
                "700",
                "-o",
                str(os.getuid()),
                private,
            ],
            check=True,
        )
        shutil.copy(Path(env["PATH"].split(":")[0]) / "claude", private / "claude")
        subprocess.run(
            ["sudo", "-n", "tee", secret],
            input="export WORKSPACE_ONLY=1\n",
            text=True,
            capture_output=True,
            check=True,
        )
        drive(
            e,
            "run",
            path,
            "--harness",
            "claude",
            "--name",
            name,
            "--out",
            out,
            "--hide",
            secret,
            "--agent-env",
            "FAKE_HIDDEN",
        )
    finally:
        subprocess.run(["sudo", "-n", "rm", "-rf", secret, private], check=False)
    seen = json.loads((out / "files" / "seen.json").read_text())
    assert seen["user"] == name
    assert seen["homes"] == [name]
    assert seen["hidden"] == ""
    assert not any(
        "drive-agent" in c or "pytest" in c or "--bind" in c for c in seen["cmdlines"]
    )
    # The run's own folder is where a login session's would be, and nothing the
    # agent can see mounted names the driver.
    runtime = f"/run/user/{seen['uid']}"
    assert seen["env"]["XDG_RUNTIME_DIR"] == runtime
    mount_points = [line.split()[4] for line in seen["mounts"].splitlines()]
    # A machine whose users have login sessions, as GitHub's runner does, mounts
    # each one's /run/user/<uid>; the box's empty /run/user covers them, and the
    # mount table still lists them, as it does any other user's on such a machine.
    in_a_session = re.compile(r"/run/user/\d+(/.*)?")
    own = (runtime, f"{runtime}/bus")
    assert [
        p
        for p in mount_points
        if p.startswith("/run/user") and (p in own or not in_a_session.fullmatch(p))
    ] == [
        "/run/user",
        runtime,
        f"{runtime}/bus",
    ]
    assert not [p for p in mount_points if "session-state" in p]
    assert "fetched: HTTP/1.1 403" in (out / "screen-01.txt").read_text()
    assert "fetched: HTTP/1.1 200" in (out / "screen-02.txt").read_text()
    for n in (3, 4):
        screen = (out / f"screen-0{n}.txt").read_text()
        assert "direct: " in screen and "direct: connected" not in screen
    assert "selfserve: 200" in (out / "screen-05.txt").read_text()
    assert (
        subprocess.run(["id", name], capture_output=True, check=False).returncode == 1
    )
    assert not Path(f"/home/{name}").exists()
    assert not Path(runtime).exists()
    rules = subprocess.run(
        ["sudo", "-n", "iptables", "-S", "OUTPUT"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert f"drive-agent-{name}" not in rules


def hanging_tmux(env, tmp_path, subcommand):
    """A tmux first on PATH that runs the real one, except `subcommand`, which notes
    it began and then never returns, as a tmux client stuck on its server would."""
    real = subprocess.run(
        ["sh", "-c", "command -v tmux"], capture_output=True, text=True, check=True
    ).stdout.strip()
    began = tmp_path / "tmux-hung"
    fake = Path(env["PATH"].split(":")[0]) / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$3" = {subcommand} ]; then echo $$ > {began}; exec sleep 3600; fi\n'
        f'exec {real} "$@"\n'
    )
    fake.chmod(0o755)
    return began


def end_hung_tmux(began: Path) -> None:
    """Kills the hung fake tmux, which no limit ends when the tool has none."""
    if began.exists() and began.read_text().strip():
        try:
            os.kill(int(began.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_tmux_call_that_never_returns_fails_the_run_and_names_it(tmp_path, env):
    began = hanging_tmux(env, tmp_path, "capture-pane")
    try:
        done = drive(
            {**env, "DRIVE_AGENT_TMUX_LIMIT": "1"},
            "run",
            scenario(tmp_path, turns=["hi"]),
            "--harness",
            "claude",
            "--name",
            "jason40",
            "--out",
            tmp_path / "out",
            check=False,
            timeout=60,
        )
    finally:
        end_hung_tmux(began)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "tmux capture-pane" in done.stderr and "1 s" in done.stderr


def test_sigusr1_prints_where_each_thread_is(tmp_path, env):
    began = hanging_tmux(env, tmp_path, "capture-pane")
    stderr = tmp_path / "stderr"
    process = subprocess.Popen(
        [
            str(TOOL),
            "run",
            str(scenario(tmp_path, turns=["hi"])),
            "--harness",
            "claude",
            "--name",
            "jason41",
            "--out",
            str(tmp_path / "out"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=(errors := stderr.open("w")),
    )
    errors.close()
    try:
        deadline = time.time() + 30
        while not began.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert began.exists(), "drive-agent never called tmux capture-pane"
        process.send_signal(signal.SIGUSR1)
        # The dump first, then the stuck tmux ended, so the stop that follows has
        # nothing to wait out.
        deadline = time.time() + 30
        while "most recent call first" not in stderr.read_text():
            if time.time() > deadline:
                break
            time.sleep(0.05)
        end_hung_tmux(began)
        process.terminate()
        process.wait(timeout=60)
        err = stderr.read_text()
    finally:
        process.kill()
        process.wait()
        end_hung_tmux(began)
    assert "most recent call first" in err, err
    assert " in tmux\n" in err, err


def test_two_runs_with_one_name_at_once_each_get_a_tmux_of_their_own(tmp_path):
    """As two tests, or two sessions' test runs, that use one name do: tmux's socket
    folder is one per user, so they once took each other's server (duplicate
    session, a turn that timed out)."""
    envs = []
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
        envs.append(make_env(tmp_path / side))

    def args(side):
        return [
            str(TOOL),
            "run",
            str(scenario(tmp_path / side, turns=["one", "two"])),
            "--harness",
            "claude",
            "--name",
            "jason42",
            "--out",
            str(tmp_path / side / "out"),
        ]

    removed = [e["TMUX_TMPDIR"] for e in envs]
    first = subprocess.Popen(
        args("a"),
        env=envs[0],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The second starts once the first's tmux server answers. The limits add up
        # to under pytest's 120 s, so drive()'s dump comes first.
        deadline = time.time() + 10
        while time.time() < deadline:
            probe = subprocess.run(
                ["tmux", "-L", "drive-agent-jason42-1", "has-session"],
                env=envs[0],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.05)
        assert probe.returncode == 0, "the first run's tmux server never answered"
        second = drive(envs[1], *args("b")[1:], check=False, timeout=50)
        _, first_err = first.communicate(timeout=50)
    finally:
        first.kill()
        first.wait()
        for folder in removed:
            remove_tmux_folder(folder)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second.stderr


def test_a_run_past_drives_limit_fails_saying_where_it_was(tmp_path, env):
    """So a hang under the full suite says where it hung, before pytest's own limit
    kills the test with no word of drive-agent's state."""
    stuck = tmp_path / "stuck"
    stuck.write_text(
        "#!/usr/bin/env python3\n"
        "import faulthandler, signal, sys, time\n"
        "faulthandler.register(signal.SIGUSR1, all_threads=True)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))\n"
        "def waiting_here():\n"
        "    time.sleep(3600)\n"
        "waiting_here()\n"
    )
    stuck.chmod(0o755)
    with pytest.raises(pytest.fail.Exception) as failed:
        drive(env, "run", tool=stuck, timeout=1)
    message = str(failed.value)
    assert "ran past 1 s" in message
    assert "waiting_here" in message


def test_a_kill_server_that_never_returns_still_ends_the_server_and_says_so(
    tmp_path, env
):
    began = hanging_tmux(env, tmp_path, "kill-server")
    # Another run's server, of the same name in a socket folder of its own, which
    # the fallback must leave alone.
    other = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMUX_TMPDIR": tempfile.mkdtemp(prefix="drive-agent-test-", dir="/tmp"),
    }
    subprocess.run(
        ["tmux", "-L", "drive-agent-jason44-1", "new-session", "-d", "sleep", "300"],
        env=other,
        check=True,
        timeout=10,
    )
    try:
        done = drive(
            {**env, "DRIVE_AGENT_TMUX_LIMIT": "2"},
            "run",
            scenario(tmp_path, turns=["hi"]),
            "--harness",
            "claude",
            "--name",
            "jason44",
            "--out",
            tmp_path / "out",
            check=False,
            timeout=60,
        )
        bystander = subprocess.run(
            ["tmux", "-L", "drive-agent-jason44-1", "has-session"],
            env=other,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        end_hung_tmux(began)
        remove_tmux_folder(other["TMUX_TMPDIR"])
    assert bystander.returncode == 0, "another socket folder's server was killed"
    assert "drive-agent-jason44-1" in done.stderr and "killed" in done.stderr, (
        done.stderr
    )
    alive = subprocess.run(
        ["tmux", "-L", "drive-agent-jason44-1", "has-session"],
        env={**env, "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )
    assert alive.returncode != 0, "the run's tmux server outlived it"


def ended(pid: int, deadline: float) -> bool:
    """Whether `pid` is gone, or a zombie, by `deadline`."""
    while time.time() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def test_kill_tmux_server_kills_the_servers_whole_tree_and_nothing_beside_it():
    folder = tempfile.mkdtemp(prefix="drive-agent-test-", dir="/tmp")
    tmux_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "TMUX_TMPDIR": folder}
    bystander = subprocess.Popen(["sleep", "300"])
    try:
        subprocess.run(
            ["tmux", "-L", "tree", "new-session", "-d", "sh -c 'sleep 300 & wait'"],
            env=tmux_env,
            check=True,
            timeout=10,
        )
        server, pane = (
            int(found)
            for found in subprocess.run(
                ["tmux", "-L", "tree", "display-message", "-p", "#{pid} #{pane_pid}"],
                env=tmux_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.split()
        )
        deadline = time.time() + 10
        while not (grandchildren := drive_agent.tree(pane)[1:]):
            assert time.time() < deadline, "the pane's sleep never started"
            time.sleep(0.05)
        killed = drive_agent.kill_tmux_server(server)
        assert sorted(killed) == sorted([server, pane, *grandchildren])
        assert all(ended(pid, time.time() + 5) for pid in killed)
        assert bystander.poll() is None, (
            "a process outside the server's tree was killed"
        )
    finally:
        bystander.kill()
        bystander.wait()
        remove_tmux_folder(folder)


def test_kill_tmux_server_leaves_a_pid_that_is_no_longer_a_tmux_server():
    # The server's pid, gone to a new process once the server ended.
    stranger = subprocess.Popen(
        ["sh", "-c", "sleep 300 & wait"], start_new_session=True
    )
    try:
        assert drive_agent.kill_tmux_server(stranger.pid) == []
        assert stranger.poll() is None
    finally:
        os.killpg(stranger.pid, signal.SIGKILL)
        stranger.wait()


def read_until(process, pattern, deadline):
    """Lines of `process`'s stdout up to the first that matches `pattern`, read
    without blocking past `deadline`; fails when none does by then. It reads the
    pipe itself, since a buffered readline may hold the line select waits for."""
    text = ""
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    while time.time() < deadline:
        if not selector.select(timeout=max(0, deadline - time.time())):
            break
        chunk = os.read(process.stdout.fileno(), 4096).decode()
        if not chunk:
            break
        text += chunk
        lines = text.splitlines(keepends=True)
        for n, line in enumerate(lines):
            if line.endswith("\n") and re.search(pattern, line):
                return lines[: n + 1]
    pytest.fail(f"no line matching {pattern!r} by the deadline: {text!r}")


def test_a_wait_for_file_turn_sends_its_text_only_once_the_file_appears(tmp_path, env):
    path = scenario(
        tmp_path,
        turns=[
            "first",
            {"wait_for_file": "confirmed", "say": "I clicked the link"},
            "last",
        ],
    )
    out = tmp_path / "out"
    process = subprocess.Popen(
        [str(TOOL), "run", str(path), "--harness", "claude", "--name", "jason150"]
        + ["--out", str(out)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        lines = read_until(process, r"waits for", time.time() + 30)
        assert str(out / "confirmed") in lines[-1]
        assert sum("waits for" in line for line in lines) == 1
        # The turn before has ended and been logged, and so has the wait, so a run
        # stopped mid-wait says what it waited for; the wait sends nothing.
        log = (out / "turns.md").read_text()
        assert "Sent: first" in log and "I clicked the link" not in log
        assert f"## Turn 2: waiting for {out / 'confirmed'}" in log
        assert process.poll() is None
        (out / "confirmed").touch()
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    log = (out / "turns.md").read_text()
    assert f"Waited for {out / 'confirmed'}" in log
    assert log.index("Sent: first") < log.index("Waited for")
    assert log.index("Waited for") < log.index("Sent: I clicked the link")
    assert log.index("Sent: I clicked the link") < log.index("Sent: last")
    assert "done: I clicked the link" in (out / "screen-02.txt").read_text()
    assert json.loads((out / "run.json").read_text())["outcome"] == "done"


def test_a_wait_for_file_past_its_limit_fails_the_run(tmp_path, env):
    path = scenario(
        tmp_path, turns=["first", {"wait_for_file": "never", "say": "never sent"}]
    )
    out = tmp_path / "out"
    done = drive(
        {**env, "DRIVE_AGENT_WAIT_LIMIT": "1"},
        "run",
        path,
        "--harness",
        "claude",
        "--name",
        "jason151",
        "--out",
        out,
        check=False,
    )
    assert done.returncode == 1
    assert json.loads((out / "run.json").read_text())["outcome"] == "no-file"
    log = (out / "turns.md").read_text()
    assert (
        f"NO FILE SEEN: {out / 'never'}" in log
        and "Sent:" not in log.split("## Turn 2")[1]
    )
    assert str(out / "never") in done.stderr
    assert not (Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason151").exists()


@pytest.mark.parametrize(
    "turn, why",
    [
        ({"wait_for_file": "a/b", "say": "x"}, "file name"),
        ({"wait_for_file": "..", "say": "x"}, "file name"),
        ({"wait_for_file": "", "say": "x"}, "file name"),
        ({"wait_for_file": "turns.md", "say": "x"}, "drive-agent writes"),
        ({"wait_for_file": "screen-03.txt", "say": "x"}, "drive-agent writes"),
        ({"wait_for_file": "files", "say": "x"}, "drive-agent writes"),
        ({"wait_for_file": "ok"}, "say"),
        ({"wait_for_file": "ok", "say": 3}, "say"),
        ({"wait_for_file": "ok", "say": "x", "only_if": "y"}, "only"),
        ({"wait_for_file": "ok", "say": "again"}, "turn 1 waits for ok too"),
    ],
)
def test_a_wait_for_file_turn_drive_agent_cant_take_is_refused_before_the_run(
    tmp_path, env, turn, why
):
    out = tmp_path / "out"
    done = drive(
        env,
        "run",
        scenario(tmp_path, turns=[{"wait_for_file": "ok", "say": "x"}, "y", turn]),
        "--harness",
        "claude",
        "--name",
        "jason152",
        "--out",
        out,
        check=False,
    )
    assert done.returncode == 2 and why in done.stderr, done.stderr
    assert not out.exists()


def test_a_wait_for_file_turn_in_print_mode_is_refused(tmp_path, env):
    done = drive(
        env,
        "run",
        scenario(
            tmp_path,
            turns=[{"wait_for_file": "ok", "say": "x"}],
            print_mode=True,
        ),
        "--harness",
        "claude",
        "--name",
        "jason153",
        "--out",
        tmp_path / "out",
        check=False,
    )
    assert done.returncode == 2 and "print_mode" in done.stderr, done.stderr


HEADER_SETUP = (
    "#!/usr/bin/env python3\nimport os\n"
    "d = os.path.join(os.environ['HOME'], '.config', 'tool')\n"
    "os.makedirs(d)\n"
    "open(os.path.join(d, 'site.header'), 'w').write("
    "'Authorization: Bearer ' + os.environ['TOKEN'] + '\\n')\n"
)


@pytest.mark.parametrize("ready", [True, False], ids=["done", "not-ready"])
def test_keep_file_copies_a_home_file_whole_before_cleanup_and_prints_none_of_it(
    tmp_path, env, ready
):
    token = "fdt_" + "q" * 52
    setup = tmp_path / "setup.py"
    setup.write_text(
        HEADER_SETUP
        + (
            ""
            if ready
            else "open(os.path.join(os.environ['HOME'], 'never-ready'), 'w')\n"
        )
    )
    out = tmp_path / "out"
    kept = tmp_path / "kept" / "site.header"
    kept.parent.mkdir()
    done = drive(
        {**env, "TOKEN": token, "DRIVE_AGENT_LAUNCH_LIMIT": "3"},
        "run",
        scenario(tmp_path, turns=["hi"]),
        "--harness",
        "claude",
        "--name",
        "jason154",
        "--out",
        out,
        "--setup",
        setup,
        "--setup-env",
        "TOKEN",
        "--keep-file",
        f".config/tool/site.header:{kept}",
        check=False,
    )
    assert done.returncode == (0 if ready else 1), done.stderr
    assert kept.read_text() == f"Authorization: Bearer {token}\n"
    assert kept.stat().st_mode & 0o777 == 0o600
    assert f"kept ~/.config/tool/site.header at {kept}" in done.stdout
    assert token not in done.stdout + done.stderr
    for saved in out.rglob("*"):
        if saved.is_file():
            assert token not in saved.read_text(errors="replace"), saved
    assert not (Path(env["DRIVE_AGENT_HOME_ROOT"]) / "jason154").exists()


@pytest.mark.parametrize("linked", ["file", "folder"])
def test_keep_file_never_follows_a_link_the_agent_planted_in_the_home(
    tmp_path, env, linked
):
    """The copy is read outside the box, where a link could reach a file the box
    hides from the agent."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "site.header").write_text("hidden-from-the-box\n")
    target = outside / "site.header" if linked == "file" else outside
    link = ".config/tool/site.header" if linked == "file" else ".config/tool"
    setup = tmp_path / "setup.py"
    setup.write_text(
        "#!/usr/bin/env python3\nimport os\n"
        f"p = os.path.join(os.environ['HOME'], {link!r})\n"
        "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
        f"os.symlink({str(target)!r}, p)\n"
    )
    kept = tmp_path / "kept.header"
    done = drive(
        env,
        "run",
        scenario(tmp_path, turns=["hi"]),
        "--harness",
        "claude",
        "--name",
        "jason157",
        "--out",
        tmp_path / "out",
        "--setup",
        setup,
        "--keep-file",
        f".config/tool/site.header:{kept}",
    )
    assert "kept no ~/.config/tool/site.header: it is a link" in done.stderr
    assert not kept.exists()
    assert "hidden-from-the-box" not in done.stdout + done.stderr


def test_keep_file_with_no_such_file_says_so_and_keeps_nothing(tmp_path, env):
    kept = tmp_path / "site.header"
    done = drive(
        env,
        "run",
        scenario(tmp_path, turns=["hi"]),
        "--harness",
        "claude",
        "--name",
        "jason155",
        "--out",
        tmp_path / "out",
        "--keep-file",
        f".config/tool/site.header:{kept}",
    )
    assert "kept no ~/.config/tool/site.header" in done.stderr
    assert not kept.exists()


@pytest.mark.parametrize(
    "spec, why",
    [
        ("site.header", "REL:DEST"),
        ("/etc/passwd:{dest}", "relative"),
        ("../x:{dest}", "relative"),
        ("a/../../x:{dest}", "relative"),
        (":{dest}", "REL:DEST"),
        ("a:", "REL:DEST"),
        ("a:{exists}", "exists"),
        ("a:{nowhere}", "no folder"),
    ],
)
def test_keep_file_it_cant_take_is_refused_before_the_run(tmp_path, env, spec, why):
    exists = tmp_path / "exists"
    exists.touch()
    spec = spec.format(
        dest=tmp_path / "dest",
        exists=exists,
        nowhere=tmp_path / "no" / "dest",
    )
    out = tmp_path / "out"
    done = drive(
        env,
        "run",
        scenario(tmp_path, turns=["hi"]),
        "--harness",
        "claude",
        "--name",
        "jason156",
        "--out",
        out,
        "--keep-file",
        spec,
        check=False,
    )
    assert done.returncode == 2 and why in done.stderr, done.stderr
    assert not out.exists()
