import datetime as dt
import subprocess
from pathlib import Path

import pytest

from worklog import collect_git


AUTHOR_NAME = "Test Dev"
AUTHOR_EMAIL = "dev@example.com"


def _run(repo: Path, *args: str, env: dict | None = None) -> None:
    full_env = {"GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
                "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
                "HOME": str(repo.parent), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    if env:
        full_env.update(env)
    subprocess.run(("git", *args), cwd=repo, check=True, env=full_env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", AUTHOR_EMAIL)
    _run(repo, "config", "user.name", AUTHOR_NAME)
    return repo


def _commit(repo: Path, filename: str, content: str, msg: str, when: dt.datetime):
    (repo / filename).write_text(content)
    _run(repo, "add", filename)
    iso = when.isoformat()
    env = {"GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso}
    _run(repo, "commit", "-q", "-m", msg, env=env)


def test_commits_filtered_by_window(fake_repo):
    before = dt.datetime(2026, 4, 18, 9, 0, tzinfo=dt.timezone.utc)
    inside = dt.datetime(2026, 4, 20, 10, 0, tzinfo=dt.timezone.utc)
    after = dt.datetime(2026, 4, 22, 14, 0, tzinfo=dt.timezone.utc)

    _commit(fake_repo, "a.txt", "a\n", "early commit", before)
    _commit(fake_repo, "b.txt", "b\n", "in-window commit", inside)
    _commit(fake_repo, "c.txt", "c\n", "later commit", after)

    window_start = dt.datetime(2026, 4, 20, 0, 0, tzinfo=dt.timezone.utc)
    window_end = dt.datetime(2026, 4, 21, 0, 0, tzinfo=dt.timezone.utc)

    commits = collect_git.commits_in_window(
        fake_repo, AUTHOR_EMAIL, window_start, window_end
    )
    subjects = [c.subject for c in commits]
    assert subjects == ["in-window commit"]
    assert commits[0].lines_changed >= 1
    assert commits[0].patch  # small commit should inline the patch


def test_wip_snapshot_none_when_clean(fake_repo):
    _commit(
        fake_repo,
        "x.txt",
        "hi\n",
        "init",
        dt.datetime(2026, 4, 20, tzinfo=dt.timezone.utc),
    )
    assert collect_git.wip_snapshot(fake_repo) is None


def test_wip_snapshot_detects_uncommitted(fake_repo):
    _commit(
        fake_repo,
        "x.txt",
        "one\n",
        "init",
        dt.datetime(2026, 4, 20, tzinfo=dt.timezone.utc),
    )
    (fake_repo / "x.txt").write_text("two\n")
    snap = collect_git.wip_snapshot(fake_repo)
    assert snap is not None
    assert "x.txt" in snap.status
    assert "two" in snap.diff
