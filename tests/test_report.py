import datetime as dt
from pathlib import Path

import pytest

from worklog import config, db, report


def _make_config(tmp_path: Path) -> config.Config:
    return config.Config(
        author="dev@example.com",
        output_dir=tmp_path / "out",
        api_base_url="https://example.invalid/v1",
        session_model="m-s",
        weekly_model="m-w",
        app_name="tk-test",
        app_url="",
        repos=[],
        privacy=config.PrivacyConfig(),
        path=tmp_path / "config.toml",
    )


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))
    c = _make_config(tmp_path)
    c.sessions_dir.mkdir(parents=True, exist_ok=True)
    c.weekly_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return c


def test_iso_week_bounds_matches_iso_calendar():
    # 2026-W17 → Mon 2026-04-20
    start, end, monday, sunday = report.iso_week_bounds("2026-W17")
    assert monday == dt.date(2026, 4, 20)
    assert sunday == dt.date(2026, 4, 26)
    assert start.startswith("2026-04-20T00:00:00")
    assert end.startswith("2026-04-27T00:00:00")


def test_iso_week_bounds_rejects_bad_label():
    with pytest.raises(ValueError, match="Bad week label"):
        report.iso_week_bounds("not-a-week")


def test_hours_line_with_and_without_budget():
    rows = [
        {"client_name": "Acme", "duration_s": 3600, "hours_budget_weekly": 10.0},
        {"client_name": "Acme", "duration_s": 1800, "hours_budget_weekly": 10.0},
        {"client_name": "Beta", "duration_s": 900, "hours_budget_weekly": None},
    ]
    line = report._hours_line(rows, client_filter=None)
    assert "Acme: 1.5 / 10h" in line
    assert "Beta: 0.2h" in line
    assert "·" in line


def test_hours_line_client_filter():
    rows = [
        {"client_name": "Acme", "duration_s": 3600, "hours_budget_weekly": None},
        {"client_name": "Beta", "duration_s": 1800, "hours_budget_weekly": None},
    ]
    assert "Beta" not in report._hours_line(rows, client_filter="Acme")
    assert report._hours_line([], client_filter=None) == "no sessions"


def test_generate_weekly_no_sessions_skips_llm(cfg, monkeypatch):
    monkeypatch.setattr(
        "worklog.summarize.summarize_weekly",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM called")),
    )
    out = report.generate_weekly(cfg, week_label="2026-W17")
    text = out.read_text()
    assert out.exists()
    assert "no sessions" in text.lower()


def test_generate_weekly_rollup_with_missing_files(cfg, monkeypatch):
    # Seed two closed sessions inside 2026-W17. One summary file exists, one
    # is missing — the missing one must be silently skipped.
    good_md = cfg.sessions_dir / "good.md"
    good_md.write_text("# good session\nworked on Acme\n")
    ghost_md = cfg.sessions_dir / "ghost.md"

    with db.connect(cfg.db_path) as con:
        acme_id = db.upsert_client(con, name="Acme", hours_budget_weekly=10.0)
        for summary, duration in [
            (str(good_md), 3600),
            (str(ghost_md), 7200),  # file absent on disk
        ]:
            sid = db.start_session(
                con,
                started_at="2026-04-21T10:00:00+00:00",
                client_id=acme_id,
                note=None,
                tags=None,
            )
            db.close_session(
                con,
                session_id=sid,
                stopped_at="2026-04-21T11:00:00+00:00",
                duration_s=duration,
                summary_path=summary,
            )

    captured: dict = {}

    def fake_weekly(cfg, week_label, session_markdowns, hours_line, **_):
        captured["markdowns"] = session_markdowns
        captured["hours"] = hours_line
        return "# Fake weekly\nbody\n"

    monkeypatch.setattr("worklog.summarize.summarize_weekly", fake_weekly)

    out = report.generate_weekly(cfg, week_label="2026-W17")
    assert out.exists()
    assert "Fake weekly" in out.read_text()
    assert len(captured["markdowns"]) == 1
    assert "good session" in captured["markdowns"][0]
    assert "Acme" in captured["hours"]


def test_generate_weekly_regenerate_overwrites(cfg, monkeypatch):
    out_path = cfg.weekly_dir / "2026-W17.md"
    out_path.write_text("stale\n")

    with db.connect(cfg.db_path) as con:
        cid = db.upsert_client(con, name="Acme")
        sid = db.start_session(
            con,
            started_at="2026-04-21T10:00:00+00:00",
            client_id=cid,
            note=None,
            tags=None,
        )
        md = cfg.sessions_dir / "s.md"
        md.write_text("# session\n")
        db.close_session(
            con,
            session_id=sid,
            stopped_at="2026-04-21T11:00:00+00:00",
            duration_s=3600,
            summary_path=str(md),
        )

    # Without --regenerate, existing file is returned unchanged.
    kept = report.generate_weekly(cfg, week_label="2026-W17")
    assert kept.read_text() == "stale\n"

    monkeypatch.setattr(
        "worklog.summarize.summarize_weekly",
        lambda *a, **kw: "# fresh\n",
    )
    refreshed = report.generate_weekly(
        cfg, week_label="2026-W17", regenerate=True
    )
    assert "fresh" in refreshed.read_text()
    assert "stale" not in refreshed.read_text()


def test_generate_weekly_unknown_client_raises(cfg):
    with pytest.raises(ValueError, match="Unknown client"):
        report.generate_weekly(
            cfg, week_label="2026-W17", client_filter="Missing"
        )


def test_build_time_breakdown_totals_and_per_day():
    monday = dt.date(2026, 4, 20)
    rows = [
        {
            "client_name": "Acme",
            "duration_s": 3600,
            "hours_budget_weekly": 10.0,
            "started_at": "2026-04-20T10:00:00+00:00",
        },
        {
            "client_name": "Acme",
            "duration_s": 5400,
            "hours_budget_weekly": 10.0,
            "started_at": "2026-04-22T09:00:00+00:00",
        },
        {
            "client_name": None,
            "duration_s": 1800,
            "hours_budget_weekly": None,
            "started_at": "2026-04-22T14:00:00+00:00",
        },
    ]
    md = report.build_time_breakdown(rows, monday)
    assert "Total:** 3.0h across 3 sessions" in md
    assert "Acme: 2.5h / 10h (25%)" in md
    assert "unassigned: 0.5h" in md
    assert "Mon 2026-04-20: 1.0h" in md
    assert "Wed 2026-04-22: 2.0h" in md
    assert "Tue 2026-04-21: —" in md
    assert "Sun 2026-04-26: —" in md


def test_build_time_breakdown_single_session_and_empty():
    monday = dt.date(2026, 4, 20)
    assert "0 sessions" in report.build_time_breakdown([], monday)
    rows = [
        {
            "client_name": "Solo",
            "duration_s": 7200,
            "hours_budget_weekly": None,
            "started_at": "2026-04-21T08:00:00+00:00",
        }
    ]
    md = report.build_time_breakdown(rows, monday)
    assert "1 session" in md and "1 sessions" not in md


def test_build_time_breakdown_client_filter():
    monday = dt.date(2026, 4, 20)
    rows = [
        {
            "client_name": "Acme",
            "duration_s": 3600,
            "hours_budget_weekly": None,
            "started_at": "2026-04-20T10:00:00+00:00",
        },
        {
            "client_name": "Beta",
            "duration_s": 1800,
            "hours_budget_weekly": None,
            "started_at": "2026-04-20T12:00:00+00:00",
        },
    ]
    md = report.build_time_breakdown(rows, monday, client_filter="Acme")
    assert "Acme: 1.0h" in md
    assert "Beta" not in md
    assert "Total:** 1.0h across 1 session" in md


def test_generate_weekly_includes_breakdown_and_session_detail(cfg, monkeypatch):
    sess_md = cfg.sessions_dir / "s.md"
    sess_md.write_text("# session marker — RAW-CONTENT-XYZ\nwork detail line\n")

    with db.connect(cfg.db_path) as con:
        cid = db.upsert_client(con, name="Acme", hours_budget_weekly=10.0)
        sid = db.start_session(
            con,
            started_at="2026-04-21T10:00:00+00:00",
            client_id=cid,
            note=None,
            tags=None,
        )
        db.close_session(
            con,
            session_id=sid,
            stopped_at="2026-04-21T11:00:00+00:00",
            duration_s=3600,
            summary_path=str(sess_md),
        )

    monkeypatch.setattr(
        "worklog.summarize.summarize_weekly",
        lambda *a, **kw: "# LLM rollup\ncompressed bullet\n",
    )

    text = report.generate_weekly(cfg, week_label="2026-W17").read_text()

    assert "# LLM rollup" in text
    assert "## Time breakdown" in text
    assert "Acme: 1.0h / 10h (10%)" in text
    assert "Tue 2026-04-21: 1.0h" in text
    assert "## Session detail" in text
    assert "RAW-CONTENT-XYZ" in text
    # Rollup stays above the breakdown, which stays above the appendix.
    assert (
        text.index("LLM rollup")
        < text.index("Time breakdown")
        < text.index("Session detail")
    )


def test_generate_weekly_llm_failure_still_includes_breakdown(cfg, monkeypatch):
    sess_md = cfg.sessions_dir / "s.md"
    sess_md.write_text("# raw evidence\n")

    with db.connect(cfg.db_path) as con:
        sid = db.start_session(
            con,
            started_at="2026-04-22T10:00:00+00:00",
            client_id=None,
            note=None,
            tags=None,
        )
        db.close_session(
            con,
            session_id=sid,
            stopped_at="2026-04-22T12:00:00+00:00",
            duration_s=7200,
            summary_path=str(sess_md),
        )

    def boom(*a, **kw):
        raise RuntimeError("no api key")

    monkeypatch.setattr("worklog.summarize.summarize_weekly", boom)
    text = report.generate_weekly(cfg, week_label="2026-W17").read_text()

    assert "LLM summary unavailable" in text
    assert "## Time breakdown" in text
    assert "Wed 2026-04-22: 2.0h" in text
    assert "raw evidence" in text
