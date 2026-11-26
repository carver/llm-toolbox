"""Runs a Python tool whose os.kill and os.killpg refuse any process outside one
tree, so a test of a tool that signals can't reach what the test didn't start.

    python3 signal_guard_testing.py ROOT TOOL ARGS...

A signal goes through only to a process with ROOT among its parents, as the test's
own pid is for every process the test starts. Any other target ends the tool at
once, before the signal is sent, with exit status REFUSED and a line on stderr
naming it. A broken match in safekill, such as a mutant that drops lib/procmatch.py's
name test, would otherwise signal every process the user owns, every session on the
machine included (#17); here its first stray target fails the test instead.
"""

import os
import runpy
import sys
from pathlib import Path

REFUSED = 86


def under(pid: int, root: int) -> bool:
    """Whether `root` is among `pid`'s parents, read from /proc."""
    seen = set()
    while pid not in seen:
        seen.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        # The parent pid is the field after the name, which may hold spaces or ")".
        pid = int(stat.rsplit(")", 1)[1].split()[1])
        if pid == root:
            return True
    return False


def guarded(send, root: int):
    def send_if_under(pid: int, sig: int) -> None:
        if pid <= 0 or not under(pid, root):
            print(
                f"signal_guard_testing: refused signal {sig} to {pid}", file=sys.stderr
            )
            sys.stderr.flush()
            os._exit(REFUSED)
        send(pid, sig)

    return send_if_under


def main() -> None:
    root, tool, *args = sys.argv[1:]
    os.kill = guarded(os.kill, int(root))
    os.killpg = guarded(os.killpg, int(root))
    sys.argv = [tool, *args]
    runpy.run_path(tool, run_name="__main__")


if __name__ == "__main__":
    main()
