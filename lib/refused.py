"""Save a Bash command a gate refused, and its heredoc bodies, for the next call to reuse.

A refused call runs none of its parts, so an agent types the whole command again, a long
heredoc of edits included. ending(event) returns what every gate's refusal ends with:
NOTHING_RAN, then a sentence naming the files it saved the command in, to copy from.

The files go in the session's scratchpad, <root>/claude-<uid>/<project>/<session>/
scratchpad/refused/: <h>.txt holds the command, <h>-<n>-<delimiter>.txt the body of its
n-th heredoc, <h> the first 12 hex digits of the command's SHA-256, so gates refusing
one call at once write the same names. The session folder is the one named like the
transcript's folder, else the only one holding the session's id; with none, nothing
is saved. The files are the user's alone (600, in 700 folders); a command can hold a
secret typed inline, as the transcript already does. Claude Code never cleans these
folders, while it sweeps transcripts after 30 days, so each save first removes the
files it names this way, older than 30 days, from every session's refused/ folder.

It never raises: a save that fails leaves the ending at NOTHING_RAN, or names only the
files written.

Environment:
    REFUSED_SCRATCH_ROOT  the folder holding claude-<uid>/, instead of Claude Code's
                          temp root (the tests set it)
    CLAUDE_CODE_TMPDIR    Claude Code's temp root, when set
    TMPDIR, TMP, TEMP     else the first of these that is set, as Node's os.tmpdir()
                          reads them on Linux, else /tmp
"""

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from shellparse import NOTHING_RAN, heredocs

SESSION_ID = re.compile(r"[A-Za-z0-9-]+")
# The names a save writes, and its temp files, which the prune removes.
SAVED = re.compile(r"[0-9a-f]{12}(-\d+-[A-Za-z0-9_-]{1,64})?\.txt(\.[^/]+\.tmp)?")
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
KEEP_SECONDS = 30 * 24 * 3600


def ending(event: dict) -> str:
    """NOTHING_RAN, then, when it could save the command, where it did."""
    try:
        saved = save(event)
    except Exception:  # noqa: BLE001 - a refusal must still print, whatever fails here
        saved = None
    if saved is None:
        return NOTHING_RAN
    sentence = f"To reuse its text, the command is in {saved.command}"
    if bodies := saved.bodies:
        names = " and ".join(
            [", ".join(bodies[:-1]), bodies[-1]] if bodies[1:] else bodies
        )
        body = "body" if len(bodies) == 1 else "bodies"
        sentence += f", and its heredoc {body} in {names} beside it"
    return f"{NOTHING_RAN} {sentence}."


class Saved(NamedTuple):
    command: Path  # the file holding the command
    bodies: list[str]  # the names of the files beside it holding its heredoc bodies


def save(event) -> Saved | None:
    """The files it saved the command in, or None when it saved none."""
    command = (event.get("tool_input") or {}).get("command")
    session = session_folder(event)
    if not command or session is None:
        return None
    prune(session.parent.parent)
    folder = session / "scratchpad" / "refused"
    for made in (folder.parent, folder):
        made.mkdir(mode=0o700, exist_ok=True)
    stem = hashlib.sha256(command.encode()).hexdigest()[:12]
    if not write(folder, f"{stem}.txt", command):
        return None
    saved = Saved(folder / f"{stem}.txt", [])
    for n, (delimiter, body) in enumerate(heredocs(command), 1):
        name = f"{stem}-{n}-{UNSAFE.sub('_', delimiter)[:64]}.txt"
        if write(folder, name, body):
            saved.bodies.append(name)
    return saved


def session_folder(event):
    """The session's folder under Claude Code's temp root, or None."""
    session = event.get("session_id")
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return None
    claude = temp_root() / f"claude-{os.getuid()}"
    transcript = event.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        named = claude / Path(transcript).parent.name / session
        if Path(transcript).parent.name and named.is_dir():
            return named
    found = [p for p in claude.glob(f"*/{session}") if p.is_dir()]
    return found[0] if len(found) == 1 else None


def temp_root():
    for name in ("REFUSED_SCRATCH_ROOT", "CLAUDE_CODE_TMPDIR", "TMPDIR", "TMP", "TEMP"):
        if value := os.environ.get(name):
            return Path(value)
    return Path("/tmp")


def write(folder, name, text):
    """Write text to folder/name atomically, mode 600; whether it was written."""
    try:
        fd, temp = tempfile.mkstemp(dir=folder, prefix=f"{name}.", suffix=".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp, folder / name)
        return True
    except OSError:
        Path(temp).unlink(missing_ok=True)
        return False


def prune(claude):
    """Remove saved files older than KEEP_SECONDS from every session's refused/ folder
    under claude, and only those: a folder that is a link is never followed."""
    cutoff = time.time() - KEEP_SECONDS
    for folder in claude.glob("*/*/scratchpad/refused"):
        try:
            # Every folder below the project's a real one, so a link leads nowhere.
            parts = (folder.parent.parent, folder.parent, folder)
            if any(part.is_symlink() for part in parts) or not folder.is_dir():
                continue
            for entry in os.scandir(folder):
                if (
                    SAVED.fullmatch(entry.name)
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ):
                    os.unlink(entry.path)
        except OSError:
            continue
