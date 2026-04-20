"""Weekly aggregation — reads per-session markdown and asks the LLM once."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from . import db, summarize
from .config import Config


YAML_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def iso_week_bounds(week_label: str | None) -> tuple[str, str, dt.date, dt.date]:
    """Return (start_iso_utc, end_iso_utc, start_date, end_date) for a week.

    week_label is "YYYY-Www" (ISO). None = current week.
    """
    if week_label is None:
        today = dt.date.today()
        iso_year, iso_week, _ = today.isocalendar()
    else:
        m = re.match(r"^(\d{4})-W(\d{1,2})$", week_label)
        if not m:
            raise ValueError(f"Bad week label: {week_label!r}. Use YYYY-Www.")
        iso_year, iso_week = int(m.group(1)), int(m.group(2))
    monday = dt.date.fromisocalendar(iso_year, iso_week, 1)
    next_monday = monday + dt.timedelta(days=7)
    start_iso = dt.datetime.combine(monday, dt.time.min, dt.timezone.utc).isoformat()
    end_iso = dt.datetime.combine(
        next_monday, dt.time.min, dt.timezone.utc
    ).isoformat()
    return start_iso, end_iso, monday, next_monday - dt.timedelta(days=1)


def _hours_line(
    sessions: list, client_filter: str | None
) -> str:
    by_client: dict[str, tuple[float, float | None]] = {}
    for s in sessions:
        name = s["client_name"] or "unassigned"
        hours = (s["duration_s"] or 0) / 3600.0
        budget = s["hours_budget_weekly"]
        prev_h, prev_b = by_client.get(name, (0.0, budget))
        by_client[name] = (prev_h + hours, prev_b if prev_b is not None else budget)
    if client_filter:
        by_client = {k: v for k, v in by_client.items() if k == client_filter}
    parts = []
    for name, (h, b) in sorted(by_client.items()):
        if b:
            parts.append(f"{name}: {h:.1f} / {b:.0f}h")
        else:
            parts.append(f"{name}: {h:.1f}h")
    return " · ".join(parts) if parts else "no sessions"


def _weekly_output_path(cfg: Config, monday: dt.date) -> Path:
    iso_year, iso_week, _ = monday.isocalendar()
    return cfg.weekly_dir / f"{iso_year}-W{iso_week:02d}.md"


def generate_weekly(
    cfg: Config,
    week_label: str | None = None,
    client_filter: str | None = None,
    regenerate: bool = False,
) -> Path:
    start_iso, end_iso, monday, sunday = iso_week_bounds(week_label)
    out_path = _weekly_output_path(cfg, monday)
    if out_path.exists() and not regenerate:
        return out_path

    client_id: int | None = None
    with db.connect(cfg.db_path) as con:
        if client_filter:
            row = db.get_client_by_name(con, client_filter)
            if row is None:
                raise ValueError(f"Unknown client: {client_filter}")
            client_id = row["id"]
        sessions = db.sessions_in_range(con, start_iso, end_iso, client_id=client_id)

    if not sessions:
        out_path.write_text(
            f"# Week of {monday.isoformat()} — no sessions\n\n"
            f"No recorded work between {monday} and {sunday}.\n"
        )
        return out_path

    session_markdowns: list[str] = []
    for s in sessions:
        p = s["summary_path"]
        if p and Path(p).exists():
            session_markdowns.append(Path(p).read_text())

    hours_line = _hours_line(sessions, client_filter)
    week_label_out = f"{monday.isoformat()} to {sunday.isoformat()}"

    try:
        body = summarize.summarize_weekly(
            cfg,
            week_label=week_label_out,
            session_markdowns=session_markdowns,
            hours_line=hours_line,
        )
    except Exception as e:  # API down, key missing — still produce an artifact
        body = (
            f"# Week of {monday.isoformat()} — LLM summary unavailable\n\n"
            f"{hours_line}\n\n"
            f"_Weekly rollup could not be generated ({e}). "
            f"Session summaries below:_\n\n"
            + "\n\n---\n\n".join(session_markdowns)
        )

    out_path.write_text(body + "\n")
    return out_path
