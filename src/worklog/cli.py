"""`tk` — CLI entry point for the personal work tracker."""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path

import click

from . import auth, collect_claude, collect_git, config, db, report, summarize


LONG_SESSION_WARN_HOURS = 4
AUTO_ABANDON_HOURS = 12


def _parse_at(at: str | None, reference: dt.datetime) -> dt.datetime:
    """Parse --at HH:MM or ISO8601 timestamp, defaulting to today's date."""
    if at is None:
        return reference
    at = at.strip()
    if re.match(r"^\d{1,2}:\d{2}$", at):
        hh, mm = (int(x) for x in at.split(":"))
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
@click.option("--client", "-c", default=None, help="Client name (see `tk clients`)")
@click.option("--note", "-n", default=None, help="What you plan to work on")
@click.option("--tag", "-t", multiple=True, help="Context tags; repeatable")
@click.option("--at", default=None, help="Backfill start time (HH:MM or ISO)")
def start(client, note, tag, at):
    """Begin a work session."""
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    started_at = _parse_at(at, now)
    with db.connect(cfg.db_path) as con:
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

        ctx = summarize.SessionContext(
            client_name=client_name,
            started=started_at,
            stopped=stopped_at,
            note=row["note"],
            tags=[t for t in (row["tags"] or "").split(",") if t],
        )

        if no_summary:
            md = summarize.render_raw_session(ctx, git_ev, claude_recs)
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
                md = summarize.render_raw_session(ctx, git_ev, claude_recs)

        fname = started_at.astimezone().strftime("%Y-%m-%d-%H%M") + ".md"
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
    click.echo(
        f"▶ active {_format_duration(live)}{warn}\n"
        f"  started: {row['started_at']}\n"
        f"  note:    {row['note'] or '-'}\n"
        f"  today closed: {_format_duration(today_total)}"
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
@click.argument("session_id", type=int)
def show(session_id):
    """Print a session's markdown summary."""
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = db.get_session(con, session_id)
    if row is None:
        raise click.ClickException(f"No session {session_id}.")
    if not row["summary_path"] or not Path(row["summary_path"]).exists():
        raise click.ClickException(f"No summary file for session {session_id}.")
    click.echo(Path(row["summary_path"]).read_text())


@cli.command()
@click.argument("session_id", type=int)
@click.option("--started", default=None)
@click.option("--stopped", default=None)
@click.option("--client", default=None)
@click.option("--note", default=None)
def edit(session_id, started, stopped, client, note):
    """Edit a session's timestamps, client, or note."""
    cfg = config.load()
    now = dt.datetime.now(dt.timezone.utc)
    with db.connect(cfg.db_path) as con:
        if db.get_session(con, session_id) is None:
            raise click.ClickException(f"No session {session_id}.")
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
@click.argument("name")
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
@click.argument("name")
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

@cli.command("report")
@click.option("--week", default=None, help="ISO week (YYYY-Www); default current")
@click.option("--client", default=None, help="Filter by client name")
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


if __name__ == "__main__":
    cli()
