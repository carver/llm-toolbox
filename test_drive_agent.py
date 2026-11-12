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
passwordless sudo and bwrap are there.
"""

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent / "bin" / "drive-agent"

# A fake agent's reply to a typed line: "ask" makes it ask to post, "fetch HOST" makes
# it CONNECT to HOST through $HTTPS_PROXY and report the status line, anything else
# gets "done: <line>". Every turn writes ~/seen.json: user, env, cwd.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, socket, subprocess, sys, time, uuid
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
    def turn_end(line, text):
        settings = json.load(open(os.path.join(os.environ["HOME"], ".claude", "settings.json")))
        for hook in settings["hooks"]["Stop"][0]["hooks"]:
            subprocess.run(hook["command"], shell=True)
else:
    sessions = os.path.join(os.environ["CODEX_HOME"], "sessions")
    os.makedirs(sessions, exist_ok=True)
    transcript = os.path.join(sessions, "rollout-" + session + ".jsonl")
    notify = [json.loads(a.split("=", 1)[1]) for a in args if a.startswith("notify=")]
    def record(line, text):
        with open(transcript, "a") as f:
            for role, t in (("user", line), ("assistant", text)):
                f.write(json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": role,
                    "content": [{"type": "text", "text": t}]}}) + "\n")
    def turn_end(line, text):
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
    """The driver's environment: fakes first on PATH, no isolation, fast waits."""
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
    )
    return e


def scenario(tmp_path, **keys):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"gated": ["posting"], **keys}))
    return path


def drive(env, *args, check=True):
    done = subprocess.run(
        [str(TOOL), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
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
    assert agent_env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-access"
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


def test_print_mode_sends_the_first_turn_once(tmp_path, env):
    path = scenario(tmp_path, turns=["just this"], print_mode=True)
    out = tmp_path / "out"
    drive(env, "run", path, "--harness", "codex", "--name", "jason12", "--out", out)
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
    # tmp_path is under /home here, which the box hides: the fake gets bound into
    # the run's ~/.local/bin, as Claude Code's own binary is.
    name = f"datest{os.getpid() % 100000}"
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
    subprocess.run(
        ["sudo", "-n", "tee", secret],
        input="export WORKSPACE_ONLY=1\n",
        text=True,
        capture_output=True,
        check=True,
    )
    e = {**env, "HTTPS_PROXY": upstream.url, "FAKE_HIDDEN": secret}
    del e["DRIVE_AGENT_ISOLATION"], e["DRIVE_AGENT_HOME_ROOT"]
    try:
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
        subprocess.run(["sudo", "-n", "rm", "-f", secret], check=False)
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
    assert [p for p in mount_points if p.startswith("/run/user")] == [
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
