"""Session notes: DB, CLI, markdown rewrite."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from worklog import cli, config, db


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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return {"output_dir": output_dir, "data_dir": data_dir}


def _seed_closed(cfg_env) -> tuple[int, Path]:
    cfg = config.load()
    md = cfg_env["output_dir"] / "sessions" / "seed.md"
    md.write_text("# Session\nbody here\n")
    with db.connect(cfg.db_path) as con:
        sid = db.start_session(
            con, started_at="2026-04-21T09:00:00+00:00",
            client_id=None, note="n", tags=None,
        )
        db.close_session(
            con, session_id=sid, stopped_at="2026-04-21T10:00:00+00:00",
            duration_s=3600, summary_path=str(md),
        )
    return sid, md


def _seed_active(cfg_env) -> int:
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        return db.start_session(
            con, started_at="2026-04-21T09:00:00+00:00",
            client_id=None, note="live", tags=None,
        )


def test_note_add_to_active_session(cfg_env):
    sid = _seed_active(cfg_env)
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["note", "add", "called Bob at 10am re: deploy"])
    assert r.exit_code == 0, r.output
    assert f"session {sid}" in r.output
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        rows = db.list_notes(con, sid)
    assert len(rows) == 1
    assert "called Bob" in rows[0]["text"]


def test_note_add_requires_active_or_session_id(cfg_env):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["note", "add", "orphan note"])
    assert r.exit_code != 0
    assert "No active session" in r.output


def test_note_add_to_closed_session_appends_to_markdown(cfg_env):
    sid, md = _seed_closed(cfg_env)
    runner = CliRunner()
    r = runner.invoke(
        cli.cli,
        ["note", "add", "also did planning call", "--session", str(sid)],
    )
    assert r.exit_code == 0, r.output
    body = md.read_text()
    assert "## Additional notes" in body
    assert "also did planning call" in body


def test_note_rm_rewrites_markdown(cfg_env):
    sid, md = _seed_closed(cfg_env)
    runner = CliRunner()
    runner.invoke(cli.cli, ["note", "add", "first", "--session", str(sid)])
    runner.invoke(cli.cli, ["note", "add", "second", "--session", str(sid)])
    assert "first" in md.read_text() and "second" in md.read_text()

    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        rows = db.list_notes(con, sid)
    r = runner.invoke(cli.cli, ["note", "rm", str(rows[0]["id"])])
    assert r.exit_code == 0
    body = md.read_text()
    assert "first" not in body
    assert "second" in body


def test_note_list_reports_contents(cfg_env):
    sid = _seed_active(cfg_env)
    runner = CliRunner()
    runner.invoke(cli.cli, ["note", "add", "aaa"])
    runner.invoke(cli.cli, ["note", "add", "bbb"])
    r = runner.invoke(cli.cli, ["note", "list"])
    assert r.exit_code == 0
    assert "aaa" in r.output and "bbb" in r.output


def test_note_empty_text_rejected(cfg_env):
    _seed_active(cfg_env)
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["note", "add", "   "])
    assert r.exit_code != 0
    assert "empty" in r.output


def test_rewrite_notes_section_idempotent(cfg_env):
    """Adding then removing all notes should restore the original body."""
    sid, md = _seed_closed(cfg_env)
    original = md.read_text()
    runner = CliRunner()
    runner.invoke(cli.cli, ["note", "add", "x", "--session", str(sid)])
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        rows = db.list_notes(con, sid)
    runner.invoke(cli.cli, ["note", "rm", str(rows[0]["id"])])
    # Trailing whitespace is allowed to differ by one newline.
    assert md.read_text().rstrip() == original.rstrip()


def test_show_includes_notes_for_active_session(cfg_env):
    sid = _seed_active(cfg_env)
    runner = CliRunner()
    runner.invoke(cli.cli, ["note", "add", "phone call notes"])
    r = runner.invoke(cli.cli, ["show", str(sid)])
    assert r.exit_code == 0
    assert "phone call notes" in r.output


def test_notes_propagate_into_session_context_on_stop(cfg_env, monkeypatch):
    """Verify notes stored during an active session land in the raw summary."""
    sid = _seed_active(cfg_env)
    runner = CliRunner()
    runner.invoke(cli.cli, ["note", "add", "led architecture review call"])
    # --no-summary avoids the LLM; we check the raw markdown for the note.
    r = runner.invoke(cli.cli, ["stop", "--no-summary", "--force"])
    assert r.exit_code == 0, r.output
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        row = db.get_session(con, sid)
    assert row["status"] == "closed"
    body = Path(row["summary_path"]).read_text()
    assert "led architecture review call" in body
    assert "Manual notes" in body
