"""Click-level tests for every `tk` subcommand we didn't already cover."""
from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from worklog import auth, cli, config, db


@pytest.fixture
def isolated_cli(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    output_dir = tmp_path / "worklog"
    data_dir = tmp_path / "data"
    cfg_path.write_text(textwrap.dedent(f"""
        author = "dev@example.com"
        output_dir = "{output_dir}"
        api_base_url = "https://example.invalid/v1"
        session_model = "m-s"
        weekly_model  = "m-w"
        repos = []

        [privacy]
        redact_secrets = true
        exclude_paths = []
    """).strip())
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(data_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-abcdef")
    return {"output_dir": output_dir, "data_dir": data_dir, "cfg_path": cfg_path}


# ---------------------------------------------------------------------------
# _parse_at
# ---------------------------------------------------------------------------

def test_parse_at_rejects_out_of_range_hhmm():
    ref = dt.datetime(2026, 4, 21, 12, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(click.BadParameter):
        cli._parse_at("25:99", ref)


def test_parse_at_accepts_iso_and_hhmm():
    ref = dt.datetime(2026, 4, 21, 12, 0, tzinfo=dt.timezone.utc)
    iso = cli._parse_at("2026-04-21T09:00:00+00:00", ref)
    assert iso.hour == 9 and iso.tzinfo is dt.timezone.utc
    hhmm = cli._parse_at("09:30", ref)
    assert hhmm.tzinfo is dt.timezone.utc


# ---------------------------------------------------------------------------
# list / show / edit / abandon
# ---------------------------------------------------------------------------

def _seed_closed_session(data_dir: Path, output_dir: Path) -> int:
    (output_dir / "sessions").mkdir(parents=True, exist_ok=True)
    md = output_dir / "sessions" / "seed.md"
    md.write_text("# seeded\nbody\n")
    with db.connect(data_dir / "state.db") as con:
        cid = db.upsert_client(con, name="Acme")
        sid = db.start_session(
            con,
            started_at="2026-04-21T09:00:00+00:00",
            client_id=cid,
            note="investigate",
            tags=["focus"],
        )
        db.close_session(
            con,
            session_id=sid,
            stopped_at="2026-04-21T10:00:00+00:00",
            duration_s=3600,
            summary_path=str(md),
        )
    return sid


def test_list_show_and_edit(isolated_cli):
    sid = _seed_closed_session(
        isolated_cli["data_dir"], isolated_cli["output_dir"]
    )
    runner = CliRunner()

    r_list = runner.invoke(cli.cli, ["list"])
    assert r_list.exit_code == 0, r_list.output
    assert str(sid) in r_list.output
    assert "Acme" in r_list.output

    r_show = runner.invoke(cli.cli, ["show", str(sid)])
    assert r_show.exit_code == 0
    assert "seeded" in r_show.output

    r_missing = runner.invoke(cli.cli, ["show", "9999"])
    assert r_missing.exit_code != 0
    assert "No session" in r_missing.output

    r_edit = runner.invoke(
        cli.cli, ["edit", str(sid), "--note", "revised"]
    )
    assert r_edit.exit_code == 0
    with db.connect(isolated_cli["data_dir"] / "state.db") as con:
        row = db.get_session(con, sid)
    assert row["note"] == "revised"


def test_hours_cmd_prints_breakdown(isolated_cli):
    _seed_closed_session(isolated_cli["data_dir"], isolated_cli["output_dir"])
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["hours", "--week", "2026-W17"])
    assert r.exit_code == 0, r.output
    assert "Week of 2026-04-20" in r.output
    assert "Time breakdown" in r.output
    assert "Acme: 1.0h" in r.output
    assert "Tue 2026-04-21: 1.0h" in r.output


def test_hours_cmd_unknown_client_errors(isolated_cli):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["hours", "--week", "2026-W17", "--client", "Ghost"])
    assert r.exit_code != 0
    assert "Unknown client" in r.output


def test_hours_cmd_bad_week_label(isolated_cli):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["hours", "--week", "not-a-week"])
    assert r.exit_code != 0
    assert "Bad week label" in r.output


def test_abandon_without_active_session_errors(isolated_cli):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["abandon"])
    assert r.exit_code != 0
    assert "No active session" in r.output


def test_abandon_closes_active(isolated_cli):
    runner = CliRunner()
    r1 = runner.invoke(cli.cli, ["start", "--note", "will abandon"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli.cli, ["abandon"])
    assert r2.exit_code == 0
    with db.connect(isolated_cli["data_dir"] / "state.db") as con:
        assert db.active_session(con) is None


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def test_clients_add_and_list(isolated_cli):
    runner = CliRunner()
    r_add = runner.invoke(
        cli.cli, ["clients", "add", "Acme", "--budget", "10", "--rate", "150"]
    )
    assert r_add.exit_code == 0, r_add.output
    r_list = runner.invoke(cli.cli, ["clients", "list"])
    assert r_list.exit_code == 0
    assert "Acme" in r_list.output
    assert "10h/wk" in r_list.output
    assert "$150/h" in r_list.output


def test_clients_archive(isolated_cli):
    runner = CliRunner()
    runner.invoke(cli.cli, ["clients", "add", "Beta"])
    r = runner.invoke(cli.cli, ["clients", "archive", "Beta"])
    assert r.exit_code == 0
    r_list = runner.invoke(cli.cli, ["clients", "list"])
    assert "(archived)" in r_list.output


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_auth_status_reports_env_source(isolated_cli):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["auth", "status"])
    assert r.exit_code == 0
    assert "env:OPENROUTER_API_KEY" in r.output
    assert "test-ke" in r.output  # 7-char prefix shown for keys > 10 chars


def test_auth_round_trip_uses_in_memory_keyring(isolated_cli, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store: dict[tuple[str, str], str] = {}

    class FakeKeyring:
        def set_password(self, service, username, value):
            store[(service, username)] = value

        def get_password(self, service, username):
            return store.get((service, username))

        def delete_password(self, service, username):
            store.pop((service, username), None)

    monkeypatch.setattr(auth, "_keyring", lambda: FakeKeyring())

    runner = CliRunner()
    r_set = runner.invoke(cli.cli, ["auth", "set", "--value", "sk-secret"])
    assert r_set.exit_code == 0, r_set.output
    assert store[(auth.SERVICE, auth.USERNAME)] == "sk-secret"

    r_status = runner.invoke(cli.cli, ["auth", "status"])
    assert "keychain" in r_status.output

    r_clear = runner.invoke(cli.cli, ["auth", "clear"])
    assert r_clear.exit_code == 0
    assert store == {}


# ---------------------------------------------------------------------------
# auto-abandon
# ---------------------------------------------------------------------------

def _seed_stale_active(data_dir: Path, hours_ago: int) -> int:
    started = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    ).isoformat()
    with db.connect(data_dir / "state.db") as con:
        return db.start_session(
            con,
            started_at=started,
            client_id=None,
            note=f"stale {hours_ago}h",
            tags=None,
        )


def test_auto_abandon_yes_starts_new_session(isolated_cli):
    _seed_stale_active(isolated_cli["data_dir"], hours_ago=13)
    runner = CliRunner()
    r = runner.invoke(
        cli.cli, ["start", "--note", "fresh"], input="y\n"
    )
    assert r.exit_code == 0, r.output
    with db.connect(isolated_cli["data_dir"] / "state.db") as con:
        rows = list(con.execute(
            "SELECT status, note FROM sessions ORDER BY id"
        ))
    statuses = {r["status"] for r in rows}
    assert statuses == {"active", "abandoned"}
    active = [r for r in rows if r["status"] == "active"][0]
    assert active["note"] == "fresh"


def test_auto_abandon_no_aborts(isolated_cli):
    _seed_stale_active(isolated_cli["data_dir"], hours_ago=13)
    runner = CliRunner()
    r = runner.invoke(
        cli.cli, ["start", "--note", "fresh"], input="n\n"
    )
    assert r.exit_code != 0
    assert "Already active" in r.output
    with db.connect(isolated_cli["data_dir"] / "state.db") as con:
        rows = list(con.execute("SELECT status FROM sessions"))
    # Only the stale one; it's still active, no new session was created.
    assert len(rows) == 1 and rows[0]["status"] == "active"


def test_fresh_active_session_blocks_start_without_prompt(isolated_cli):
    _seed_stale_active(isolated_cli["data_dir"], hours_ago=1)
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["start", "--note", "fresh"])
    assert r.exit_code != 0
    assert "Already active" in r.output
    with db.connect(isolated_cli["data_dir"] / "state.db") as con:
        rows = list(con.execute("SELECT status FROM sessions"))
    assert len(rows) == 1 and rows[0]["status"] == "active"


def test_status_shows_stale_hint(isolated_cli):
    _seed_stale_active(isolated_cli["data_dir"], hours_ago=13)
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["status"])
    assert r.exit_code == 0
    assert "stale" in r.output
    assert "tk abandon" in r.output
