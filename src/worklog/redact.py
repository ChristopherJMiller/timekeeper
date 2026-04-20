"""Secret scrubbing before any data crosses the network.

Each pattern is a (name, compiled_regex) pair. Matches are replaced with
`[REDACTED:<name>]` so the LLM sees a placeholder rather than a truncation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern[str]


PATTERNS: tuple[Pattern, ...] = (
    Pattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Pattern(
        "aws_secret",
        re.compile(
            r"(?i)aws(.{0,20})?(secret|private)[_-]?(access)?[_-]?key"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
        ),
    ),
    Pattern("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    Pattern("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Pattern(
        "bearer",
        re.compile(r"(?i)\b(Authorization|Bearer)\s*[:=]?\s*[A-Za-z0-9._\-]{20,}"),
    ),
    Pattern(
        "env_secret",
        re.compile(
            r"(?im)^\s*([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
            r"\s*=\s*[\"']?([^\s\"']+)[\"']?"
        ),
    ),
    Pattern(
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
)


def scrub(text: str) -> str:
    """Replace every pattern match with a named placeholder."""
    if not text:
        return text
    for p in PATTERNS:
        if p.name == "env_secret":
            text = p.regex.sub(lambda m: f"{m.group(1)}=[REDACTED:env_secret]", text)
        else:
            text = p.regex.sub(f"[REDACTED:{p.name}]", text)
    return text


def scrub_path_mentions(text: str, exclude_paths: list[str]) -> str:
    """Replace references to configured sensitive paths."""
    if not text or not exclude_paths:
        return text
    for raw in exclude_paths:
        needle = raw.strip()
        if not needle:
            continue
        text = re.sub(
            re.escape(needle) + r"[^\s]*",
            f"[REDACTED:path:{needle}]",
            text,
        )
    return text


def scrub_all(text: str, exclude_paths: list[str] | None = None) -> str:
    text = scrub(text)
    if exclude_paths:
        text = scrub_path_mentions(text, exclude_paths)
    return text
