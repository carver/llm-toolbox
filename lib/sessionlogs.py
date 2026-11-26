"""Where Claude Code keeps a session's logs, and how their records give their time.

Logs live under $CLAUDE_CONFIG_DIR (default ~/.claude) in
projects/<project>/<session>.jsonl, with subagents in
projects/<project>/<session>/subagents/agent-<id>.jsonl (beside each, an
agent-<id>.meta.json with its description) and workflow agents in
projects/<project>/<session>/subagents/workflows/<run>/agent-<id>.jsonl.
"""

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple


class AgentLog(NamedTuple):
    name: str  # agent-<id>, or <run>:<label> for a workflow agent
    meta: dict  # the .meta.json beside the log, or {} when there is none
    path: Path
    workflow: bool


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def timestamp(record: dict) -> datetime | None:
    """The record's time; one without a zone is read as UTC, as the logs write it."""
    try:
        when = datetime.fromisoformat(str(record["timestamp"]))
    except (KeyError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def read_jsonl(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def agent_logs(session: Path) -> list[AgentLog]:
    """The session's subagents' logs, then its workflow agents', each in the order
    they were last written. One deleted while they are listed is left out."""
    folder = session.with_suffix("") / "subagents"
    found = [AgentLog(path.stem, meta, path, False) for path, meta in _logs_in(folder)]
    for run in _by_mtime((folder / "workflows").glob("*/")):
        found += _workflow_run(run)
    return found


def _by_mtime(paths: Iterable[Path]) -> list[Path]:
    """The paths that still exist, by when each was last written, then by name: logs
    written at once keep one order."""
    stamped = []
    for path in paths:
        try:
            stamped.append((path.stat().st_mtime, path.name, path))
        except FileNotFoundError:
            continue
    return [path for _, _, path in sorted(stamped)]


def _meta(log: Path) -> dict:
    try:
        return json.loads(log.with_suffix(".meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _logs_in(folder: Path) -> list[tuple[Path, dict]]:
    return [(log, _meta(log)) for log in _by_mtime(folder.glob("agent-*.jsonl"))]


def _workflow_run(run: Path) -> list[AgentLog]:
    """A workflow run's agents, named <run>:<label> (run id without wf_), plus
    #<agent id tail> when two agents in the run share a label."""
    logs = _logs_in(run)
    labels = [meta.get("description") or log.stem for log, meta in logs]
    found = []
    for (log, meta), label in zip(logs, labels, strict=True):
        suffix = (
            f"#{log.stem.removeprefix('agent-')[:6]}" if labels.count(label) > 1 else ""
        )
        name = f"{run.name.removeprefix('wf_')}:{label}{suffix}"
        found.append(AgentLog(name, meta, log, True))
    return found
