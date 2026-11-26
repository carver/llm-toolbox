"""Tests for bin/profile-top: a summary of a samply profile, and its record mode."""

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "bin" / "profile-top"

# The made-up program's symbols: each covers addresses [rva, rva + size).
SYMBOLS = [
    (0, 100, "main"),
    (100, 100, "app::work::h0123456789abcdef"),
    (200, 100, "argon2::compress"),
    (300, 100, "app::other"),
]
# Each frame's address, by name; 900 lies in no symbol.
ADDRESS = {"main": 10, "work": 110, "hash": 210, "other": 310, "unknown": 900}
# Each process's samples, as stacks from the root to the leaf.
SAMPLES = {
    "it-test": [
        ["main", "work", "hash"],
        ["main", "work", "hash"],
        ["main", "work", "hash"],
        ["main", "work"],
        ["main", "other"],
        ["main", "other"],
        ["main", "unknown"],
    ],
    "server": [["main", "other", "hash"], ["main", "other", "hash"]],
}


def thread(
    process: str, stacks: list[list[str]], address: dict[str, int] = ADDRESS
) -> dict:
    """One thread of samply's processed profile holding `stacks`."""
    names = list(address)
    stack_table: dict[str, list] = {"prefix": [], "frame": []}
    index: dict[tuple[str, ...], int] = {}
    samples = []
    for stack in stacks:
        prefix = None
        for depth in range(len(stack)):
            key = tuple(stack[: depth + 1])
            if key not in index:
                index[key] = len(stack_table["frame"])
                stack_table["prefix"].append(prefix)
                stack_table["frame"].append(names.index(stack[depth]))
            prefix = index[key]
        samples.append(prefix)
    return {
        # "NAME#2" is a second thread of the process NAME.
        "processName": process.split("#")[0],
        "samples": {"length": len(samples), "stack": samples, "weight": None},
        "stackTable": stack_table,
        "frameTable": {
            "address": [address[name] for name in names],
            "func": list(range(len(names))),
        },
        "funcTable": {"resource": [0] * len(names), "name": [0] * len(names)},
        "resourceTable": {"lib": [0]},
        "stringArray": ["unused"],
    }


def write_profile(
    folder: Path,
    samples: dict[str, list[list[str]]] = SAMPLES,
    symbols: list[tuple[int, int, str]] = SYMBOLS,
    address: dict[str, int] = ADDRESS,
    name: str = "prof.json.gz",
) -> Path:
    """The profile and its presymbolicated sidecar, as samply writes them."""
    profile = {
        "libs": [{"name": "prog", "debugName": "prog"}],
        "threads": [
            thread(process, stacks, address) for process, stacks in samples.items()
        ],
    }
    path = folder / name
    with (gzip.open if name.endswith(".gz") else open)(path, "wt") as out:
        json.dump(profile, out)
    strings = [name for _, _, name in symbols]
    sidecar = {
        "string_table": strings,
        "data": [
            {
                "debug_name": "prog",
                "symbol_table": [
                    {"rva": rva, "size": size, "symbol": strings.index(name)}
                    for rva, size, name in symbols
                ],
            }
        ],
    }
    # samply names the sidecar by replacing the profile's last suffix.
    path.with_suffix(".syms.json").write_text(json.dumps(sidecar))
    return path


def run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


def rows(output: str, heading: str) -> list[str]:
    """The lines under `--- heading`, up to the next heading or the end."""
    lines = output.splitlines()
    start = lines.index(f"--- {heading}") + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("--- ")),
        len(lines),
    )
    return [line.strip() for line in lines[start:end]]


def test_it_sums_up_samples_by_process_self_and_inclusive(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)))

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.splitlines()[0] == "9 samples"
    assert rows(out, "by process") == ["77.8%  7  it-test", "22.2%  2  server"]
    assert rows(out, "self") == [
        "55.6%  5  argon2::compress",
        "22.2%  2  app::other",
        "11.1%  1  app::work",
        "11.1%  1  prog+?",
    ]
    # main is on every stack, and is left out of inclusive.
    assert rows(out, "inclusive") == [
        "55.6%  5  argon2::compress",
        "44.4%  4  app::other",
        "44.4%  4  app::work",
        "11.1%  1  prog+?",
    ]


LONG = (
    "<std::thread::local::LocalKey<tokio::runtime::context::Context>>::with::"
    + "x" * 300
)
# A stripped library's start-up frame under every stack, a runtime frame whose name
# starts with a generic, and a name past any width.
NOISY_SYMBOLS = [
    (0, 100, "fun_1375c5"),
    (100, 100, LONG),
    (200, 100, "app::hot"),
    (300, 100, "app::" + "y" * 300),
]
NOISY_ADDRESS = {"start": 10, "local": 110, "hot": 210, "long": 310}
NOISY_SAMPLES = {
    "prog": [["start", "local", "hot"]] * 3 + [["start", "long"]] * 1,
}


def test_inclusive_leaves_out_what_nearly_every_stack_carries(tmp_path: Path) -> None:
    profile = write_profile(tmp_path, NOISY_SAMPLES, NOISY_SYMBOLS, NOISY_ADDRESS)

    result = run(str(profile))

    assert result.returncode == 0, result.stderr
    assert rows(result.stdout, "self") == [
        "75.0%  3  app::hot",
        f"25.0%  1  app::{'y' * 154}…",
    ]
    assert rows(result.stdout, "inclusive") == [
        "75.0%  3  app::hot",
        f"25.0%  1  app::{'y' * 154}…",
    ]
    assert "100.0%  4  prog:fun_1375c5" in rows(
        run(str(profile), "--all").stdout, "inclusive"
    )


def test_a_frame_under_all_of_one_process_is_left_out_though_rare_overall(
    tmp_path: Path,
) -> None:
    # 20 samples of a program whose every stack starts in `start`, beside 60 of
    # processes that never run it: `start` is a quarter of all samples.
    samples = {"prog": [["start", "local", "hot"]] * 20}
    samples |= {f"<{pid}>": [["hot"]] * 3 for pid in range(20)}
    profile = write_profile(tmp_path, samples, NOISY_SYMBOLS, NOISY_ADDRESS)

    names = [row.split("  ")[-1] for row in rows(run(str(profile)).stdout, "inclusive")]

    assert names == ["app::hot"]


def test_a_frame_under_every_sample_of_one_thread_is_left_out(tmp_path: Path) -> None:
    # A spawned thread's samples all start in libc's thread start; the main thread's,
    # as many, never do, so it's on half of the process's samples.
    samples = {
        "prog": [["local", "hot"]] * 20,
        "prog#2": [["start", "local", "long"]] * 20,
    }
    profile = write_profile(tmp_path, samples, NOISY_SYMBOLS, NOISY_ADDRESS)

    names = [row.split("  ")[-1] for row in rows(run(str(profile)).stdout, "inclusive")]

    assert "prog:fun_1375c5" not in names
    assert names[0] == "app::hot"


@pytest.mark.parametrize("name", ["prof.json", "prof"])
def test_a_profile_not_named_json_gz_is_read_with_its_sidecar(
    tmp_path: Path, name: str
) -> None:
    result = run(str(write_profile(tmp_path, name=name)))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "9 samples"


def test_a_root_shared_with_a_busier_thread_stays_in_inclusive(tmp_path: Path) -> None:
    # The caller of the hot code runs on a pool thread, where it's on every sample,
    # and on the main thread, where most of its samples are.
    samples = {
        "prog": [["main", "work", "hash"]] * 250 + [["main", "other"]] * 250,
        "prog#2": [["work", "hash"]] * 20,
    }
    names = [
        row.split("  ")[-1]
        for row in rows(run(str(write_profile(tmp_path, samples))).stdout, "inclusive")
    ]

    assert "app::work" in names


def test_by_thread_names_each_threads_own_root(tmp_path: Path) -> None:
    samples = {
        "prog": [["start", "local", "hot"]] * 30,
        "prog#2": [["start", "long"]] * 10,
    }
    profile = write_profile(tmp_path, samples, NOISY_SYMBOLS, NOISY_ADDRESS)

    lines = rows(run(str(profile)).stdout, "by thread")

    assert lines[0] == "75.0%  30  prog: app::hot"
    assert lines[1].startswith("25.0%  10  prog: app::yyy")


def test_a_threads_root_is_the_frame_its_samples_share_past_the_runtime(
    tmp_path: Path,
) -> None:
    # Every stack holds main, then a tokio frame, then app::work; the leaves differ.
    address = {**ADDRESS, "local": 410}
    symbols = [*SYMBOLS, (400, 100, LONG)]
    samples = {
        "prog": [["main", "local", "work", "hash"]] * 2
        + [["main", "local", "work"]] * 2,
    }

    lines = rows(
        run(str(write_profile(tmp_path, samples, symbols, address))).stdout,
        "by thread",
    )

    assert lines == ["100.0%  4  prog: app::work"]


def test_a_thread_sharing_only_runtime_frames_has_no_root(tmp_path: Path) -> None:
    address = {**ADDRESS, "local": 410}
    symbols = [*SYMBOLS, (400, 100, LONG)]
    samples = {
        "prog": [["main", "local", "work", "hash"]] * 2
        + [["main", "local", "other"]] * 2,
    }

    lines = rows(
        run(str(write_profile(tmp_path, samples, symbols, address))).stdout,
        "by thread",
    )

    assert lines == ["100.0%  4  prog: (no frame common to its samples)"]


def test_callers_skip_runtime_frames(tmp_path: Path) -> None:
    address = {**ADDRESS, "local": 410}
    symbols = [*SYMBOLS, (400, 100, LONG)]
    samples = {"prog": [["main", "work", "local", "hash"]] * 2}
    profile = write_profile(tmp_path, samples, symbols, address)

    lines = rows(run(str(profile), "--callers", "argon2").stdout, "callers of argon2")

    assert lines == ["100.0%  2  app::work"]


def test_a_pattern_that_isnt_a_regex_is_a_usage_error(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)), "--match", "(")

    assert result.returncode == 2
    assert "isn't a regular expression" in result.stderr


def test_callers_skip_raw_addresses_and_unsymbolized_frames(tmp_path: Path) -> None:
    symbols = [*SYMBOLS, (500, 100, "0x2c")]
    address = {**ADDRESS, "raw": 510}
    samples = {"prog": [["main", "work", "unknown", "raw", "hash"]] * 2}
    profile = write_profile(tmp_path, samples, symbols, address)

    lines = rows(run(str(profile), "--callers", "argon2").stdout, "callers of argon2")

    assert lines == ["100.0%  2  app::work"]


def test_runtime_plumbing_is_left_out_on_however_few_stacks(tmp_path: Path) -> None:
    # The tokio frame is on half the stacks, so only its name leaves it out.
    samples = {"prog": [["local", "hot"]] * 10 + [["hot"]] * 10}
    profile = write_profile(tmp_path, samples, NOISY_SYMBOLS, NOISY_ADDRESS)

    names = [row.split("  ")[-1] for row in rows(run(str(profile)).stdout, "inclusive")]

    assert names == ["app::hot"]


def test_a_frame_outside_any_library_takes_its_name_from_the_thread(
    tmp_path: Path,
) -> None:
    path = write_profile(tmp_path, {"prog": [["main", "work"]] * 2})
    with gzip.open(path, "rt") as source:
        profile = json.load(source)
    # samply puts a frame with no library, such as the kernel's, in the thread's own
    # strings: resource -1, its name an index into stringArray.
    thread = profile["threads"][0]
    thread["funcTable"]["resource"][1] = -1
    thread["funcTable"]["name"][1] = 1
    thread["stringArray"] = ["unused", "[kernel] do_syscall"]
    with gzip.open(path, "wt") as out:
        json.dump(profile, out)

    assert rows(run(str(path)).stdout, "self") == ["100.0%  2  [kernel] do_syscall"]


def test_a_profile_with_no_samples_says_so(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path, {})))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 samples"


def test_match_shows_the_processes_it_hit_then_counts_the_rest(tmp_path: Path) -> None:
    samples = {f"<{pid}>": [["main", "other"]] for pid in range(12)}
    samples["hit"] = [["main", "work", "hash"]] * 2
    profile = write_profile(tmp_path, samples)

    lines = rows(run(str(profile), "--match", "argon2").stdout, "matching argon2")

    assert lines[:2] == ["14.3%  2 of 14  all", "100.0%  2 of 2  hit"]
    assert len(lines) == 2 + 7 + 1
    assert lines[-1] == "0.0%  0 of 5  5 other processes"


def test_all_keeps_the_frames_every_stack_carries(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)), "--all")

    assert rows(result.stdout, "inclusive")[0] == "100.0%  9  main"


def test_n_limits_each_list(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)), "-n", "1")

    assert rows(result.stdout, "self") == ["55.6%  5  argon2::compress"]
    assert rows(result.stdout, "inclusive") == ["55.6%  5  argon2::compress"]


def test_match_gives_its_share_overall_and_of_each_process(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)), "--match", "argon2")

    assert result.returncode == 0, result.stderr
    assert rows(result.stdout, "matching argon2") == [
        "55.6%  5 of 9  all",
        "42.9%  3 of 7  it-test",
        "100.0%  2 of 2  server",
    ]


def test_callers_are_the_nearest_named_frames_above_the_match(tmp_path: Path) -> None:
    result = run(str(write_profile(tmp_path)), "--callers", "argon2")

    assert rows(result.stdout, "callers of argon2") == [
        "60.0%  3  app::work",
        "40.0%  2  app::other",
    ]


def test_a_profile_that_cant_be_read_is_a_usage_error(tmp_path: Path) -> None:
    result = run(str(tmp_path / "missing.json.gz"))

    assert result.returncode == 2
    assert "missing.json.gz" in result.stderr


# Copies the test profile to its -o path unless $FAKE_SAMPLY_WRITES is 0, logs its
# arguments, and exits $FAKE_SAMPLY_STATUS.
FAKE_SAMPLY = """#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
Path("{log}").write_text(" ".join(args))
out = Path(args[args.index("-o") + 1])
if os.environ.get("FAKE_SAMPLY_WRITES", "1") == "1":
    shutil.copy("{profile}", out)
    shutil.copy("{sidecar}", out.with_suffix(".syms.json"))
sys.exit(int(os.environ.get("FAKE_SAMPLY_STATUS", "0")))
"""


@pytest.fixture
def samply(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """An environment whose `samply` copies the test profile to its -o path and logs
    its arguments, with perf events allowed."""
    source = tmp_path / "source"
    source.mkdir()
    write_profile(source)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    log = tmp_path / "samply.args"
    (fake / "samply").write_text(
        FAKE_SAMPLY.format(
            log=log,
            profile=source / "prof.json.gz",
            sidecar=source / "prof.json.syms.json",
        )
    )
    (fake / "samply").chmod(0o755)
    paranoid = tmp_path / "paranoid"
    paranoid.write_text("1\n")
    env = os.environ | {
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "PROFILE_TOP_PARANOID": str(paranoid),
        "TMPDIR": str(tmp_path),
    }
    return env, log


def test_record_profiles_a_command_and_sums_it_up(
    tmp_path: Path, samply: tuple[dict[str, str], Path]
) -> None:
    env, log = samply

    result = run("record", "-n", "1", "--", "sleep", "1", env=env)

    assert result.returncode == 0, result.stderr
    args = log.read_text().split()
    assert args[:5] == [
        "record",
        "--save-only",
        "--unstable-presymbolicate",
        "--rate",
        "1000",
    ]
    assert args[args.index("--") :] == ["--", "sleep", "1"]
    written = Path(args[args.index("-o") + 1])
    assert written.parent == tmp_path
    assert str(written) in result.stdout
    assert rows(result.stdout, "self") == ["55.6%  5  argon2::compress"]


def test_record_takes_a_rate_and_an_output(
    tmp_path: Path, samply: tuple[dict[str, str], Path]
) -> None:
    env, log = samply
    out = tmp_path / "mine.json.gz"

    result = run(
        "record",
        "--rate",
        "250",
        "-o",
        str(out),
        "--match",
        "argon2",
        "--",
        "true",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = log.read_text().split()
    assert args[args.index("--rate") + 1] == "250"
    assert args[args.index("-o") + 1] == str(out)
    assert "--- matching argon2" in result.stdout


def test_a_failed_recording_exits_1_and_still_sums_up_what_it_wrote(
    samply: tuple[dict[str, str], Path],
) -> None:
    env, _ = samply

    result = run("record", "--", "true", env=env | {"FAKE_SAMPLY_STATUS": "1"})

    assert result.returncode == 1
    assert "9 samples" in result.stdout


def test_a_recording_that_wrote_nothing_leaves_no_file(
    tmp_path: Path, samply: tuple[dict[str, str], Path]
) -> None:
    env, _ = samply
    env = env | {"FAKE_SAMPLY_WRITES": "0", "FAKE_SAMPLY_STATUS": "1"}

    result = run("record", "--", "/nonexistent", env=env)

    assert result.returncode == 1
    assert not list(tmp_path.glob("profile-top-*"))
    assert "profile:" not in result.stdout


def test_a_recording_to_a_named_file_that_never_came_is_a_failure_not_a_crash(
    tmp_path: Path, samply: tuple[dict[str, str], Path]
) -> None:
    env, _ = samply
    env = env | {"FAKE_SAMPLY_WRITES": "0"}

    result = run("record", "-o", str(tmp_path / "x.json.gz"), "--", "true", env=env)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_record_without_a_command_is_a_usage_error(
    samply: tuple[dict[str, str], Path],
) -> None:
    env, log = samply

    result = run("record", "--", env=env)

    assert result.returncode == 2
    assert "needs a command" in result.stderr
    assert not log.exists()


def test_record_names_a_paranoid_setting_it_cant_read(
    samply: tuple[dict[str, str], Path],
) -> None:
    env, _ = samply
    Path(env["PROFILE_TOP_PARANOID"]).write_text("closed\n")

    result = run("record", "--", "true", env=env)

    assert result.returncode == 2
    assert f"can't read {env['PROFILE_TOP_PARANOID']}" in result.stderr


def test_record_refuses_while_perf_events_are_closed(
    tmp_path: Path, samply: tuple[dict[str, str], Path]
) -> None:
    env, log = samply
    Path(env["PROFILE_TOP_PARANOID"]).write_text("2\n")

    result = run("record", "--", "true", env=env)

    assert result.returncode == 2
    assert "sudo sysctl kernel.perf_event_paranoid=1" in result.stderr
    assert not log.exists()


def test_record_without_samply_names_where_it_comes_from(tmp_path: Path) -> None:
    paranoid = tmp_path / "paranoid"
    paranoid.write_text("1\n")
    env = os.environ | {"PATH": "/nonexistent", "PROFILE_TOP_PARANOID": str(paranoid)}

    result = run("record", "--", "true", env=env)

    assert result.returncode == 2
    assert "samply" in result.stderr
    assert "sandbox-setup" in result.stderr
