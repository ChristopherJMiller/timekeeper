from worklog import redact


def test_aws_access_key_redacted():
    s = "key=AKIAIOSFODNN7EXAMPLE rest"
    out = redact.scrub(s)
    assert "AKIA" not in out
    assert "[REDACTED:aws_access_key]" in out


def test_github_token_redacted():
    s = "token: ghp_1234567890abcdefghijABCDEFGHIJ1234"
    out = redact.scrub(s)
    assert "ghp_" not in out
    assert "[REDACTED:github_token]" in out


def test_env_secret_keeps_key_name():
    s = "DATABASE_PASSWORD=supersecret123\nOTHER=ok"
    out = redact.scrub(s)
    assert "DATABASE_PASSWORD=[REDACTED:env_secret]" in out
    assert "OTHER=ok" in out


def test_private_key_block_redacted():
    s = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAu...\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    out = redact.scrub(s)
    assert "PRIVATE KEY" not in out
    assert "[REDACTED:private_key_block]" in out
    assert "after" in out


def test_path_mention_redacted():
    s = "diff in .env and src/main.py"
    out = redact.scrub_path_mentions(s, [".env"])
    # The original .env path mention is replaced; the label retains the
    # config key so the LLM knows what kind of thing was redacted.
    assert "[REDACTED:path:.env]" in out
    assert "diff in .env and" not in out
    assert "src/main.py" in out


def test_scrub_all_empty_is_fine():
    assert redact.scrub_all("", []) == ""
    assert redact.scrub_all("hello world", []) == "hello world"
