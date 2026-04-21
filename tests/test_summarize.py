import datetime as dt

from worklog import summarize
from worklog.collect_claude import ClaudeRecord
from worklog.collect_git import Commit, GitEvidence, Wip


def _ctx(tags=None):
    return summarize.SessionContext(
        client_name="Acme",
        started=dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.timezone.utc),
        stopped=dt.datetime(2026, 4, 21, 10, 30, tzinfo=dt.timezone.utc),
        note="investigate timeouts",
        tags=tags or ["focus"],
    )


def _git_with_secrets() -> GitEvidence:
    return GitEvidence(
        commits=[
            Commit(
                repo="acme",
                sha="abc1234567",
                author="Dev <dev@example.com>",
                date="2026-04-21T09:15:00+00:00",
                subject="wire API",
                body="",
                stat="",
                lines_changed=12,
                patch=(
                    "+ use token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "+ touched .env config\n"
                ),
            )
        ],
        wip=[
            Wip(
                repo="acme",
                branch="main",
                status="M secrets/creds.json",
                diffstat="secrets/creds.json | 1",
                diff="--- a/secrets/creds.json\n+++ b/secrets/creds.json",
            )
        ],
    )


def test_render_raw_session_applies_path_redaction():
    out = summarize.render_raw_session(
        _ctx(), _git_with_secrets(), [], exclude_paths=[".env", "secrets/"]
    )
    assert "[REDACTED:path:.env]" in out
    assert "[REDACTED:path:secrets/]" in out
    # Literal sensitive paths should no longer be present.
    assert "secrets/creds.json" not in out


def test_render_raw_session_without_exclude_paths_still_scrubs_secrets():
    out = summarize.render_raw_session(_ctx(), _git_with_secrets(), [])
    # Path mentions pass through when no exclude_paths configured,
    # but env-secret scrubbing still runs.
    assert "[REDACTED:github_token]" in out
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in out


def test_render_raw_session_header_fields():
    out = summarize.render_raw_session(_ctx(tags=["a", "b"]), GitEvidence(), [])
    assert "client: Acme" in out
    assert "duration_min: 90" in out
    assert "tags: a, b" in out
    assert "# Session (raw)" in out


def test_build_session_user_prompt_section_order():
    claude = [
        ClaudeRecord(
            timestamp=dt.datetime(2026, 4, 21, 9, 30, tzinfo=dt.timezone.utc),
            cwd="/tmp/acme",
            session_id="abc",
            prompt_count=4,
            duration_s=600,
            files=["src/a.py", "src/b.py"],
        )
    ]
    out = summarize.build_session_user_prompt(
        _ctx(), _git_with_secrets(), claude, exclude_paths=[]
    )
    evidence_idx = out.index("# Evidence")
    commits_idx = out.index("## Commits")
    wip_idx = out.index("## WIP")
    claude_idx = out.index("### Claude Code activity")
    assert evidence_idx < commits_idx < wip_idx < claude_idx


def test_build_session_user_prompt_empty_git():
    out = summarize.build_session_user_prompt(
        _ctx(), GitEvidence(), [], exclude_paths=[]
    )
    assert "_No commits in window._" in out
    assert "_No uncommitted changes._" in out
    assert "### Claude Code activity" not in out
