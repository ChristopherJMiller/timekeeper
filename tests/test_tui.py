"""TUI pure-logic tests (load_draft + apply). The curses loop itself is
not exercised — we test the persistence layer it calls, not terminal I/O."""
from __future__ import annotations

import textwrap
from dataclasses import replace

import pytest

from worklog import config, db, tui


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
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
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        db.upsert_client(con, name="Acme")
        db.upsert_client(con, name="Beta")
        sid = db.start_session(
            con, started_at="2026-04-21T09:00:00+00:00",
            client_id=None, note="orig", tags=["focus"],
        )
        db.close_session(
            con, session_id=sid, stopped_at="2026-04-21T10:00:00+00:00",
            duration_s=3600, summary_path="",
        )
    return {"cfg": cfg, "session_id": sid}


def test_load_draft_reads_current_state(cfg_env):
    with db.connect(cfg_env["cfg"].db_path) as con:
        draft, clients, notes = tui.load_draft(con, cfg_env["session_id"])
    assert draft.client_name == tui.SENTINEL_UNASSIGNED
    assert draft.note == "orig"
    assert draft.tags == ["focus"]
    assert tui.SENTINEL_UNASSIGNED in clients
    assert "Acme" in clients and "Beta" in clients
    assert notes == []


def test_apply_persists_diff_only(cfg_env):
    sid = cfg_env["session_id"]
    with db.connect(cfg_env["cfg"].db_path) as con:
        draft, _, _ = tui.load_draft(con, sid)
        original = replace(draft, tags=list(draft.tags))
        draft.client_name = "Acme"
        draft.note = "updated"
        draft.tags = ["focus", "billing"]
        changed = tui.apply(con, sid, original, draft)
        assert changed
        row = db.get_session(con, sid)
        crow = con.execute(
            "SELECT name FROM clients WHERE id=?", (row["client_id"],)
        ).fetchone()
    assert crow["name"] == "Acme"
    assert row["note"] == "updated"
    assert sorted(row["tags"].split(",")) == ["billing", "focus"]


def test_apply_unassign_clears_client(cfg_env):
    sid = cfg_env["session_id"]
    # First assign a client, then unassign via apply.
    with db.connect(cfg_env["cfg"].db_path) as con:
        crow = db.get_client_by_name(con, "Acme")
        con.execute("UPDATE sessions SET client_id=? WHERE id=?", (crow["id"], sid))
        draft, _, _ = tui.load_draft(con, sid)
        assert draft.client_name == "Acme"
        original = replace(draft, tags=list(draft.tags))
        draft.client_name = tui.SENTINEL_UNASSIGNED
        changed = tui.apply(con, sid, original, draft)
        assert changed
        row = db.get_session(con, sid)
    assert row["client_id"] is None


def test_apply_no_diff_returns_false(cfg_env):
    sid = cfg_env["session_id"]
    with db.connect(cfg_env["cfg"].db_path) as con:
        draft, _, _ = tui.load_draft(con, sid)
        original = replace(draft, tags=list(draft.tags))
        changed = tui.apply(con, sid, original, draft)
    assert changed is False


def test_apply_unknown_client_raises(cfg_env):
    sid = cfg_env["session_id"]
    with db.connect(cfg_env["cfg"].db_path) as con:
        draft, _, _ = tui.load_draft(con, sid)
        original = replace(draft, tags=list(draft.tags))
        draft.client_name = "NotAClient"
        with pytest.raises(ValueError, match="unknown client"):
            tui.apply(con, sid, original, draft)


def test_apply_timestamp_change_recomputes_duration(cfg_env):
    sid = cfg_env["session_id"]
    with db.connect(cfg_env["cfg"].db_path) as con:
        draft, _, _ = tui.load_draft(con, sid)
        original = replace(draft, tags=list(draft.tags))
        draft.stopped = "2026-04-21T11:00:00+00:00"
        tui.apply(con, sid, original, draft)
        row = db.get_session(con, sid)
    assert row["duration_s"] == 2 * 3600


def test_edit_cli_routes_flags_non_interactively(cfg_env, monkeypatch):
    """Passing field flags must bypass the TUI — tui.run_edit should not run."""
    from click.testing import CliRunner
    from worklog import cli

    called = {"tui": False}

    def fake_run_edit(*a, **kw):
        called["tui"] = True
        return False

    monkeypatch.setattr("worklog.tui.run_edit", fake_run_edit)
    runner = CliRunner()
    r = runner.invoke(
        cli.cli,
        ["edit", str(cfg_env["session_id"]), "--note", "direct"],
    )
    assert r.exit_code == 0, r.output
    assert called["tui"] is False
    with db.connect(cfg_env["cfg"].db_path) as con:
        row = db.get_session(con, cfg_env["session_id"])
    assert row["note"] == "direct"


def test_edit_cli_launches_tui_without_flags(cfg_env, monkeypatch):
    from click.testing import CliRunner
    from worklog import cli

    called = {"tui": False}

    def fake_run_edit(cfg, sid):
        called["tui"] = True
        return True

    monkeypatch.setattr("worklog.tui.run_edit", fake_run_edit)
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["edit", str(cfg_env["session_id"])])
    assert r.exit_code == 0, r.output
    assert called["tui"] is True
    assert "edited session" in r.output
