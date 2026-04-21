import os
from pathlib import Path

import pytest

from worklog import config


VALID_BODY = """\
author = "dev@example.com"
output_dir = "{out}"
repos = []
"""


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_default_config_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("WORKLOG_CONFIG", raising=False)
    assert config.default_config_path() == (
        Path.home() / ".config" / "worklog" / "config.toml"
    )


def test_default_data_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))
    assert config.default_data_dir() == tmp_path / "data"


def test_load_uses_env_override_paths(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    out_dir = tmp_path / "out"
    data_dir = tmp_path / "data"
    _write(cfg_path, VALID_BODY.format(out=out_dir))
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(data_dir))

    cfg = config.load()
    assert cfg.path == cfg_path
    assert cfg.output_dir == out_dir
    assert cfg.data_dir == data_dir
    assert cfg.sessions_dir == out_dir / "sessions"
    assert cfg.weekly_dir == out_dir / "weekly"
    assert cfg.db_path == data_dir / "state.db"
    assert cfg.hooks_log_path == data_dir / "hooks.jsonl"
    assert cfg.sessions_dir.is_dir()
    assert cfg.weekly_dir.is_dir()


def test_load_creates_template_when_missing(monkeypatch, tmp_path):
    cfg_path = tmp_path / "missing.toml"
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))

    with pytest.raises(ValueError, match="Set `author`"):
        config.load()
    assert cfg_path.exists()
    assert "author = \"you@example.com\"" in cfg_path.read_text()


def test_load_privacy_overrides(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    out_dir = tmp_path / "out"
    body = VALID_BODY.format(out=out_dir) + (
        "\n[privacy]\n"
        "redact_secrets = false\n"
        'exclude_paths = ["private/", "creds.json"]\n'
    )
    _write(cfg_path, body)
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))

    cfg = config.load()
    assert cfg.privacy.redact_secrets is False
    assert cfg.privacy.exclude_paths == ["private/", "creds.json"]


def test_load_privacy_defaults_when_absent(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    out_dir = tmp_path / "out"
    _write(cfg_path, VALID_BODY.format(out=out_dir))
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))

    cfg = config.load()
    assert cfg.privacy.redact_secrets is True
    assert cfg.privacy.exclude_paths == [".env", "secrets/"]


def test_load_rejects_template_author(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(config.DEFAULT_CONFIG_TEMPLATE)
    monkeypatch.setenv("WORKLOG_CONFIG", str(cfg_path))
    monkeypatch.setenv("WORKLOG_DATA", str(tmp_path / "data"))

    with pytest.raises(ValueError, match="Set `author`"):
        config.load()
