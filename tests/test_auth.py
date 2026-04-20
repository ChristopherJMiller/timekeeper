import os
from unittest.mock import patch

from worklog import auth


class FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def test_env_var_takes_precedence():
    fake = FakeKeyring()
    fake.set_password(auth.SERVICE, auth.USERNAME, "from-keychain")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-env"}, clear=False):
        with patch.object(auth, "_keyring", return_value=fake):
            assert auth.get_api_key() == "from-env"
            assert auth.source() == "env:OPENROUTER_API_KEY"


def test_keychain_fallback():
    fake = FakeKeyring()
    fake.set_password(auth.SERVICE, auth.USERNAME, "k")
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "WORKLOG_API_KEY")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(auth, "_keyring", return_value=fake):
            assert auth.get_api_key() == "k"
            assert auth.source() == "keychain"


def test_set_and_clear_roundtrip():
    fake = FakeKeyring()
    with patch.object(auth, "_keyring", return_value=fake):
        auth.set_api_key("abc")
        assert fake.store[(auth.SERVICE, auth.USERNAME)] == "abc"
        auth.clear_api_key()
        assert (auth.SERVICE, auth.USERNAME) not in fake.store


def test_require_raises_when_missing():
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "WORKLOG_API_KEY")}
    with patch.dict(os.environ, env, clear=True):
        fake = FakeKeyring()
        with patch.object(auth, "_keyring", return_value=fake):
            try:
                auth.require_api_key()
            except RuntimeError as e:
                assert "No API key" in str(e)
            else:
                raise AssertionError("expected RuntimeError")
