"""Config loading and path resolution.

Single source of truth for every filesystem path the tool touches. Uses
XDG-ish defaults without pulling in platformdirs — one file, edited by hand.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_config_path() -> Path:
    override = os.environ.get("WORKLOG_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "worklog" / "config.toml"


def default_data_dir() -> Path:
    override = os.environ.get("WORKLOG_DATA")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "worklog"

DEFAULT_CONFIG_TEMPLATE = """\
# worklog config — edit by hand
author = "you@example.com"                  # matches git commit author email
output_dir = "~/worklog"                    # session + weekly markdown lives here

# LLM provider — defaults to OpenRouter (OpenAI-compatible).
api_base_url  = "https://openrouter.ai/api/v1"
session_model = "anthropic/claude-haiku-4.5"
weekly_model  = "anthropic/claude-sonnet-4.5"

# Optional. Sent as HTTP-Referer / X-Title on OpenRouter for attribution.
app_name = "timekeeper"
app_url  = ""

repos = [
    # "~/code/acme-backend",
]

[privacy]
redact_secrets = true
exclude_paths = [".env", "secrets/"]
"""


@dataclass
class PrivacyConfig:
    redact_secrets: bool = True
    exclude_paths: list[str] = field(default_factory=lambda: [".env", "secrets/"])


@dataclass
class Config:
    author: str
    output_dir: Path
    api_base_url: str
    session_model: str
    weekly_model: str
    app_name: str
    app_url: str
    repos: list[Path]
    privacy: PrivacyConfig
    path: Path

    @property
    def sessions_dir(self) -> Path:
        return self.output_dir / "sessions"

    @property
    def weekly_dir(self) -> Path:
        return self.output_dir / "weekly"

    @property
    def data_dir(self) -> Path:
        return default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def hooks_log_path(self) -> Path:
        return self.data_dir / "hooks.jsonl"


def ensure_config(path: Path | None = None) -> Path:
    """Create a template config if missing. Returns the config path."""
    path = path or default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TEMPLATE)
    return path


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    ensure_config(path)
    raw = tomllib.loads(path.read_text())

    author = raw.get("author", "").strip()
    if not author or author == "you@example.com":
        raise ValueError(
            f"Set `author` in {path} to your git commit email before running."
        )

    output_dir = Path(raw.get("output_dir", "~/worklog")).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sessions").mkdir(exist_ok=True)
    (output_dir / "weekly").mkdir(exist_ok=True)
    default_data_dir().mkdir(parents=True, exist_ok=True)

    repos = [Path(p).expanduser() for p in raw.get("repos", [])]

    priv_raw = raw.get("privacy", {})
    privacy = PrivacyConfig(
        redact_secrets=bool(priv_raw.get("redact_secrets", True)),
        exclude_paths=list(priv_raw.get("exclude_paths", [".env", "secrets/"])),
    )

    return Config(
        author=author,
        output_dir=output_dir,
        api_base_url=raw.get("api_base_url", "https://openrouter.ai/api/v1"),
        session_model=raw.get("session_model", "anthropic/claude-haiku-4.5"),
        weekly_model=raw.get("weekly_model", "anthropic/claude-sonnet-4.5"),
        app_name=raw.get("app_name", "timekeeper"),
        app_url=raw.get("app_url", ""),
        repos=repos,
        privacy=privacy,
        path=path,
    )
