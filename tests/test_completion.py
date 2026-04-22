"""Shell-completion callbacks + `tk completion` command."""
from __future__ import annotations

import textwrap

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
    return {"output_dir": output_dir, "data_dir": data_dir}


def _seed(cfg_env):
    cfg = config.load()
    with db.connect(cfg.db_path) as con:
        cid = db.upsert_client(con, name="Acme", hours_budget_weekly=10.0)
        db.upsert_client(con, name="Beta")
        db.upsert_client(con, name="Gamma", active=False)
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
            summary_path="",
        )
    (cfg_env["output_dir"] / "weekly").mkdir(parents=True, exist_ok=True)
    (cfg_env["output_dir"] / "weekly" / "2026-W17.md").write_text("# wk\n")
    (cfg_env["output_dir"] / "weekly" / "2026-W16.md").write_text("# wk\n")
    return sid


def test_complete_client_names_prefix_filter(cfg_env):
    _seed(cfg_env)
    out = cli._complete_client_names(None, None, "A")
    assert [i.value for i in out] == ["Acme"]
    out = cli._complete_client_names(None, None, "")
    # Archived clients still appear so users can reassign onto them.
    assert set(i.value for i in out) == {"Acme", "Beta", "Gamma"}


def test_complete_session_ids_shows_metadata(cfg_env):
    sid = _seed(cfg_env)
    out = cli._complete_session_ids(None, None, "")
    assert [i.value for i in out] == [str(sid)]
    assert "Acme" in out[0].help
    assert "investigate" in out[0].help


def test_complete_weeks_sorted_desc(cfg_env):
    _seed(cfg_env)
    out = cli._complete_weeks(None, None, "2026")
    assert [i.value for i in out] == ["2026-W17", "2026-W16"]


def test_complete_callbacks_swallow_errors(monkeypatch, tmp_path):
    # Config unset + no env → load() raises. Callbacks must not propagate.
    monkeypatch.setenv("WORKLOG_CONFIG", str(tmp_path / "missing.toml"))
    assert cli._complete_client_names(None, None, "") == []
    assert cli._complete_session_ids(None, None, "") == []
    assert cli._complete_weeks(None, None, "") == []


def test_completion_command_emits_bash_script(cfg_env):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["completion", "bash"])
    assert r.exit_code == 0
    assert "_tk_completion" in r.output
    assert "complete " in r.output


def test_completion_command_rejects_unknown_shell(cfg_env):
    runner = CliRunner()
    r = runner.invoke(cli.cli, ["completion", "tcsh"])
    assert r.exit_code != 0
