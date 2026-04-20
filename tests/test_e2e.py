"""End-to-end: tk start → git commit → tk stop with a mocked LLM."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from worklog import auth, cli, config


AUTHOR_EMAIL = "dev@example.com"


def _git(cwd: Path, *args: str, env_extra: dict | None = None) -> None:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "HOME": str(cwd.parent),
    })
    if env_extra:
        env.update(env_extra)
    subprocess.run(("git", *args), cwd=cwd, check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def isolated_worklog(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "worklog"
    repo = tmp_path / "repo"

    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", AUTHOR_EMAIL)
    _git(repo, "config", "user.name", "Dev")

    cfg_path.write_text(textwrap.dedent(f"""
        author = "{AUTHOR_EMAIL}"
        output_dir = "{output_dir}"
        api_base_url = "https://example.invalid/v1"
        session_model = "anthropic/claude-haiku-4.5"
        weekly_model  = "anthropic/claude-sonnet-4.5"
        repos = ["{repo}"]

        [privacy]
        redact_secrets = true
        exclude_paths = []
    """).strip())

    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(data_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    return {"repo": repo, "output_dir": output_dir, "data_dir": data_dir}


class FakeChoice:
    def __init__(self, text):
        self.message = type("M", (), {"content": text})()


class FakeResponse:
    def __init__(self, text):
        self.choices = [FakeChoice(text)]


class FakeCompletions:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._text)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeOpenAIClient:
    def __init__(self, text):
        self.chat = FakeChat(FakeCompletions(text))

    def __call__(self, **kwargs):  # makes OpenAI(...) callable
        return self


def _patch_openai(text: str):
    """Context manager that replaces openai.OpenAI with our fake."""
    fake_text = text

    class _Fake:
        def __init__(self, **kwargs):
            self.chat = FakeChat(FakeCompletions(fake_text))

    fake_module = type(sys)("openai")
    fake_module.OpenAI = _Fake
    return patch.dict(sys.modules, {"openai": fake_module})


def test_start_stop_roundtrip(isolated_worklog):
    repo = isolated_worklog["repo"]
    output_dir = isolated_worklog["output_dir"]

    # Make a commit inside what will be the session window.
    (repo / "hello.py").write_text("print('hi')\n")
    _git(repo, "add", "hello.py")
    _git(repo, "commit", "-q", "-m", "add greeter (p95 lookup 820ms→490ms)")

    runner = CliRunner()
    summary_body = textwrap.dedent("""\
        ---
        client: unassigned
        started: 2026-04-20T09:00:00+00:00
        stopped: 2026-04-20T09:30:00+00:00
        duration_min: 30
        tags:
        ---

        # Session summary

        ## Shipped
        - Greeter lookup p95 820ms→490ms (commit add hello.py)

        ## In progress
        - none

        ## Blockers / decisions needed
        - none

        ## Next
        - none
    """)

    with _patch_openai(summary_body):
        r1 = runner.invoke(cli.cli, ["start", "--note", "smoke test"])
        assert r1.exit_code == 0, r1.output
        r2 = runner.invoke(cli.cli, ["stop"])
        assert r2.exit_code == 0, r2.output

    sessions = list((output_dir / "sessions").glob("*.md"))
    assert len(sessions) == 1
    body = sessions[0].read_text()
    assert "## Shipped" in body
    assert "p95 820ms" in body


def test_no_summary_still_writes_file(isolated_worklog):
    runner = CliRunner()
    r1 = runner.invoke(cli.cli, ["start"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli.cli, ["stop", "--no-summary"])
    assert r2.exit_code == 0, r2.output
    sessions = list((isolated_worklog["output_dir"] / "sessions").glob("*.md"))
    assert len(sessions) == 1
    # Raw fallback header
    assert "# Session (raw)" in sessions[0].read_text()


def test_long_session_warning_requires_confirm(isolated_worklog):
    runner = CliRunner()
    # Start backdated 6 hours ago so stop triggers the long-session guard.
    import datetime as dt

    six_hours_ago = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
    ).isoformat()
    r1 = runner.invoke(cli.cli, ["start", "--at", six_hours_ago])
    assert r1.exit_code == 0, r1.output
    # "no" at the confirm prompt → abort
    r2 = runner.invoke(cli.cli, ["stop", "--no-summary"], input="n\n")
    assert r2.exit_code != 0
