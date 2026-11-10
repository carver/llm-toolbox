"""session-scan has a model read every chunk of a session's digest, with claude-fresh faked."""

import json
import os
import re
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
system = sys.argv[sys.argv.index("--system-prompt") + 1]
if system.startswith("You check"):
    # A verifier: each candidate in the batch real, unless FAKE_VERDICTS (id ->
    # verdict) says otherwise; FAKE_VERDICT_SKIP ids left out; FAKE_VERDICT_EXTRA added.
    if os.environ.get("FAKE_VERIFY_FAIL"):
        print("claude-fresh: rate limited", file=sys.stderr)
        sys.exit(1)
    batch = prompt.split("Candidates to check", 1)[1]
    ids = re.findall(r"^\[(\d+\.\d+)\]", batch, re.M)
    given = json.loads(os.environ.get("FAKE_VERDICTS", "{}"))
    skip = os.environ.get("FAKE_VERDICT_SKIP", "").split(",")
    verdicts = [
        {"id": i, "verdict": given.get(i, "real"), "reason": f"checked {i}"}
        for i in ids
        if i not in skip
    ]
    verdicts += json.loads(os.environ.get("FAKE_VERDICT_EXTRA", "[]"))
    print(json.dumps({"verdicts": verdicts}))
    sys.exit(0)
if system.startswith("You group"):
    # The merger: FAKE_FINDINGS, or one finding of every real id.
    # FAKE_MERGE_FAIL_AFTER_FIRST: every merge call after the first fails (this
    # call's own log is already written, so a later call sees more than one).
    merges = [p for p in log.glob("*.json") if "You group" in p.read_text()]
    if os.environ.get("FAKE_MERGE_FAIL") or (
        os.environ.get("FAKE_MERGE_FAIL_AFTER_FIRST") and len(merges) > 1
    ):
        print("claude-fresh: rate limited", file=sys.stderr)
        sys.exit(1)
    real = re.findall(r"^(\d+\.\d+) real ", prompt, re.M)
    default = [{"title": "One habit", "summary": "It cost time.", "ids": real}]
    found = json.loads(os.environ.get("FAKE_FINDINGS", "null")) or default
    if attempt == 0 and os.environ.get("FAKE_FINDINGS_FIRST"):
        found = json.loads(os.environ["FAKE_FINDINGS_FIRST"])
    print(json.dumps({"findings": found}))
    sys.exit(0)
timeline = prompt.split("Timeline lines", 1)[1]
numbered = [int(n) for n in re.findall(r"^ *(\d+)  ", timeline, re.M)]
if os.environ.get("FAKE_REPLY_EACH"):
    # A Waste finding at every numbered line.
    each = [
        {"category": "Waste", "first_line": n, "last_line": n, "finding": f"at {n}"}
        for n in numbered
    ]
    print(json.dumps({"findings": each}))
    sys.exit(0)
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


class Env(dict):
    """An environment that shows only its keys, so a failing assertion that names it
    prints no secret."""

    def __repr__(self) -> str:
        return f"Env({sorted(self)})"


@pytest.fixture(autouse=True)
def outside_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """session-digest, which session-scan runs, reads $CLAUDE_CODE_SESSION_ID's log when
    given no session, so no test inherits the id of the session that runs the suite."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "claude-fresh"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    return Env(
        {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "FAKE_LOG": str(tmp_path / "calls"),
            "SESSION_SCAN_BACKOFF": "0",
        }
    )


def scan(
    env: dict, out: Path, *args: str, verify: bool = False, **fake: str
) -> subprocess.CompletedProcess:
    """A run of session-scan; the readers' candidates only, unless verify."""
    stage = [] if verify or "--verify-only" in args else ["--candidates-only"]
    return subprocess.run(
        [str(SCRIPT), "--cwd", "/work/repo", "--out", str(out), *stage, *args],
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
            "id": "1.1",
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
    # No tails: the sizes this test packs are those of the lines alone.
    assert scan(env, out, "--chunk-chars", "600", "--no-tails").returncode == 0
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
        [
            str(SCRIPT),
            "--cwd",
            "/work/repo",
            "--out",
            str(out),
            "--chunk-chars",
            "500",
            "--candidates-only",
        ],
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


def test_the_digest_holds_whole_messages_and_output_tails(
    env: dict, tmp_path: Path
) -> None:
    long = "My guess is the margin is too tight. " * 20
    main = [user("Go"), assistant("r1", {"type": "text", "text": long})]
    main += call("t1", "pytest", "....\n1 failed, 40 passed")
    session(Path(env["CLAUDE_CONFIG_DIR"]), main)
    out = tmp_path / "scan"
    assert scan(env, out).returncode == 0
    lines = digest_lines(out)
    assert f"  {long.strip()}" in lines
    assert "    | 1 failed, 40 passed" in lines


def test_no_tails_leaves_the_outputs_out(env: dict, tmp_path: Path) -> None:
    session(Path(env["CLAUDE_CONFIG_DIR"]), [user("Go"), *call("t1", "pytest", "1 ok")])
    out = tmp_path / "scan"
    assert scan(env, out, "--no-tails").returncode == 0
    assert not any(line.startswith("    | ") for line in digest_lines(out))


# Verification


def role_calls(env: dict, role: str) -> list[dict]:
    """The fake's calls whose system prompt starts with role's opening words."""
    opening = {"reader": "You read", "verifier": "You check", "merger": "You group"}
    found = []
    for given in calls(env).values():
        argv = given["argv"]
        if argv[argv.index("--system-prompt") + 1].startswith(opening[role]):
            found.append(given)
    return found


def batch_ids(given: dict) -> list[str]:
    batch = given["stdin"].split("Candidates to check", 1)[1]
    return re.findall(r"^\[(\d+\.\d+)\]", batch, re.MULTILINE)


def verdicts(out: Path) -> dict[str, dict]:
    text = (out / "verdicts.jsonl").read_text()
    return {v["id"]: v for v in map(json.loads, text.splitlines())}


def many(env: dict, calls_: int = 10) -> None:
    """A main session whose every digest line the fake reader makes a candidate of."""
    session(Path(env["CLAUDE_CONFIG_DIR"]), busy("main", calls_))


def test_candidates_get_ids_by_chunk(env: dict, tmp_path: Path) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    assert scan(env, out, "--chunk-chars", "500", FAKE_REPLY_EACH="1").returncode == 0
    ids = [c["id"] for c in candidates(out)]
    chunk_of = {c["id"]: c["chunk"] for c in candidates(out)}
    assert ids[0] == "1.1"
    for chunk in set(chunk_of.values()):
        mine = [i for i in ids if chunk_of[i] == chunk]
        assert mine == [f"{chunk}.{n}" for n in range(1, len(mine) + 1)]


def test_candidates_are_verified_in_batches_of_15_in_line_order(
    env: dict, tmp_path: Path
) -> None:
    many(env)
    out = tmp_path / "scan"
    proc = scan(env, out, "-j", "1", verify=True, FAKE_REPLY_EACH="1")
    assert proc.returncode == 0, proc.stderr
    every = [c["id"] for c in candidates(out)]
    assert len(every) > 15
    batches = role_calls(env, "verifier")
    expected = [every[i : i + 15] for i in range(0, len(every), 15)]
    assert sorted(map(batch_ids, batches)) == sorted(expected)
    for given in batches:
        assert "--tools" not in given["argv"]
        index = given["stdin"].split("Candidates to check", 1)[0]
        assert all(f"{i}  " in index for i in every)  # every candidate, for context


def test_a_candidate_comes_with_its_lines_and_ten_either_side(
    env: dict, tmp_path: Path
) -> None:
    many(env, 30)
    out = tmp_path / "scan"
    reply = [{"category": "Waste", "first_line": 40, "last_line": 41, "finding": "x"}]
    proc = scan(env, out, verify=True, FAKE_REPLY=json.dumps(reply))
    assert proc.returncode == 0, proc.stderr
    [given] = role_calls(env, "verifier")
    batch = given["stdin"].split("Candidates to check", 1)[1]
    lines = digest_lines(out)
    numbers = [int(n) for n in re.findall(r"^ *(\d+)  ", batch, re.MULTILINE)]
    assert numbers == list(range(30, 52))
    assert f"{40:>6}  {lines[39]}" in batch


def test_a_wide_candidate_comes_with_its_first_and_last_hundred_lines(
    env: dict, tmp_path: Path
) -> None:
    many(env, 150)
    out = tmp_path / "scan"
    reply = [
        {
            "category": "Waste",
            "first_line": "$FIRST",
            "last_line": "$LAST",
            "finding": "x",
        }
    ]
    assert scan(env, out, verify=True, FAKE_REPLY=json.dumps(reply)).returncode == 0
    [given] = role_calls(env, "verifier")
    batch = given["stdin"].split("Candidates to check", 1)[1]
    numbers = [int(n) for n in re.findall(r"^ *(\d+)  ", batch, re.MULTILINE)]
    [candidate] = candidates(out)
    first, last = candidate["first_line"], candidate["last_line"]
    assert numbers == [*range(first, first + 100), *range(last - 99, last + 1)]
    assert "lines left out" in batch


def test_verified_findings_print_with_their_candidates(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    proc = scan(env, out, verify=True, FAKE_REPLY_EACH="1")
    assert proc.returncode == 0, proc.stderr
    report = proc.stdout.splitlines()
    assert report[0] == "1. One habit: It cost time."
    shown = [line.strip() for line in report[1 : 1 + len(candidates(out))]]
    assert shown == [line_of_candidate(c) for c in candidates(out)]
    found = json.loads((out / "findings.json").read_text())
    assert found == [
        {
            "title": "One habit",
            "summary": "It cost time.",
            "ids": [c["id"] for c in candidates(out)],
        }
    ]


def line_of_candidate(c: dict) -> str:
    return f"L{c['first_line']}-{c['last_line']}  {c['agent']}  {c['category']}: {c['finding']}"


def test_dismissed_and_unclear_candidates_print_with_their_reasons(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICTS=json.dumps({"1.1": "trivial", "1.2": "false", "1.3": "unclear"}),
    )
    assert proc.returncode == 0, proc.stderr
    by_id = {c["id"]: line_of_candidate(c) for c in candidates(out)}
    unclear = proc.stdout.split("\nunclear\n")[1].split("\n\n")[0]
    assert f"{by_id['1.3']}  [unclear: checked 1.3]" in unclear
    dismissed = proc.stdout.split("\ndismissed\n")[1].split("\n\n")[0]
    assert f"{by_id['1.1']}  [trivial: checked 1.1]" in dismissed
    assert f"{by_id['1.2']}  [false: checked 1.2]" in dismissed
    assert verdicts(out)["1.1"] == {
        "id": "1.1",
        "verdict": "trivial",
        "reason": "checked 1.1",
    }


def test_a_candidate_with_no_verdict_is_unverified(env: dict, tmp_path: Path) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    extra = [{"id": "9.9", "verdict": "false", "reason": "not in this batch"}]
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICT_SKIP="1.2",
        FAKE_VERDICT_EXTRA=json.dumps(extra),
    )
    assert proc.returncode == 4
    assert "--verify-only" in proc.stderr
    unverified = proc.stdout.split("\nunverified\n")[1].split("\n\n")[0]
    assert line_of_candidate(candidates(out)[1]) in unverified
    assert "9.9" not in verdicts(out)
    assert verdicts(out)["1.2"]["verdict"] == "unverified"


def test_a_batch_that_fails_twice_stops_the_rest(env: dict, tmp_path: Path) -> None:
    many(env)
    out = tmp_path / "scan"
    proc = scan(
        env, out, "-j", "1", verify=True, FAKE_REPLY_EACH="1", FAKE_VERIFY_FAIL="1"
    )
    assert proc.returncode == 4
    assert len(role_calls(env, "verifier")) == 2  # the first batch, tried twice
    assert {v["verdict"] for v in verdicts(out).values()} == {"unverified"}
    assert "rate limited" in proc.stderr


def test_the_merge_is_checked_against_the_verdicts(env: dict, tmp_path: Path) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    findings = [
        {"title": "T" * 300, "summary": "S" * 900, "ids": ["1.1", "7.7"]},
        {"title": "Second", "summary": "again", "ids": ["1.1", "1.2"]},
    ]
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_FINDINGS=json.dumps(findings),
    )
    assert proc.returncode == 0, proc.stderr
    found = json.loads((out / "findings.json").read_text())
    every = [c["id"] for c in candidates(out)]
    assert found[0] == {"title": "T" * 100, "summary": "S" * 500, "ids": ["1.1"]}
    assert found[1]["ids"] == ["1.2"]
    by_id = {c["id"]: c for c in candidates(out)}
    rest = [f for f in found[2:]]
    assert [f["ids"] for f in rest] == [[i] for i in every[2:]]
    assert rest[0]["title"] == by_id[every[2]]["finding"]


def test_a_merge_may_take_in_trivial_instances_of_a_habit(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICTS=json.dumps({"1.2": "trivial"}),
        FAKE_FINDINGS=json.dumps(
            [{"title": "Polling", "summary": "Waits.", "ids": ["1.1", "1.2"]}]
        ),
    )
    assert proc.returncode == 0, proc.stderr
    every = candidates(out)
    findings_block = proc.stdout.split("\n\n")[0]
    assert line_of_candidate(every[1]) in findings_block
    assert "\ndismissed\n" not in proc.stdout  # its one trivial candidate is in it


def test_a_failed_merge_makes_each_real_candidate_a_finding(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    proc = scan(env, out, verify=True, FAKE_REPLY_EACH="1", FAKE_MERGE_FAIL="1")
    assert proc.returncode == 0, proc.stderr
    found = json.loads((out / "findings.json").read_text())
    assert [f["ids"] for f in found] == [[c["id"]] for c in candidates(out)]


def test_verify_only_verifies_what_has_no_verdict(env: dict, tmp_path: Path) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    scan(env, out, verify=True, FAKE_REPLY_EACH="1", FAKE_VERDICT_SKIP="1.2")
    before = set(calls(env))
    proc = scan(env, out, "--verify-only")
    assert proc.returncode == 0, proc.stderr
    new = [given for name, given in calls(env).items() if name not in before]
    assert [batch_ids(g) for g in new if g in role_calls(env, "verifier")] == [["1.2"]]
    assert verdicts(out)["1.2"]["verdict"] == "real"
    assert role_calls(env, "reader") and all(
        name in before
        for name, g in calls(env).items()
        if g in role_calls(env, "reader")
    )


def test_reverify_redoes_every_verdict(env: dict, tmp_path: Path) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    scan(env, out, verify=True, FAKE_REPLY_EACH="1")
    proc = scan(
        env,
        out,
        "--verify-only",
        "--reverify",
        FAKE_VERDICTS=json.dumps({"1.1": "false"}),
    )
    assert proc.returncode == 0, proc.stderr
    assert verdicts(out)["1.1"]["verdict"] == "false"


def test_verify_only_needs_an_earlier_run(env: dict, tmp_path: Path) -> None:
    assert scan(env, tmp_path / "empty", "--verify-only").returncode == 2


def test_reverify_needs_verify_only(env: dict, tmp_path: Path) -> None:
    assert scan(env, tmp_path / "scan", "--reverify", verify=True).returncode == 2


def test_rereading_a_chunk_reverifies_its_candidates_and_keeps_the_rest(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    scan(env, out, "--chunk-chars", "500", verify=True, FAKE_REPLY_EACH="1")
    kept = {i: v for i, v in verdicts(out).items() if not i.startswith("1.")}
    before = set(calls(env))
    proc = scan(env, out, "--chunks", "1", verify=True, FAKE_REPLY_EACH="1")
    assert proc.returncode == 0, proc.stderr
    new = [g for name, g in calls(env).items() if name not in before]
    checked = [i for g in new if g in role_calls(env, "verifier") for i in batch_ids(g)]
    assert checked and all(i.startswith("1.") for i in checked)
    assert {i: v for i, v in verdicts(out).items() if not i.startswith("1.")} == kept


def test_an_earlier_run_without_ids_gets_them(env: dict, tmp_path: Path) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    scan(env, out, FAKE_REPLY_EACH="1")
    old = [{k: v for k, v in c.items() if k != "id"} for c in candidates(out)]
    (out / "candidates.jsonl").write_text("".join(json.dumps(c) + "\n" for c in old))
    proc = scan(env, out, "--verify-only")
    assert proc.returncode == 0, proc.stderr
    assert [c["id"] for c in candidates(out)] == [
        f"1.{n}" for n in range(1, len(old) + 1)
    ]


def test_each_batch_gets_a_line_with_its_verdict_counts(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICTS=json.dumps({"1.1": "trivial"}),
    )
    n = len(candidates(out))
    assert f"batch 01  1.1-1.{n}  {n - 1} real, 1 trivial" in proc.stdout


def test_a_fresh_run_into_a_used_folder_verifies_everything(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    scan(env, out, verify=True, FAKE_REPLY_EACH="1")
    before = set(calls(env))
    proc = scan(env, out, verify=True, FAKE_REPLY_EACH="1")
    assert proc.returncode == 0, proc.stderr
    new = [g for name, g in calls(env).items() if name not in before]
    checked = {i for g in new if g in role_calls(env, "verifier") for i in batch_ids(g)}
    assert checked == {c["id"] for c in candidates(out)}


def test_a_reread_chunk_loses_its_verdicts_even_unverified_after(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    scan(env, out, "--chunk-chars", "500", verify=True, FAKE_REPLY_EACH="1")
    scan(env, out, "--chunks", "1", FAKE_REPLY_EACH="1")  # read only, no verifying
    assert not any(i.startswith("1.") for i in verdicts(out))
    before = set(calls(env))
    assert scan(env, out, "--verify-only").returncode == 0
    new = [g for name, g in calls(env).items() if name not in before]
    checked = {i for g in new if g in role_calls(env, "verifier") for i in batch_ids(g)}
    assert checked == {c["id"] for c in candidates(out) if c["chunk"] == 1}


def test_a_failed_chunk_still_reports_failed_verification(
    env: dict, tmp_path: Path
) -> None:
    two_chunk_session(env)
    out = tmp_path / "scan"
    proc = scan(
        env,
        out,
        "--chunk-chars",
        "500",
        verify=True,
        FAKE_FAIL_ON="Brief for a",
        FAKE_VERIFY_FAIL="1",
    )
    assert proc.returncode == 3
    assert "verify batch 1" in proc.stderr and "--verify-only" in proc.stderr


def test_a_merge_keeps_only_real_and_trivial_ids_and_needs_a_real_one(
    env: dict, tmp_path: Path
) -> None:
    many(env, 2)
    out = tmp_path / "scan"
    findings = [
        {"title": "Only trivial", "summary": "s", "ids": ["1.2"]},
        {"title": "Mixed", "summary": "s", "ids": ["1.1", "1.3", "1.4"]},
    ]
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICTS=json.dumps({"1.2": "trivial", "1.3": "false", "1.4": "unclear"}),
        FAKE_FINDINGS=json.dumps(findings),
    )
    assert proc.returncode == 0, proc.stderr
    found = json.loads((out / "findings.json").read_text())
    assert found[0] == {"title": "Mixed", "summary": "s", "ids": ["1.1"]}
    assert all(f["title"] != "Only trivial" for f in found)


def test_a_reason_is_cut_to_300_characters(env: dict, tmp_path: Path) -> None:
    many(env, 1)
    out = tmp_path / "scan"
    long = [{"id": "1.1", "verdict": "trivial", "reason": "r" * 400}]
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_VERDICT_SKIP="1.1",
        FAKE_VERDICT_EXTRA=json.dumps(long),
    )
    assert proc.returncode == 0, proc.stderr
    assert verdicts(out)["1.1"]["reason"] == "r" * 300


@pytest.mark.parametrize(("span", "cut"), [(200, False), (201, True)])
def test_a_candidate_is_cut_only_past_200_lines(
    env: dict, tmp_path: Path, span: int, cut: bool
) -> None:
    many(env, 150)
    out = tmp_path / "scan"
    first = 30
    reply = [
        {
            "category": "Waste",
            "first_line": first,
            "last_line": first + span - 1,
            "finding": "x",
        }
    ]
    assert scan(env, out, verify=True, FAKE_REPLY=json.dumps(reply)).returncode == 0
    [given] = role_calls(env, "verifier")
    assert ("lines left out" in given["stdin"]) == cut


def test_a_merge_that_groups_too_little_is_tried_again(
    env: dict, tmp_path: Path
) -> None:
    many(env, 3)
    out = tmp_path / "scan"
    poor = [{"title": "One", "summary": "s", "ids": ["1.1"]}]
    proc = scan(
        env, out, verify=True, FAKE_REPLY_EACH="1", FAKE_FINDINGS_FIRST=json.dumps(poor)
    )
    assert proc.returncode == 0, proc.stderr
    assert len(role_calls(env, "merger")) == 2
    found = json.loads((out / "findings.json").read_text())
    assert [f["title"] for f in found] == ["One habit"]


def test_the_verifier_and_merger_default_to_opus(env: dict, tmp_path: Path) -> None:
    many(env, 1)
    assert (
        scan(env, tmp_path / "scan", verify=True, FAKE_REPLY_EACH="1").returncode == 0
    )
    for role in ("verifier", "merger"):
        for given in role_calls(env, role):
            argv = given["argv"]
            assert argv[argv.index("-m") + 1] == "opus", role


def test_what_an_agent_set_aside_is_listed(env: dict, tmp_path: Path) -> None:
    main = [
        user("Go"),
        assistant(
            "r1",
            {
                "type": "text",
                "text": "Done. Left for later: a paused heavy freezes the queue.",
            },
        ),
        assistant("r2", {"type": "text", "text": "All tests pass."}),
    ]
    sub = [
        user("Brief a"),
        assistant("s1", {"type": "text", "text": "The lint fix is out of scope here."}),
    ]
    session(Path(env["CLAUDE_CONFIG_DIR"]), main, a=sub)
    out = tmp_path / "scan"
    proc = scan(env, out, FAKE_REPLY="[]")
    aside = ("\n" + proc.stdout).split("\nset aside\n")[1].split("\n\n")[0].splitlines()
    lines = digest_lines(out)
    expected = [
        f"L{n}  {agent}  {lines[n - 1].strip()}"
        for n, agent in [
            (
                lines.index("  Done. Left for later: a paused heavy freezes the queue.")
                + 1,
                "main",
            ),
            (lines.index("  The lint fix is out of scope here.") + 1, "agent-a"),
        ]
    ]
    assert aside == expected


def test_a_retry_that_fails_keeps_the_first_merge_and_says_nothing_failed(
    env: dict, tmp_path: Path
) -> None:
    many(env, 3)
    out = tmp_path / "scan"
    poor = [{"title": "One", "summary": "s", "ids": ["1.1"]}]
    proc = scan(
        env,
        out,
        verify=True,
        FAKE_REPLY_EACH="1",
        FAKE_FINDINGS_FIRST=json.dumps(poor),
        FAKE_MERGE_FAIL_AFTER_FIRST="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert "merge failed" not in proc.stderr
    found = json.loads((out / "findings.json").read_text())
    assert found[0]["title"] == "One"


def test_every_merge_answer_is_saved_as_given(env: dict, tmp_path: Path) -> None:
    many(env, 3)
    out = tmp_path / "scan"
    poor = [{"title": "One", "summary": "s", "ids": ["1.1"]}]
    scan(
        env, out, verify=True, FAKE_REPLY_EACH="1", FAKE_FINDINGS_FIRST=json.dumps(poor)
    )
    attempts = json.loads((out / "merge.json").read_text())
    assert [[f["title"] for f in a] for a in attempts] == [["One"], ["One habit"]]
