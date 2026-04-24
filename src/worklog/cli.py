"""`tk` — CLI entry point for the personal work tracker."""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path

import click
from click.shell_completion import CompletionItem

from . import auth, collect_claude, collect_git, config, db, report, summarize


LONG_SESSION_WARN_HOURS = 4
AUTO_ABANDON_HOURS = 12


# ---------------------------------------------------------------------------
# shell completion helpers
#
# These run inside a forked `tk` process when the user hits <TAB>. They must
# swallow every exception — a raised error would print a stack trace into the
# user's shell. Return [] on any failure and let completion silently no-op.
# ---------------------------------------------------------------------------

def _complete_session_ids(ctx, param, incomplete):
    try:
        cfg = config.load()
        with db.connect(cfg.db_path) as con:
            rows = db.recent_sessions(con, limit=50)
    except Exception:
        return []
    out: list[CompletionItem] = []
    for r in rows:
        sid = str(r["id"])
        if not sid.startswith(incomplete):
            continue
        when = (r["started_at"] or "")[:16].replace("T", " ")
        client = r["client_name"] or "-"
        note = (r["note"] or "").replace("\n", " ")[:40]
        out.append(CompletionItem(sid, help=f"{when} [{client}] {note}".strip()))
    return out


def _complete_client_names(ctx, param, incomplete):
    try:
        cfg = config.load()
        with db.connect(cfg.db_path) as con:
            rows = db.list_clients(con)
    except Exception:
        return []
    return [
        CompletionItem(
            r["name"],
            help="archived" if not r["active"] else "",
        )
        for r in rows
        if r["name"].startswith(incomplete)
    ]


def _complete_weeks(ctx, param, incomplete):
    try:
        cfg = config.load()
        labels = sorted(
            (p.stem for p in cfg.weekly_dir.glob("*-W*.md")),
            reverse=True,
        )
    except Exception:
        return []
    return [CompletionItem(lbl) for lbl in labels if lbl.startswith(incomplete)]


def _parse_at(at: str | None, reference: dt.datetime) -> dt.datetime:
    """Parse --at HH:MM or ISO8601 timestamp, defaulting to today's date."""
    if at is None:
        return reference
    at = at.strip()
    if re.match(r"^\d{1,2}:\d{2}$", at):
        hh, mm = (int(x) for x in at.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise click.BadParameter(
                f"Bad --at value {at!r}: hour 0-23, minute 0-59"
            )
        local = reference.astimezone().replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        return local.astimezone(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(at)
    except ValueError as e:
        raise click.BadParameter(f"Bad --at value {at!r}: {e}") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.timezone.utc)


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _resolve_client(con: sqlite3.Connection, name: str | None) -> int | None:
    if not name:
        return None
    row = db.get_client_by_name(con, name)
    if row is None:
        raise click.ClickException(
            f"Unknown client {name!r}. Run `tk clients add {name}` first."
        )
    return row["id"]


@click.group()
def cli():
    """tk — turn commits + Claude Code sessions into weekly impact reports."""


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--client", "-c", default=None,
    help="Client name (see `tk clients`)",
    shell_complete=_complete_client_names,
)
@click.option("--note", "-n", default=None, help="What you plan to work on")
@click.option("--tag", "-t", multiple=True, help="Context tags; repeatable")
@click.option("--at", default=None, help="Backfill start time (HH:MM or ISO)")
def start(client, note, tag, at):
    """Begin a work session."""
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    started_at = _parse_at(at, now)
    with db.connect(cfg.db_path) as con:
        existing = db.active_session(con)
        if existing is not None:
            existing_started = dt.datetime.fromisoformat(existing["started_at"])
            age_h = (now - existing_started).total_seconds() / 3600
            if age_h >= AUTO_ABANDON_HOURS and click.confirm(
                f"Active session started {age_h:.0f}h ago looks stale. "
                f"Abandon it?",
                default=True,
            ):
                db.abandon_active(con)
            else:
                raise click.ClickException(
                    f"Already active since {existing['started_at']}. "
                    f"Run `tk stop` or `tk abandon` first."
                )
        client_id = _resolve_client(con, client)
        try:
            db.start_session(
                con,
                started_at=started_at.isoformat(),
                client_id=client_id,
                note=note,
                tags=list(tag) if tag else None,
            )
        except sqlite3.IntegrityError:
            row = db.active_session(con)
            raise click.ClickException(
                f"Already active since {row['started_at']}. "
                f"Run `tk stop` or `tk abandon` first."
            ) from None
    click.echo(
        f"▶ started at {started_at.isoformat()}"
        + (f" — {note}" if note else "")
        + (f" [{client}]" if client else "")
    )


@cli.command()
@click.option("--at", default=None, help="Backfill stop time (HH:MM or ISO)")
@click.option("--no-summary", is_flag=True, help="Skip LLM call")
@click.option("--model", default=None, help="Override session_model")
@click.option("--force", is_flag=True, help="Skip long-session guard")
def stop(at, no_summary, model, force):
    """End the active session and write its markdown summary."""
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    stopped_at = _parse_at(at, now)

    with db.connect(cfg.db_path) as con:
        row = db.active_session(con)
        if row is None:
            raise click.ClickException("No active session. Run `tk start` first.")
        started_at = dt.datetime.fromisoformat(row["started_at"])
        duration_s = int((stopped_at - started_at).total_seconds())
        if duration_s < 0:
            raise click.ClickException(
                f"Stop time ({stopped_at.isoformat()}) is before start time "
                f"({started_at.isoformat()})."
            )
        if (
            not force
            and duration_s > LONG_SESSION_WARN_HOURS * 3600
            and not click.confirm(
                f"Session is {_format_duration(duration_s)} long. Still stop?",
                default=True,
            )
        ):
            raise click.Abort()

        client_name = "unassigned"
        if row["client_id"] is not None:
            crow = con.execute(
                "SELECT name FROM clients WHERE id=?", (row["client_id"],)
            ).fetchone()
            if crow:
                client_name = crow["name"]

        git_ev = collect_git.collect(
            cfg.repos, cfg.author, started_at, stopped_at
        )
        claude_recs = collect_claude.records_in_window(
            cfg.hooks_log_path, started_at, stopped_at
        )

        note_rows = db.list_notes(con, row["id"])
        manual_notes = [(n["added_at"], n["text"]) for n in note_rows]

        ctx = summarize.SessionContext(
            client_name=client_name,
            started=started_at,
            stopped=stopped_at,
            note=row["note"],
            tags=[t for t in (row["tags"] or "").split(",") if t],
            manual_notes=manual_notes or None,
        )

        if no_summary:
            md = summarize.render_raw_session(
                ctx, git_ev, claude_recs, cfg.privacy.exclude_paths
            )
        else:
            try:
                md = summarize.summarize_session(
                    cfg,
                    ctx,
                    git_ev,
                    claude_recs,
                    model=model,
                )
            except Exception as e:
                click.echo(
                    f"! LLM call failed ({e}). Falling back to raw evidence.",
                    err=True,
                )
                md = summarize.render_raw_session(
                    ctx, git_ev, claude_recs, cfg.privacy.exclude_paths
                )

        fname = started_at.strftime("%Y-%m-%d-%H%M") + ".md"
        path = cfg.sessions_dir / fname
        path.write_text(md + "\n")

        db.close_session(
            con,
            session_id=row["id"],
            stopped_at=stopped_at.isoformat(),
            duration_s=duration_s,
            summary_path=str(path),
        )

    click.echo(f"■ stopped. {_format_duration(duration_s)} → {path}")


# ---------------------------------------------------------------------------
# status / list / show / edit / abandon
# ---------------------------------------------------------------------------

@cli.command()
def status():
    """Show active session and today's totals."""
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    with db.connect(cfg.db_path) as con:
        row = db.active_session(con)
        today_start = dt.datetime.combine(
            now.date(), dt.time.min, dt.timezone.utc
        ).isoformat()
        today_total = con.execute(
            "SELECT COALESCE(SUM(duration_s), 0) AS s FROM sessions "
            "WHERE started_at >= ? AND status='closed'",
            (today_start,),
        ).fetchone()["s"]
    if row is None:
        click.echo(f"idle. today: {_format_duration(today_total)}")
        return
    started = dt.datetime.fromisoformat(row["started_at"])
    live = int((now - started).total_seconds())
    warn = " ⚠ long session" if live > LONG_SESSION_WARN_HOURS * 3600 else ""
    stale_hint = (
        f"\n  ! stale: started {live // 3600}h ago — consider `tk abandon`"
        if live >= AUTO_ABANDON_HOURS * 3600
        else ""
    )
    click.echo(
        f"▶ active {_format_duration(live)}{warn}\n"
        f"  started: {row['started_at']}\n"
        f"  note:    {row['note'] or '-'}\n"
        f"  today closed: {_format_duration(today_total)}"
        f"{stale_hint}"
    )


@cli.command("list")
@click.option("--limit", default=20, show_default=True)
def list_cmd(limit):
    """List recent sessions."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        rows = db.recent_sessions(con, limit=limit)
    if not rows:
        click.echo("no sessions yet")
        return
    for r in rows:
        dur = _format_duration(r["duration_s"] or 0)
        when = r["started_at"][:16].replace("T", " ")
        client = r["client_name"] or "-"
        note = (r["note"] or "").replace("\n", " ")[:48]
        click.echo(f"{r['id']:>4}  {when}  {dur:>6}  {client:<12} {note}")


@cli.command()
@click.argument("session_id", type=int, shell_complete=_complete_session_ids)
def show(session_id):
    """Print a session's markdown summary."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = db.get_session(con, session_id)
        if row is None:
            raise click.ClickException(f"No session {session_id}.")
        notes = db.list_notes(con, session_id)
    if row["summary_path"] and Path(row["summary_path"]).exists():
        click.echo(Path(row["summary_path"]).read_text())
        return
    # Active / abandoned sessions have no summary file — show header + notes.
    click.echo(f"# Session {session_id} ({row['status']})")
    click.echo(f"started: {row['started_at']}")
    if row["note"]:
        click.echo(f"note: {row['note']}")
    if notes:
        click.echo(_render_notes_section(notes))
    elif row["status"] != "closed":
        click.echo("(no summary yet)")


@cli.command()
@click.argument("session_id", type=int, shell_complete=_complete_session_ids)
@click.option("--started", default=None)
@click.option("--stopped", default=None)
@click.option(
    "--client", default=None, shell_complete=_complete_client_names,
)
@click.option("--note", default=None)
@click.option(
    "--tui/--no-tui", default=None,
    help="Force TUI on or off (default: TUI when no field flags are given).",
)
def edit(session_id, started, stopped, client, note, tui):
    """Edit a session's timestamps, client, note, or tags.

    With no field options, launches an interactive TUI. Pass any of
    --started / --stopped / --client / --note to edit non-interactively
    (suitable for scripts).
    """
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    any_flag = any(v is not None for v in (started, stopped, client, note))
    use_tui = tui if tui is not None else not any_flag

    with db.connect(cfg.db_path) as con:
        if db.get_session(con, session_id) is None:
            raise click.ClickException(f"No session {session_id}.")

    if use_tui:
        from . import tui as tui_mod  # lazy: curses import only when needed
        changed = tui_mod.run_edit(cfg, session_id)
        if changed:
            click.echo(f"edited session {session_id}")
        else:
            click.echo(f"no changes to session {session_id}")
        return

    with db.connect(cfg.db_path) as con:
        started_iso = (
            _parse_at(started, now).isoformat() if started is not None else None
        )
        stopped_iso = (
            _parse_at(stopped, now).isoformat() if stopped is not None else None
        )
        client_id = _resolve_client(con, client) if client is not None else None
        db.update_session(
            con,
            session_id,
            started_at=started_iso,
            stopped_at=stopped_iso,
            client_id=client_id,
            note=note,
        )
    click.echo(f"edited session {session_id}")


@cli.command()
def abandon():
    """Discard the active session without summarizing."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        n = db.abandon_active(con)
    if n == 0:
        raise click.ClickException("No active session.")
    click.echo("✗ abandoned active session")


# ---------------------------------------------------------------------------
# notes — capture work not visible in commits (calls, planning, manual steps)
# ---------------------------------------------------------------------------

NOTES_SECTION_HEADER = "## Additional notes"
_NOTES_SECTION_RE = re.compile(
    rf"\n{re.escape(NOTES_SECTION_HEADER)}\n(?:.*)\Z", re.DOTALL
)


def _render_notes_section(rows) -> str:
    """Render the '## Additional notes' block appended to closed-session markdown."""
    if not rows:
        return ""
    lines = ["", NOTES_SECTION_HEADER, ""]
    for r in rows:
        when = r["added_at"][:16].replace("T", " ")
        text = r["text"].replace("\n", " ")
        lines.append(f"- _{when}_ — {text}")
    return "\n".join(lines) + "\n"


def _rewrite_notes_section(path: Path, note_rows) -> None:
    """Replace or append the additional-notes block in the session's md file."""
    if not path.exists():
        return
    body = path.read_text()
    stripped = _NOTES_SECTION_RE.sub("", body).rstrip()
    new_section = _render_notes_section(note_rows)
    if new_section:
        stripped = stripped + "\n" + new_section
    path.write_text(stripped + ("\n" if not stripped.endswith("\n") else ""))


def _resolve_note_target(
    con: sqlite3.Connection, session_id: int | None
) -> sqlite3.Row:
    if session_id is not None:
        row = db.get_session(con, session_id)
        if row is None:
            raise click.ClickException(f"No session {session_id}.")
        return row
    row = db.active_session(con)
    if row is None:
        raise click.ClickException(
            "No active session. Pass --session ID to target a closed one."
        )
    return row


@cli.group("note")
def note_group():
    """Attach free-form notes to a session (calls, planning, manual work)."""


@note_group.command("add")
@click.argument("text")
@click.option(
    "--session", "session_id", type=int, default=None,
    shell_complete=_complete_session_ids,
    help="Target a specific session; defaults to the active one.",
)
def note_add(text, session_id):
    """Add a note to the active session (or --session ID)."""
    text = text.strip()
    if not text:
        raise click.ClickException("empty note — nothing added")
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = _resolve_note_target(con, session_id)
        note_id = db.add_note(con, row["id"], text)
        rows = db.list_notes(con, row["id"])
        if row["status"] == "closed" and row["summary_path"]:
            _rewrite_notes_section(Path(row["summary_path"]), rows)
    click.echo(f"+ note {note_id} → session {row['id']}")


@note_group.command("list")
@click.option(
    "--session", "session_id", type=int, default=None,
    shell_complete=_complete_session_ids,
)
def note_list(session_id):
    """List notes attached to a session."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = _resolve_note_target(con, session_id)
        rows = db.list_notes(con, row["id"])
    if not rows:
        click.echo(f"no notes on session {row['id']}")
        return
    for r in rows:
        when = r["added_at"][:16].replace("T", " ")
        click.echo(f"{r['id']:>4}  {when}  {r['text']}")


@note_group.command("rm")
@click.argument("note_id", type=int)
def note_rm(note_id):
    """Delete a note by ID."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = db.get_note(con, note_id)
        if row is None:
            raise click.ClickException(f"No note {note_id}.")
        session_id = row["session_id"]
        db.delete_note(con, note_id)
        session = db.get_session(con, session_id)
        rows = db.list_notes(con, session_id)
        if session and session["status"] == "closed" and session["summary_path"]:
            _rewrite_notes_section(Path(session["summary_path"]), rows)
    click.echo(f"- note {note_id}")


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

@cli.group()
def clients():
    """Manage clients."""


@clients.command("add")
@click.argument("name")
@click.option("--budget", type=float, default=None, help="Weekly hour budget")
@click.option("--rate", type=float, default=None, help="Hourly rate")
def clients_add(name, budget, rate):
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        db.upsert_client(
            con, name=name, hours_budget_weekly=budget, rate=rate, active=True
        )
    click.echo(f"+ client {name}")


@clients.command("list")
def clients_list():
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        rows = db.list_clients(con)
    if not rows:
        click.echo("no clients. `tk clients add <name>`")
        return
    for r in rows:
        tag = "" if r["active"] else " (archived)"
        budget = f"{r['hours_budget_weekly']:.0f}h/wk" if r["hours_budget_weekly"] else "-"
        rate = f"${r['rate']:.0f}/h" if r["rate"] else "-"
        click.echo(f"{r['id']:>3}  {r['name']:<16}  {budget:<8}  {rate}{tag}")


@clients.command("update")
@click.argument("name", shell_complete=_complete_client_names)
@click.option("--budget", type=float, default=None)
@click.option("--rate", type=float, default=None)
def clients_update(name, budget, rate):
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        if db.get_client_by_name(con, name) is None:
            raise click.ClickException(f"No client {name!r}.")
        db.upsert_client(
            con, name=name, hours_budget_weekly=budget, rate=rate, active=True
        )
    click.echo(f"~ client {name}")


@clients.command("archive")
@click.argument("name", shell_complete=_complete_client_names)
def clients_archive(name):
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = db.get_client_by_name(con, name)
        if row is None:
            raise click.ClickException(f"No client {name!r}.")
        db.upsert_client(con, name=name, active=False)
    click.echo(f"- archived {name}")


# ---------------------------------------------------------------------------
# report / doctor
# ---------------------------------------------------------------------------

@cli.command("hours")
@click.option(
    "--week", default=None, help="ISO week (YYYY-Www); default current",
    shell_complete=_complete_weeks,
)
@click.option(
    "--client", default=None, help="Filter by client name",
    shell_complete=_complete_client_names,
)
def hours_cmd(week, client):
    """Print a week's time breakdown (total, per-client, per-day). No LLM call."""
    cfg = config.load()
    try:
        start_iso, end_iso, monday, sunday = report.iso_week_bounds(week)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    with db.connect(cfg.db_path) as con:
        client_id: int | None = None
        if client:
            row = db.get_client_by_name(con, client)
            if row is None:
                raise click.ClickException(f"Unknown client: {client}")
            client_id = row["id"]
        sessions = db.sessions_in_range(con, start_iso, end_iso, client_id=client_id)
    click.echo(f"# Week of {monday.isoformat()} to {sunday.isoformat()}\n")
    click.echo(report.build_time_breakdown(sessions, monday, client))


@cli.command("report")
@click.option(
    "--week", default=None, help="ISO week (YYYY-Www); default current",
    shell_complete=_complete_weeks,
)
@click.option(
    "--client", default=None, help="Filter by client name",
    shell_complete=_complete_client_names,
)
@click.option("--regenerate", is_flag=True, help="Force re-running the weekly LLM call")
def report_cmd(week, client, regenerate):
    """Produce the weekly boss-ready report."""
    cfg = config.load()
    try:
        path = report.generate_weekly(
            cfg,
            week_label=week,
            client_filter=client,
            regenerate=regenerate,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"✎ {path}")


@cli.command()
@click.option("--install-hook", is_flag=True, help="Install Claude Code Stop hook")
def doctor(install_hook):
    """Verify config, repos, API key, and output paths."""
    cfg = config.load()
    problems: list[str] = []

    if auth.get_api_key() is None:
        problems.append(
            "no API key — set with `tk auth set` or export OPENROUTER_API_KEY"
        )
    if not cfg.repos:
        problems.append(f"no repos listed in {cfg.path}")
    for r in cfg.repos:
        if not r.exists():
            problems.append(f"repo path missing: {r}")
        elif not (r / ".git").exists():
            problems.append(f"not a git repo: {r}")
    if not cfg.sessions_dir.is_dir():
        problems.append(f"sessions dir not writable: {cfg.sessions_dir}")

    click.echo(f"config:    {cfg.path}")
    click.echo(f"db:        {cfg.db_path}")
    click.echo(f"sessions:  {cfg.sessions_dir}")
    click.echo(f"hooks log: {cfg.hooks_log_path}")
    click.echo(f"repos:     {len(cfg.repos)} configured")
    click.echo(f"api base:  {cfg.api_base_url}")
    click.echo(f"api key:   {auth.source()}")

    if install_hook:
        _install_hook(cfg.hooks_log_path)

    if problems:
        click.echo("\nproblems:")
        for p in problems:
            click.echo(f"  ✗ {p}")
        sys.exit(1)
    click.echo("\nall checks passed")


def _install_hook(hooks_log: Path) -> None:
    """Write hook script + settings.json snippet instructions."""
    claude_dir = Path.home() / ".claude"
    target = claude_dir / "worklog-stop-hook.sh"
    src = Path(__file__).resolve().parent.parent.parent / "hooks" / "claude-stop-hook.sh"
    if not src.exists():
        click.echo(f"! hook source not found at {src}", err=True)
        return
    claude_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(src.read_text().replace(
        "__HOOKS_LOG__", str(hooks_log)
    ))
    target.chmod(0o755)
    click.echo(f"✓ installed hook to {target}")
    click.echo(
        "\nAdd to ~/.claude/settings.json:\n"
        '  "hooks": { "Stop": [{ "type": "command", "command": '
        f'"{target}" }}] }}'
    )


# ---------------------------------------------------------------------------
# auth (system keychain)
# ---------------------------------------------------------------------------

@cli.group("auth")
def auth_group():
    """Manage the LLM API key (stored in the system keychain)."""


@auth_group.command("set")
@click.option(
    "--value",
    default=None,
    help="Key value. If omitted, you'll be prompted (input hidden).",
)
def auth_set(value):
    """Store the API key in the system keychain."""
    if value is None:
        value = click.prompt("API key", hide_input=True, confirmation_prompt=False)
    value = (value or "").strip()
    if not value:
        raise click.ClickException("empty key — nothing stored")
    try:
        auth.set_api_key(value)
    except Exception as e:
        raise click.ClickException(f"keyring error: {e}") from None
    click.echo(f"✓ stored key in keychain (service={auth.SERVICE}, user={auth.USERNAME})")


@auth_group.command("status")
def auth_status():
    """Show where the API key is currently being read from."""
    src = auth.source()
    click.echo(f"source: {src}")
    key = auth.get_api_key()
    if key:
        prefix = key[:7] if len(key) > 10 else "***"
        click.echo(f"key:    {prefix}… ({len(key)} chars)")
    else:
        click.echo("key:    (none)")


@auth_group.command("clear")
def auth_clear():
    """Remove the API key from the keychain."""
    auth.clear_api_key()
    click.echo("✓ cleared key from keychain")


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------

_COMPLETION_HINTS = {
    "bash": "Add to ~/.bashrc:  eval \"$(tk completion bash)\"",
    "zsh":  "Add to ~/.zshrc:   eval \"$(tk completion zsh)\"",
    "fish": "Save to:           tk completion fish > ~/.config/fish/completions/tk.fish",
}


@cli.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion_cmd(shell):
    """Emit a tab-completion script for the named shell.

    The Nix flake installs these automatically into system completion
    directories. Use this command for pip installs or ad-hoc sourcing.
    """
    from click.shell_completion import shell_complete
    # Delegate to click's internal generator by setting the env var it reads.
    os.environ["_TK_COMPLETE"] = f"{shell}_source"
    try:
        shell_complete(cli, {}, "tk", "_TK_COMPLETE", f"{shell}_source")
    finally:
        os.environ.pop("_TK_COMPLETE", None)
    click.echo(f"\n# {_COMPLETION_HINTS[shell]}", err=True)


if __name__ == "__main__":
    cli()
