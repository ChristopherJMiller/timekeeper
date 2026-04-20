import datetime as dt
import json

from worklog import collect_claude


def test_returns_empty_when_log_missing(tmp_path):
    recs = collect_claude.records_in_window(
        tmp_path / "nope.jsonl",
        dt.datetime(2026, 4, 20, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 21, tzinfo=dt.timezone.utc),
    )
    assert recs == []


def test_parses_records_in_window(tmp_path):
    path = tmp_path / "hooks.jsonl"
    t0 = dt.datetime(2026, 4, 20, 10, 0, tzinfo=dt.timezone.utc)
    t_before = (t0 - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    t_in = (t0 + dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    t_after = (t0 + dt.timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    lines = [
        json.dumps({"timestamp": t_before, "cwd": "/a", "session_id": "s1",
                    "files": ["x.py"], "prompt_count": 3, "duration_s": 120}),
        json.dumps({"timestamp": t_in, "cwd": "/b", "session_id": "s2",
                    "files": ["y.py", "z.py"], "prompt_count": 7, "duration_s": 600}),
        json.dumps({"timestamp": t_after, "cwd": "/c", "session_id": "s3",
                    "files": [], "prompt_count": 1, "duration_s": 60}),
        "",  # blank line tolerated
        "{not-json",  # garbage tolerated
    ]
    path.write_text("\n".join(lines) + "\n")

    recs = collect_claude.records_in_window(
        path,
        t0,
        t0 + dt.timedelta(hours=1),
    )
    assert len(recs) == 1
    assert recs[0].session_id == "s2"
    assert recs[0].prompt_count == 7
    assert recs[0].files == ["y.py", "z.py"]
