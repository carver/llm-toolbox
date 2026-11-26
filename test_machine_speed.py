"""machine-speed: the cron probe that mails when the host throttles the CPU, and the
status line. The tests run the tool as a process with the probe's time faked
(HEAVY_SPEED_PROBE, read under the PERF_PROMISES_DIR conftest gives each test), its
clock set (MACHINE_SPEED_NOW) and the mail sent to a fake script
(MACHINE_SPEED_SENDER) that writes each message to a file; no test sends mail."""

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "bin" / "machine-speed"
sys.path.insert(0, str(ROOT / "lib"))
import machinespeed

ADDRESS = "me@example.com"
START = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
FAKE_SENDER = """\
import json, sys
from pathlib import Path
out = Path(sys.argv[0]).parent / "outbox"
out.mkdir(exist_ok=True)
args = sys.argv[1:]
with open(out.parent / "tries", "a") as tries:
    tries.write("x\\n")
if (out.parent / "fail").exists():
    print("Traceback (most recent call last):", file=sys.stderr)
    print("smtplib.SMTPAuthenticationError: (535, b'no for me@example.com')",
          file=sys.stderr)
    sys.exit(1)
body = Path(args[args.index("--body-file") + 1]).read_text()
n = len(list(out.iterdir()))
(out / f"{n:03}.json").write_text(json.dumps({"args": args, "body": body}))
print("Sent to me@example.com.")
"""


@pytest.fixture
def faked_machine(tmp_path):
    """machine-speed with its probe, clock and mail faked, as (run, mails, fail):
    run(cmd, minutes, factor) runs machine-speed at START plus minutes, its probe at
    factor times the usual speed; mails() lists the messages sent; fail(True) makes
    the sender fail; run.tries() counts its runs, failed ones included."""
    sender = tmp_path / "mail" / "send.py"
    sender.parent.mkdir()
    sender.write_text(FAKE_SENDER)
    config = tmp_path / "gitconfig"
    config.write_text(f"[llm-toolbox]\n\tnewsEmail = {ADDRESS}\n")
    stat = tmp_path / "stat"
    stat.write_text(proc_stat(0, 0))
    probe = tmp_path / "probe"
    env = os.environ | {
        "MACHINE_SPEED_SENDER": str(sender),
        "GIT_CONFIG_GLOBAL": str(config),
        "HEAVY_SPEED_PROBE": str(probe),
        "HEAVY_STAT": str(stat),
    }
    usual()

    def run(command, minutes=0, factor=1.0, **extra):
        probe.write_text(f"{0.01 / factor}\n")
        when = START + timedelta(minutes=minutes)
        return subprocess.run(
            [str(TOOL), command],
            env=env | {"MACHINE_SPEED_NOW": when.isoformat()} | extra,
            capture_output=True,
            text=True,
            input="",
            timeout=60,
            check=False,
        )

    def mails():
        outbox = sender.parent / "outbox"
        if not outbox.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(outbox.iterdir())]

    def fail(on):
        flag = sender.parent / "fail"
        if on:
            flag.write_text("")
        else:
            flag.unlink(missing_ok=True)

    def tries():
        path = sender.parent / "tries"
        return len(path.read_text().splitlines()) if path.exists() else 0

    run.stat, run.env, run.config, run.tries = stat, env, config, tries
    return run, mails, fail


def proc_stat(busy, steal, idle=1000):
    """A /proc/stat whose every core has run busy ticks, steal of them stolen, and
    idled idle ticks."""
    lines = ["cpu  0 0 0 0 0 0 0 0"]
    lines += [
        f"cpu{cpu} {busy - steal} 0 0 {idle} 0 0 0 {steal}"
        for cpu in range(os.cpu_count())
    ]
    return "\n".join(lines) + "\n"


def usual():
    """20 probes of 10 ms on vCPU 0 under no load: the cron probe's usual speed."""
    path = machinespeed.speed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.now(UTC).isoformat(timespec="seconds")
    line = f"{when}\t0.01\t0.01\t-\t0\t0\t0.00\tfake\t-\n"
    path.write_text(line * 20)


def subject(mail):
    args = mail["args"]
    return args[args.index("--subject") + 1]


def probes(run, factors, start=0, every=5):
    """Run a probe every `every` minutes from `start`, one per factor: the last run."""
    for step, factor in enumerate(factors):
        done = run("probe", start + step * every, factor)
        assert done.returncode == 0, done.stderr
    return done


def test_three_slow_probes_in_a_row_mail_once(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.2, 0.2])
    assert mails() == []

    done = probes(run, [0.2], start=10)
    (mail,) = mails()
    assert subject(mail) == f"{socket.gethostname()}: machine at 0.20x its usual speed"
    assert mail["args"][mail["args"].index("--to") + 1] == ADDRESS
    assert mail["body"].count("0.20x") >= 3
    assert "the same load" in mail["body"]
    assert "normal lane" in mail["body"]  # heavy's queue
    assert "throttl" in mail["body"]
    assert ADDRESS not in done.stdout + done.stderr

    probes(run, [0.2, 0.1], start=15)
    assert len(mails()) == 1


def test_a_probe_that_changes_nothing_prints_nothing(faked_machine):
    run, mails, _ = faked_machine
    done = run("probe", 0, 1.0)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert mails() == []


def test_slow_probes_spread_over_more_than_20_minutes_are_no_streak(faked_machine):
    run, mails, _ = faked_machine
    for minutes in (0, 5, 25):
        run("probe", minutes, 0.2)
    assert mails() == []


def test_a_probe_under_ordinary_speed_breaks_the_streak(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.2, 0.2, 0.3, 0.2, 0.2])
    assert mails() == []


def test_under_half_speed_alone_mails_nothing(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.4] * 5)
    assert mails() == []


def test_recovery_mails_once_after_a_slow_mail(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.2] * 3)
    probes(run, [0.6, 0.9], start=15)
    assert len(mails()) == 1
    probes(run, [1.0, 1.0], start=25)
    assert [subject(m) for m in mails()][1:] == [
        f"{socket.gethostname()}: machine back to 1.00x"
    ]
    assert "since" in mails()[1]["body"]


def test_a_slow_spell_whose_mail_never_went_gets_no_recovery_mail(faked_machine):
    run, mails, fail = faked_machine
    fail(True)
    probes(run, [0.2] * 3)
    fail(False)
    probes(run, [1.0] * 3, start=15)
    probes(run, [1.0] * 20, start=30)
    assert mails() == []


def test_a_new_slow_spell_mails_only_3_hours_after_the_last(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.2] * 3)  # slow mail at 10 min
    probes(run, [1.0] * 3, start=15)  # recovery mail
    probes(run, [0.2] * 3, start=60)  # slow again: too soon
    assert len(mails()) == 2
    probes(run, [0.2] * 3, start=165)  # 2 h 55 m after the first
    assert len(mails()) == 2
    probes(run, [0.2], start=190)  # 3 h after it
    assert len(mails()) == 3
    assert "at 0.20x" in subject(mails()[2])


def test_at_most_4_mails_a_day(faked_machine):
    run, mails, _ = faked_machine
    for hour in (0, 3, 6):
        probes(run, [0.2] * 3 + [1.0] * 3, start=hour * 60)
    assert len(mails()) == 4  # slow, back, slow, back; the third slow waits
    probes(run, [0.2] * 3, start=24 * 60 + 20)
    assert len(mails()) == 5


def test_a_failed_mail_is_retried_hourly_and_shown_on_the_line(faked_machine):
    run, mails, fail = faked_machine
    fail(True)
    done = probes(run, [0.2] * 3)
    assert "SMTPAuthenticationError" in done.stdout
    assert ADDRESS not in done.stdout + done.stderr
    line = run("line", 10, MACHINE_SPEED_NOW="")
    assert line.stdout == "cpu 0.2x slow (mail failed)\n"

    fail(False)
    probes(run, [0.2] * 11, start=15)  # 15 to 65 min: within the hour
    assert mails() == []
    probes(run, [0.2], start=70)
    assert len(mails()) == 1
    assert run("line", 70, MACHINE_SPEED_NOW="").stdout == "cpu 0.2x slow\n"


def test_a_failed_send_counts_toward_the_day_cap(faked_machine):
    """A send that fails may have delivered (a timeout after DATA, a disconnect
    from quit), so it counts as one of the day's 4."""
    run, _, fail = faked_machine
    fail(True)
    probes(run, [0.2] * 3)  # the first try, at 10 min
    probes(run, [0.2] * 5, start=70, every=60)  # retried at 70, 130 and 190 only
    assert run.tries() == 4
    probes(run, [0.2], start=24 * 60 + 15)  # a day after the first
    assert run.tries() == 5


def test_a_spell_that_ends_unsent_leaves_no_failure_behind(faked_machine):
    run, mails, fail = faked_machine
    fail(True)
    probes(run, [0.2] * 3)  # fails at 10 min
    fail(False)
    probes(run, [1.0] * 3, start=15)  # the spell ends, unsent
    assert run("line", 25, MACHINE_SPEED_NOW="").stdout == "cpu 1.0x\n"
    probes(run, [0.2] * 3, start=30)  # a new spell, within the hour
    assert [subject(m) for m in mails()] == [
        f"{socket.gethostname()}: machine at 0.20x its usual speed"
    ]


def test_with_no_recipient_the_mail_fails_and_says_so(faked_machine):
    run, mails, _ = faked_machine
    run.config.write_text("")
    done = probes(run, [0.2] * 3)
    assert "no recipient" in done.stdout
    assert mails() == []


def test_the_mail_names_the_steal_between_probes(faked_machine):
    run, mails, _ = faked_machine
    probes(run, [0.2, 0.2])
    run.stat.write_text(proc_stat(500, 250, idle=1500))
    probes(run, [0.2], start=10)
    (mail,) = mails()
    assert "steal 25%" in mail["body"]  # 250 of 1000 more ticks a core


def test_a_probe_that_cannot_run_exits_1(faked_machine):
    run, _, _ = faked_machine
    probe = Path(run.env["HEAVY_SPEED_PROBE"])
    probe.write_text("not a number\n")
    done = subprocess.run(
        [str(TOOL), "probe"],
        env=run.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert len(done.stderr.splitlines()) == 1


def seed(*lines):
    path = machinespeed.speed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))


def logged(factor, minutes_ago):
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    stamp = when.isoformat(timespec="seconds")
    return f"{stamp}\t0.02\t0.02\t-\t0\t0\t2.00\tprobe\t{factor}\n"


def line(stdin='{"model": {"id": "x"}}'):
    return subprocess.run(
        [str(TOOL), "line"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("factor", "shown"),
    [("0.93", "cpu 0.9x"), ("1.07", "cpu 1.0x"), ("0.50", "cpu 0.5x")],
)
def test_the_line_shows_the_newest_probes_speed(factor, shown):
    seed(logged("0.10", 9), logged(factor, 2))
    done = line()
    assert (done.returncode, done.stdout) == (0, shown + "\n")


def test_the_line_shows_an_older_known_probe_when_the_newest_is_unknown():
    seed(logged("0.90", 10), logged("-", 1))
    assert line().stdout == "cpu 0.9x\n"


def test_the_line_says_slow_under_half_speed():
    seed(logged("0.49", 1))
    assert line().stdout == "cpu 0.4x slow\n"


@pytest.mark.parametrize(
    "lines",
    [[], [logged("0.90", 16)], [logged("-", 1)]],
    ids=["no probes", "stale", "no baseline"],
)
def test_the_line_is_a_question_mark_without_a_recent_known_speed(lines):
    seed(*lines)
    done = line()
    assert (done.returncode, done.stdout) == (0, "cpu ?\n")


def test_the_line_survives_a_broken_alert_file():
    seed(logged("0.90", 1))
    path = machinespeed.speed_file()
    path.with_name(path.stem + ".alert.json").write_text("{not json")
    done = line(stdin="")
    assert (done.returncode, done.stdout) == (0, "cpu 0.9x\n")


@pytest.mark.parametrize("broken", ["missing", "raises"])
def test_the_line_survives_a_broken_lib(tmp_path, broken):
    tool = tmp_path / "bin" / "machine-speed"
    tool.parent.mkdir()
    tool.write_text(TOOL.read_text())
    tool.chmod(0o755)
    if broken == "raises":
        (tmp_path / "lib").mkdir()
        for module in ("cores", "machinespeed"):
            (tmp_path / "lib" / f"{module}.py").write_text("raise ImportError\n")
    done = subprocess.run(
        [str(tool), "line"], input="", capture_output=True, text=True, check=False
    )
    assert (done.returncode, done.stdout) == (0, "cpu ?\n")


def test_help_is_the_docstring():
    done = subprocess.run(
        [str(TOOL), "-h"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0
    assert "machine-speed probe" in done.stdout
    assert "machine-speed line" in done.stdout


# The slow spell's warning in `heavy status`, `heavy waits` and the session start.

HEAVY = ROOT / "bin" / "heavy"
PERF_PROMISES = ROOT / "bin" / "perf-promises"


def spell(factor, minutes_ago, slow=True):
    """machine-speed's state as its cron leaves it: three probes 5 minutes apart, the
    newest `minutes_ago` minutes ago at `factor`, and a slow spell open or not."""
    newest = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    readings = [
        {"at": (newest - timedelta(minutes=5 * n)).isoformat(), "factor": factor}
        for n in (2, 1, 0)
    ]
    state = {"readings": readings, "slow": slow, "since": readings[0]["at"]}
    path = machinespeed.alert_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def surfaces(tmp_path):
    """What `heavy status`, `heavy waits`, `heavy waits --json` and perf-promises'
    session start print, each as one text, stdout and stderr together."""
    env = os.environ | {"HEAVY_DIR": str(tmp_path / "heavy")}

    def run(tool, *args, stdin=""):
        done = subprocess.run(
            [str(tool), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=tmp_path,
        )
        assert done.returncode == 0, done.stderr
        return done

    hook = run(PERF_PROMISES, "--session-start", stdin=json.dumps({"cwd": "/"}))
    return {
        "status": run(HEAVY, "status"),
        "waits": run(HEAVY, "waits"),
        "waits --json": run(HEAVY, "waits", "--json"),
        "session start": hook,
    }


def test_a_slow_spell_is_one_loud_line_in_heavy_status_waits_and_session_start(
    tmp_path,
):
    spell(0.14, 2)

    shown = surfaces(tmp_path)

    warning = "WARNING: host CPU running 7x slower than usual (0.14x its usual speed"
    status = shown["status"].stdout.splitlines()
    assert status[0].startswith(warning), status
    assert status[0].endswith("check the host for throttling (PROCHOT)."), status
    for name in ("waits", "waits --json"):
        # On stderr, so that stdout stays waits' one line, or its JSON.
        assert shown[name].stderr.startswith(warning), shown[name].stderr
        assert warning not in shown[name].stdout, shown[name].stdout
    json.loads(shown["waits --json"].stdout)
    hook = json.loads(shown["session start"].stdout)
    assert hook["systemMessage"].startswith(warning), hook
    assert hook["hookSpecificOutput"]["additionalContext"].startswith(warning), hook


@pytest.mark.parametrize(
    "state",
    [(0.14, 21, True), (0.6, 1, False), None],
    ids=["cron stopped 21 minutes ago", "no spell open", "no state"],
)
def test_without_a_fresh_slow_spell_nothing_warns(tmp_path, state):
    if state is not None:
        spell(*state)

    shown = surfaces(tmp_path)

    for name, done in shown.items():
        assert "WARNING" not in done.stdout + done.stderr, name
    assert shown["session start"].stdout == ""


@pytest.mark.parametrize(
    ("factors", "warns"),
    [
        ([0.14, 0.2, 0.3], True),
        ([0.14, 0.2, 0.4], False),
        ([0.2, 0.2, None], True),
        ([0.2, 0.45, None], False),
    ],
    ids=["still slow", "recovering", "newest unknown", "recovering, newest unknown"],
)
def test_a_spell_warns_while_its_newest_known_factor_is_under_a_third(factors, warns):
    """A spell stays open until 3 probes at 0.5x or above, but by then the host runs
    at most 2x slower: the line would say `1x slower` through the recovery."""
    now = datetime.now(UTC)
    readings = [
        {"at": (now - timedelta(minutes=minutes)).isoformat(), "factor": factor}
        for minutes, factor in zip((10, 5, 0), factors, strict=True)
    ]
    path = machinespeed.alert_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"readings": readings, "slow": True, "since": readings[0]["at"]})
    )

    assert (machinespeed.slow_warning(now) is not None) == warns


def test_a_spell_whose_known_factors_are_stale_gives_no_warning():
    # Cron still runs, but its probes have failed for 21 minutes.
    now = datetime.now(UTC)
    readings = [
        {"at": (now - timedelta(minutes=minutes)).isoformat(), "factor": factor}
        for minutes, factor in ((26, 0.14), (21, 0.14), (1, None))
    ]
    path = machinespeed.alert_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"readings": readings, "slow": True, "since": readings[0]["at"]})
    )

    assert machinespeed.slow_warning(now) is None


@pytest.mark.parametrize("text", ["{not json", "[]", '{"slow": true}'])
def test_a_broken_state_gives_no_warning(text):
    path = machinespeed.alert_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    assert machinespeed.slow_warning() is None
