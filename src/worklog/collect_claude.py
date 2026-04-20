"""Read Claude Code Stop-hook records from hooks.jsonl.

Each line is a JSON object written by hooks/claude-stop-hook.sh at the end of
a Claude Code session, with shape:

    {
        "timestamp": "2026-04-20T18:45:12Z",
        "cwd": "/home/user/proj",
        "session_id": "…",
        "files": ["src/a.py", "src/b.py"],
        "prompt_count": 14,
        "duration_s": 1820
    }

Anything we can't parse, we silently skip — this input is best-effort.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClaudeRecord:
    timestamp: dt.datetime
    cwd: str
    session_id: str
    files: list[str] = field(default_factory=list)
    prompt_count: int = 0
    duration_s: int = 0


def _parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def records_in_window(
    hooks_log: Path,
    since: dt.datetime,
    until: dt.datetime,
) -> list[ClaudeRecord]:
    if not hooks_log.exists():
        return []
    out: list[ClaudeRecord] = []
    with hooks_log.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(rec.get("timestamp", ""))
            if ts is None:
                continue
            if ts < since or ts > until:
                continue
            files = rec.get("files") or []
            if not isinstance(files, list):
                files = []
            out.append(
                ClaudeRecord(
                    timestamp=ts,
                    cwd=str(rec.get("cwd", "")),
                    session_id=str(rec.get("session_id", "")),
                    files=[str(f) for f in files][:40],
                    prompt_count=int(rec.get("prompt_count", 0) or 0),
                    duration_s=int(rec.get("duration_s", 0) or 0),
                )
            )
    return out
