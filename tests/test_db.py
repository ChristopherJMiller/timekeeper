import datetime as dt
import sqlite3

import pytest

from worklog import db


def _db_path(tmp_path):
    return tmp_path / "state.db"


def test_start_and_close_session(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        sid = db.start_session(
            con,
            started_at=db.iso_utc(),
            client_id=None,
            note="initial",
            tags=["acme"],
        )
        assert sid > 0
        assert db.active_session(con) is not None

        db.close_session(
            con,
            session_id=sid,
            stopped_at=db.iso_utc(),
            duration_s=600,
            summary_path="/tmp/x.md",
        )
        assert db.active_session(con) is None


def test_only_one_active_session(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        db.start_session(
            con, started_at=db.iso_utc(), client_id=None, note=None, tags=None
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.start_session(
                con, started_at=db.iso_utc(), client_id=None, note=None, tags=None
            )


def test_abandon_and_restart(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        db.start_session(
            con, started_at=db.iso_utc(), client_id=None, note=None, tags=None
        )
        assert db.abandon_active(con) == 1
        # abandoning must free up the partial-unique constraint
        db.start_session(
            con, started_at=db.iso_utc(), client_id=None, note=None, tags=None
        )
        assert db.active_session(con) is not None


def test_upsert_and_list_clients(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        cid = db.upsert_client(con, "acme", hours_budget_weekly=10, rate=150)
        db.upsert_client(con, "acme", rate=175)  # update
        rows = db.list_clients(con)
        assert len(rows) == 1
        assert rows[0]["rate"] == 175
        assert rows[0]["hours_budget_weekly"] == 10
        assert rows[0]["id"] == cid


def test_sessions_in_range_filters_by_client(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        a = db.upsert_client(con, "acme", hours_budget_weekly=10)
        b = db.upsert_client(con, "beta")
        t0 = dt.datetime(2026, 4, 20, 9, 0, tzinfo=dt.timezone.utc)

        for client_id, offset in [(a, 0), (a, 1), (b, 2)]:
            sid = db.start_session(
                con,
                started_at=(t0 + dt.timedelta(hours=offset)).isoformat(),
                client_id=client_id,
                note=None,
                tags=None,
            )
            db.close_session(
                con,
                session_id=sid,
                stopped_at=(t0 + dt.timedelta(hours=offset, minutes=30)).isoformat(),
                duration_s=1800,
                summary_path=None,
            )

        window_start = t0.isoformat()
        window_end = (t0 + dt.timedelta(days=1)).isoformat()

        all_ = db.sessions_in_range(con, window_start, window_end)
        assert len(all_) == 3

        only_a = db.sessions_in_range(con, window_start, window_end, client_id=a)
        assert {r["client_name"] for r in only_a} == {"acme"}


def test_update_session_recomputes_duration(tmp_path):
    with db.connect(_db_path(tmp_path)) as con:
        sid = db.start_session(
            con,
            started_at="2026-04-20T09:00:00+00:00",
            client_id=None,
            note=None,
            tags=None,
        )
        db.close_session(
            con,
            session_id=sid,
            stopped_at="2026-04-20T10:00:00+00:00",
            duration_s=3600,
            summary_path=None,
        )
        db.update_session(
            con,
            sid,
            stopped_at="2026-04-20T11:30:00+00:00",
        )
        row = db.get_session(con, sid)
        assert row["duration_s"] == 2.5 * 3600
