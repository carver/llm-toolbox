"""session-scan has a model read every chunk of a session's digest, with claude-fresh faked."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "session-scan"
CATEGORIES = [
    "Mistakes",
    "Waste",
    "Repetition",
    "Missing context",
    "Hungry tools",
    "Skipped checks",
]

# Replies with one Waste finding at the chunk's first numbered line, unless FAKE_REPLY
# gives the findings, with "$FIRST"/"$LAST" standing for the chunk's first and last
# numbered lines. Fails every time a prompt holds FAKE_FAIL_ON, and the first time for
# each prompt when FAKE_FAIL_ONCE is set; FAKE_GARBAGE_ONCE answers the first time with
# text that isn't JSON. FAKE_SLEEP holds each call that long first. Logs each call, with
# its pid, to FAKE_LOG.
FAKE = r"""#!/usr/bin/env python3
import hashlib, json, os, re, sys, time
from pathlib import Path

prompt = sys.stdin.read()
log = Path(os.environ["FAKE_LOG"])
log.mkdir(exist_ok=True)
key = hashlib.sha1(prompt.encode()).hexdigest()[:12]
attempt = len(list(log.glob(f"{key}-*.json")))
record = {"argv": sys.argv[1:], "stdin": prompt, "pid": os.getpid()}
part = log / f".{key}-{attempt}.part"
part.write_text(json.dumps(record))
part.replace(log / f"{key}-{attempt}.json")  # whole, for a test reading meanwhile
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
fail_on = os.environ.get("FAKE_FAIL_ON")
if (fail_on and fail_on in prompt) or (os.environ.get("FAKE_FAIL_ONCE") and attempt == 0):
    print("claude-fresh: rate limited", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAKE_GARBAGE_ONCE") and attempt == 0:
    print("Sure! Here are the findings.")
    sys.exit(0)
timeline = prompt.split("Timeline lines", 1)[1]
numbered = [int(n) for n in re.findall(r"^ *(\d+)  ", timeline, re.M)]
reply = os.environ.get(
    "FAKE_REPLY",
    '[{"category": "Waste", "agent": "main", "first_line": "$FIRST",'
    ' "last_line": "$FIRST", "finding": "Read big.log whole"}]',
)
reply = reply.replace('"$FIRST"', str(numbered[0])).replace('"$LAST"', str(numbered[-1]))
print(json.dumps({"findings": json.loads(reply)}))
"""


def assistant(request: str, *blocks: dict) -> dict:
    return {
        "type": "assistant",
        "requestId": request,
        "message": {"content": list(blocks), "usage": {"output_tokens": 1}},
    }


def user(content: str | list, injected: bool = False) -> dict:
    return {"type": "user", "isMeta": injected, "message": {"content": content}}


def call(tid: str, command: str, output: str = "ok") -> list[dict]:
    """A Bash call and its result."""
    use = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
    done = {"type": "tool_result", "tool_use_id": tid, "content": output}
    return [assistant(f"r-{tid}", use), user([done])]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def session(config: Path, main: list[dict], **subagents: list[dict]) -> None:
    """Session s1 of /work/repo, with subagents by name (agent-<name>)."""
    project = config / "projects" / "-work-repo"
    write_jsonl(project / "s1.jsonl", main)
    for name, records in subagents.items():
        folder = project / "s1" / "subagents"
        write_jsonl(folder / f"agent-{name}.jsonl", records)
        meta = {"agentType": "general-purpose", "description": f"Task {name}"}
        (folder / f"agent-{name}.meta.json").write_text(json.dumps(meta))


def busy(name: str, calls: int) -> list[dict]:
    records = [user(f"Brief for {name}")]
    for n in range(calls):
        records += call(f"{name}{n}", f"step {n} of {name}")
    return records


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "claude-fresh"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "FAKE_LOG": str(tmp_path / "calls"),
        "SESSION_SCAN_BACKOFF": "0",
    }


def scan(env: dict, out: Path, *args: str, **fake: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), "--cwd", "/work/repo", "--out", str(out), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**env, **fake},
    )


def calls(env: dict) -> dict[str, dict]:
    """What the fake was given, by its log file's name, one per call."""
    log = Path(env["FAKE_LOG"])
    return {p.name: json.loads(p.read_text()) for p in log.glob("*.json")}


def candidates(out: Path) -> list[dict]:
    text = (out / "candidates.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def digest_lines(out: Path) -> list[str]:
    return (out / "digest.txt").read_text().splitlines()


def line_of(out: Path, text: str) -> int:
    return digest_lines(out).index(text) + 1


def chunk_bodies(out: Path) -> list[str]:
    """Each chunk's numbered lines, without the context its reader got before them."""
    return [
        p.read_text().split("Timeline lines", 1)[1].split("\n", 1)[1]
        for p in sorted(out.glob("chunk-*.txt"))
    ]


def numbered(body: str) -> list[int]:
    return [int(line[:6]) for line in body.splitlines() if line[:6].strip()]


def test_each_candidate_prints_with_its_lines_and_agent(
    env: dict, tmp_path: Path
) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    out = tmp_path / "scan"
    proc = scan(env, out)
    assert proc.returncode == 0, proc.stderr
    first = line_of(out, "== main")
    assert f"L{first}-{first}  main  Waste: Read big.log whole" in proc.stdout
    assert candidates(out) == [
        {
            "chunk": 1,
            "category": "Waste",
            "agent": "main",
            "first_line": first,
            "last_line": first,
            "finding": "Read big.log whole",
        }
    ]


def test_the_reader_gets_the_categories_the_summary_and_numbered_lines(
    env: dict, tmp_path: Path
) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    out = tmp_path / "scan"
    assert scan(env, out).returncode == 0
    [given] = calls(env).values()
    argv = given["argv"]
    assert argv[argv.index("-m") + 1] == "sonnet"
    reader = argv[argv.index("--system-prompt") + 1]
    assert all(category in reader for category in CATEGORIES)
    assert "biggest outputs" in given["stdin"]  # the summary, for context
    lines = digest_lines(out)
    first = line_of(out, "== main")
    for n in range(first, len(lines) + 1):
        assert f"{n:>6}  {lines[n - 1]}" in given["stdin"]


def test_the_model_can_be_chosen(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 1))
    assert scan(env, tmp_path / "scan", "-m", "opus").returncode == 0
    [given] = calls(env).values()
    argv = given["argv"]
    assert argv[argv.index("-m") + 1] == "opus"


def test_chunks_hold_whole_sections_in_order_up_to_the_limit(
    env: dict, tmp_path: Path
) -> None:
    session(
        Path(env["CLAUDE_CONFIG_DIR"]),
        busy("main", 5),
        a=busy("a", 1),
        b=busy("b", 1),
        c=busy("c", 5),
    )
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "600").returncode == 0
    bodies = chunk_bodies(out)
    assert len(bodies) > 1
    assert all(len(body) <= 600 + 1 for body in bodies)
    assert any(body.count("  == ") >= 2 for body in bodies)  # small ones share
    every = [n for body in bodies for n in numbered(body)]
    first = line_of(out, "== main")
    assert every == list(range(first, len(digest_lines(out)) + 1))
    for name in ("agent-a", "agent-b", "agent-c"):
        holding = [body for body in bodies if name in body]
        assert len(holding) == 1, name  # a section that fits isn't cut


def test_a_long_section_is_cut_at_a_user_turn_and_continued(
    env: dict, tmp_path: Path
) -> None:
    records = []
    for turn in range(4):
        records += [user(f"Turn {turn}")]
        for n in range(6):
            records += call(f"t{turn}-{n}", f"work {n} for turn {turn}")
    session(Path(env["CLAUDE_CONFIG_DIR"]), records)
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "1500").returncode == 0
    later = chunk_bodies(out)[1:]
    assert later
    lines = digest_lines(out)
    for body in later:
        header, first = body.splitlines()[:2]
        assert header == f"{'':>6}  == main (continued)"
        assert lines[int(first[:6]) - 1].startswith("USER Turn")


def test_invalid_candidates_are_dropped_and_counted(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    reply = [
        {"category": "Bogus", "first_line": "$FIRST", "last_line": "$FIRST"},
        {"category": "Waste", "first_line": 1, "last_line": "$FIRST"},
        {"category": "Waste", "first_line": "$LAST", "last_line": "$FIRST"},
        {"category": "Waste", "first_line": "$FIRST", "last_line": 99999},
        {"category": "Mistakes", "first_line": "$FIRST", "last_line": "$LAST"},
    ]
    for r in reply:
        r |= {"agent": "main", "finding": f"{r['category']} finding"}
    proc = scan(env, tmp_path / "scan", FAKE_REPLY=json.dumps(reply))
    assert proc.returncode == 0, proc.stderr
    printed = [line for line in proc.stdout.splitlines() if line.startswith("L")]
    assert len(printed) == 1 and "Mistakes: Mistakes finding" in printed[0]
    assert "4 dropped" in proc.stdout.splitlines()[-1]


def test_a_candidate_over_150_lines_is_marked_wide(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 100))
    reply = [
        {
            "category": "Repetition",
            "agent": "main",
            "first_line": "$FIRST",
            "last_line": "$LAST",
            "finding": "Ran the steps one by one",
        }
    ]
    proc = scan(env, tmp_path / "scan", FAKE_REPLY=json.dumps(reply))
    assert "Repetition: Ran the steps one by one (wide)" in proc.stdout


def test_a_failed_chunk_is_retried_once(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    proc = scan(env, tmp_path / "scan", FAKE_FAIL_ONCE="1")
    assert proc.returncode == 0, proc.stderr
    assert len(calls(env)) == 2
    assert "Waste: Read big.log whole" in proc.stdout


def test_an_answer_that_isnt_json_is_retried(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    proc = scan(env, tmp_path / "scan", FAKE_GARBAGE_ONCE="1")
    assert proc.returncode == 0, proc.stderr
    assert len(calls(env)) == 2
    assert "Waste: Read big.log whole" in proc.stdout


def tokens(stdout: str) -> int:
    return int(stdout.split("~")[-1].split(" input tokens")[0])


def test_the_token_estimate_counts_each_attempt(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    once = scan(env, tmp_path / "once")
    # A log of its own, so the fake counts this run's first attempt afresh.
    twice = scan(
        env, tmp_path / "twice", FAKE_FAIL_ONCE="1", FAKE_LOG=str(tmp_path / "log2")
    )
    # Twice the characters, so twice the tokens, give or take the rounding.
    assert abs(tokens(twice.stdout) - 2 * tokens(once.stdout)) <= 1
    assert tokens(once.stdout) > 0


def two_chunk_session(env: dict) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 5), a=busy("a", 5))


def test_a_chunk_that_fails_twice_exits_3_and_the_rest_still_print(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    proc = scan(env, out, "--chunk-chars", "500", FAKE_FAIL_ON="Brief for a")
    assert proc.returncode == 3
    failed = [n for n, body in enumerate(chunk_bodies(out), 1) if "Brief for a" in body]
    assert len(failed) == 1
    assert f"chunk {failed[0]}" in proc.stderr
    assert "rate limited" in proc.stderr
    assert "--chunks" in proc.stderr
    assert "main  Waste: Read big.log whole" in proc.stdout
    assert f"chunk {failed[0]:02d}" in proc.stdout and "FAILED" in proc.stdout


def test_chunks_rereads_only_those_and_keeps_the_digest(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    scan(env, out, "--chunk-chars", "500", FAKE_FAIL_ON="Brief for a")
    failed = next(n for n, b in enumerate(chunk_bodies(out), 1) if "Brief for a" in b)
    digest = (out / "digest.txt").read_text()
    before = set(calls(env))
    # The session grows meanwhile; the reread keeps the digest its lines point into.
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 9), a=busy("a", 5))
    proc = scan(env, out, "--chunks", str(failed))
    assert proc.returncode == 0, proc.stderr
    assert (out / "digest.txt").read_text() == digest
    reread = [given for name, given in calls(env).items() if name not in before]
    assert reread and all("Brief for a" in c["stdin"] for c in reread)
    chunks = [c["chunk"] for c in candidates(out)]
    assert sorted(chunks) == sorted(set(chunks)) and failed in chunks


def test_a_reread_replaces_the_chunks_earlier_candidates(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "500").returncode == 0
    first_run = candidates(out)
    assert len(first_run) == 2
    proc = scan(env, out, "--chunks", "1")
    assert proc.returncode == 0, proc.stderr
    assert sorted(c["chunk"] for c in candidates(out)) == [1, 2]


def test_chunks_without_an_earlier_run_is_a_usage_error(
    env: dict, tmp_path: Path
) -> None:
    proc = scan(env, tmp_path / "empty", "--chunks", "1")
    assert proc.returncode == 2
    assert "no earlier run" in proc.stderr


def test_an_unknown_session_exits_1(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 1))
    proc = scan(env, tmp_path / "scan", "nope")
    assert proc.returncode == 1
    assert calls(env) == {}


def test_calls_repeated_across_agents_are_listed(env: dict, tmp_path: Path) -> None:
    shared = "cat docs/design.md"
    session(
        Path(env["CLAUDE_CONFIG_DIR"]),
        [user("Go"), *call("m1", shared), *call("m2", "pytest"), *call("m3", "pytest")]
        + call("m4", "pytest"),
        a=[user("Brief a"), *call("a1", shared)],
        b=[user("Brief b"), *call("b1", shared)],
    )
    out = tmp_path / "scan"
    proc = scan(env, out)
    repeats = proc.stdout.split("repeated across agents")[1].split("\n\n")[0]
    lines = ", ".join(
        f"L{n}"
        for n, text in enumerate(digest_lines(out), 1)
        if text == f"  > Bash: {shared}"
    )
    assert f"3x in 3 agents  Bash: {shared}  ({lines})" in repeats
    assert "pytest" not in repeats  # one agent's own repeats are its reader's to see


def test_repeated_calls_match_whatever_their_numbers(env: dict, tmp_path: Path) -> None:
    session(
        Path(env["CLAUDE_CONFIG_DIR"]),
        [user("Go"), *call("m1", "waitone --pid 4411 -t 170")],
        a=[user("Brief a"), *call("a1", "waitone --pid 902 -t 165")],
        b=[user("Brief b"), *call("b1", "waitone --pid 17 -t 5")],
    )
    proc = scan(env, tmp_path / "scan")
    repeats = proc.stdout.split("repeated across agents")[1]
    # Shown as the first of them was made.
    assert "3x in 3 agents  Bash: waitone --pid 4411 -t 170  (" in repeats


def test_at_most_ten_repeated_calls_are_listed(env: dict, tmp_path: Path) -> None:
    def shared(agent: str) -> list[dict]:
        return [c for n in range(12) for c in call(f"{agent}{n}", f"cat file{'x' * n}")]

    session(
        Path(env["CLAUDE_CONFIG_DIR"]),
        [user("Go"), *shared("m")],
        a=[user("Brief a"), *shared("a")],
        b=[user("Brief b"), *shared("b")],
    )
    proc = scan(env, tmp_path / "scan")
    repeats = proc.stdout.split("repeated across agents")[1].strip().split("\n\n")[0]
    assert len(repeats.splitlines()) == 10


def test_each_chunk_gets_a_line_with_its_candidate_count(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    proc = scan(env, out, "--chunk-chars", "500", FAKE_REPLY="[]")
    assert proc.returncode == 0, proc.stderr
    rows = [line for line in proc.stdout.splitlines() if line.startswith("chunk ")]
    assert len(rows) == len(chunk_bodies(out)) >= 2
    assert all("0 candidates" in row for row in rows)


def test_a_candidates_agent_is_the_section_its_first_line_is_in(
    env: dict, tmp_path: Path
) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 2), a=busy("a", 2))
    reply = [
        {
            "category": "Waste",
            "agent": "multiple agents",
            "first_line": "$LAST",
            "last_line": "$LAST",
            "finding": "Ran step 1 of a",
        }
    ]
    proc = scan(env, tmp_path / "scan", FAKE_REPLY=json.dumps(reply))
    assert "  agent-a  Waste: Ran step 1 of a" in proc.stdout


def test_a_long_section_is_not_cut_at_an_early_turn(env: dict, tmp_path: Path) -> None:
    records = [user("Turn 0")]
    for n in range(30):
        records += call(f"t{n}", f"work {n} for the only turn")
    session(Path(env["CLAUDE_CONFIG_DIR"]), records)
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "1000").returncode == 0
    bodies = chunk_bodies(out)
    assert len(bodies) > 2
    assert all(len(body) > 500 for body in bodies[:-1])  # each at least half full


def test_a_long_section_is_not_cut_at_an_injected_turn(
    env: dict, tmp_path: Path
) -> None:
    # Each turn is followed by injected text, which a cut must not take for the turn.
    records = []
    for turn in range(8):
        records += [user(f"Turn {turn}"), user("<skill text>", injected=True)]
        for n in range(4):
            records += call(f"t{turn}-{n}", f"work {n} for turn {turn}")
    session(Path(env["CLAUDE_CONFIG_DIR"]), records)
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "1000").returncode == 0
    lines = digest_lines(out)
    later = chunk_bodies(out)[1:]
    assert later
    for body in later:
        assert lines[numbered(body)[0] - 1].startswith("USER Turn")


def test_each_chunk_gets_the_users_turns_for_context(env: dict, tmp_path: Path) -> None:
    main = [user("Keep the commit checks light, I approve that"), *busy("main", 5)[1:]]
    session(Path(env["CLAUDE_CONFIG_DIR"]), main, a=busy("a", 5))
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "500").returncode == 0
    prompts = [given["stdin"] for given in calls(env).values()]
    assert len(prompts) >= 2
    assert all("Keep the commit checks light, I approve that" in p for p in prompts)


def test_a_stopped_scan_stops_its_readers_and_can_be_resumed(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    proc = subprocess.Popen(
        [str(SCRIPT), "--cwd", "/work/repo", "--out", str(out), "--chunk-chars", "500"],
        env={**env, "FAKE_SLEEP": "60"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while len(calls(env)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        readers = [given["pid"] for given in calls(env).values()]
        assert len(readers) == 2
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 128 + signal.SIGTERM
        deadline = time.monotonic() + 30
        while any(alive(pid) for pid in readers) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(alive(pid) for pid in readers)
    finally:
        proc.kill()
        for given in calls(env).values():
            try:
                os.kill(given["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    resumed = scan(env, out, "--chunks", "1,2")
    assert resumed.returncode == 0, resumed.stderr
    assert sorted(c["chunk"] for c in candidates(out)) == [1, 2]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (ProcessLookupError, FileNotFoundError):
        return False
    return stat.split(")")[1].split()[0] != "Z"  # a zombie has stopped


def test_candidates_print_and_save_in_digest_line_order(
    env: dict, tmp_path: Path
) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", 3))
    reply = [
        {"category": "Waste", "first_line": "$LAST", "last_line": "$LAST"},
        {"category": "Waste", "first_line": "$FIRST", "last_line": "$FIRST"},
    ]
    for n, r in enumerate(reply):
        r["finding"] = f"finding {n}"
    out = tmp_path / "scan"
    proc = scan(env, out, FAKE_REPLY=json.dumps(reply))
    printed = [line for line in proc.stdout.splitlines() if line.startswith("L")]
    assert [line.split(": ")[-1] for line in printed] == ["finding 1", "finding 0"]
    assert [c["finding"] for c in candidates(out)] == ["finding 1", "finding 0"]
