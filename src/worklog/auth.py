"""API key resolution via env var or system keychain.

Resolution order for the LLM API key:
  1. $OPENROUTER_API_KEY     (preferred override)
  2. $WORKLOG_API_KEY        (generic fallback)
  3. system keychain         (service=SERVICE, username=USERNAME)

The keyring backend is whatever the platform provides: macOS Keychain,
Windows Credential Locker, or libsecret / kwallet on Linux.
"""
from __future__ import annotations

import os

SERVICE = "timekeeper"
USERNAME = "openrouter"

_ENV_VARS = ("OPENROUTER_API_KEY", "WORKLOG_API_KEY")


class KeyringUnavailable(RuntimeError):
    """Raised when keyring is installed but no backend is functional."""


def _keyring():
    import keyring  # lazy; keyring + backends can be slow to import

    return keyring


def get_api_key() -> str | None:
    """Return the API key from env or keychain, or None if nothing is set."""
    for var in _ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val.strip()
    try:
        return _keyring().get_password(SERVICE, USERNAME)
    except Exception:
        return None


def set_api_key(value: str) -> None:
    if not value:
        raise ValueError("refusing to store an empty API key")
    _keyring().set_password(SERVICE, USERNAME, value)


def clear_api_key() -> None:
    try:
        _keyring().delete_password(SERVICE, USERNAME)
    except Exception:
        pass


def source() -> str:
    """Human-readable description of where the key currently comes from."""
    for var in _ENV_VARS:
        if os.environ.get(var):
            return f"env:{var}"
    try:
        if _keyring().get_password(SERVICE, USERNAME):
            return "keychain"
    except Exception:
        return "keychain-unavailable"
    return "none"


def require_api_key() -> str:
    key = get_api_key()
    if not key:
        raise RuntimeError(
            "No API key found. Set one with `tk auth set`, or export "
            "OPENROUTER_API_KEY."
        )
    return key
