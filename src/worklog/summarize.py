"""LLM summarization — per-session and weekly rollup, via OpenRouter.

We talk to OpenRouter through the OpenAI-compatible Chat Completions API.
The system prompt is sent as a cached text block (`cache_control: ephemeral`)
which OpenRouter forwards to Anthropic-hosted models unchanged, so repeat
calls pay only for the delta.

The API key is resolved by `worklog.auth` — env var or system keychain.
"""
from __future__ import annotations

import datetime as dt
import textwrap
from dataclasses import dataclass

from . import auth, redact
from .collect_claude import ClaudeRecord
from .collect_git import Commit, GitEvidence, Wip
from .config import Config


SESSION_SYSTEM_PROMPT = """\
You are a senior engineer writing a short, impact-framed work log entry for a \
contractor's weekly status report. Your output will be read by the contractor's \
manager, not the contractor.

Rules:
- Lead with OUTCOMES, not activities. "Refactored auth; login p95 820ms→490ms (commit abc123)" \
beats "Worked on auth".
- Name the artifact whenever possible: commit SHA (short), PR number, file path.
- Quantify when the evidence supports it (latency, rows, LOC, errors avoided). Never invent \
numbers that aren't in the evidence.
- Omit activity-only statements ("spent time investigating X") unless they produced a decision \
or blocker worth surfacing.
- If evidence is thin (few commits, mostly WIP), say so plainly — do not pad.
- If a "Manual notes" section is present, treat those notes as primary evidence — the contractor \
added them to capture work that isn't visible in commits (calls, planning, manual deploys).
- Do NOT mention AI tooling, Claude Code, or how the work was done.

Output format (strict):

---
client: <name or "unassigned">
started: <ISO timestamp>
stopped: <ISO timestamp>
duration_min: <integer>
tags: <comma-separated or empty>
---

# Session summary

## Shipped
- <bullet>

## In progress
- <bullet>

## Blockers / decisions needed
- <bullet or "none">

## Next
- <bullet or "none">

Each section: max 5 bullets, max ~150 words total. If a section has no \
content, write "- none".
"""

WEEKLY_SYSTEM_PROMPT = """\
You are a senior engineer consolidating a contractor's per-session logs into a \
single one-page weekly status report for their manager. Your output will be \
emailed or pasted directly into Slack — it must be skimmable in 60 seconds.

Rules:
- Group by CLIENT if multiple are present; within a client, merge related bullets \
across sessions. Do not just concatenate session entries.
- Keep the same four sections: Shipped / In progress / Blockers / Next.
- Lead with impact, name artifacts (PRs, commits, files), quantify from evidence only.
- Open with a one-line HEADER: week dates, total hours, and hours-vs-budget per client.
- Target ≤400 words total.
- Do NOT mention AI tooling, Claude, or session boundaries.

Output format:

# Week of <YYYY-MM-DD> — <summary>

<hours line, e.g. "Acme: 9.2 / 10h · Beta: 3.5 / 5h">

## Shipped
- <bullet> (Acme)
- <bullet> (Beta)

## In progress
- <bullet>

## Blockers / decisions needed
- <bullet or "none">

## Next week
- <bullet>
"""


@dataclass
class SessionContext:
    client_name: str
    started: dt.datetime
    stopped: dt.datetime
    note: str | None
    tags: list[str]
    manual_notes: list[tuple[str, str]] | None = None  # [(added_at_iso, text), …]


def _format_commit(c: Commit) -> str:
    parts = [
        f"### {c.repo}@{c.sha[:10]} — {c.subject}",
        f"(author: {c.author}, date: {c.date}, lines changed: {c.lines_changed})",
    ]
    if c.body:
        parts.append(c.body)
    if c.stat:
        parts.append("```\n" + c.stat + "\n```")
    if c.patch:
        parts.append("```diff\n" + c.patch + "\n```")
    return "\n\n".join(parts)


def _format_wip(w: Wip) -> str:
    parts = [f"### WIP in {w.repo} (branch {w.branch})", "```\n" + w.status + "\n```"]
    if w.diffstat:
        parts.append("```\n" + w.diffstat + "\n```")
    if w.diff:
        parts.append("```diff\n" + w.diff + "\n```")
    return "\n\n".join(parts)


def _format_claude(recs: list[ClaudeRecord]) -> str:
    if not recs:
        return ""
    lines = ["### Claude Code activity"]
    for r in recs:
        files_preview = ", ".join(r.files[:10]) + (
            f" (+{len(r.files) - 10} more)" if len(r.files) > 10 else ""
        )
        lines.append(
            f"- {r.timestamp.isoformat()} in {r.cwd}: "
            f"{r.prompt_count} prompts, {r.duration_s // 60}m, "
            f"files: {files_preview or 'none'}"
        )
    return "\n".join(lines)


def build_session_user_prompt(
    ctx: SessionContext,
    git: GitEvidence,
    claude: list[ClaudeRecord],
    exclude_paths: list[str] | None,
) -> str:
    duration_min = int((ctx.stopped - ctx.started).total_seconds() // 60)
    blocks: list[str] = [
        "# Evidence",
        "",
        f"- client: {ctx.client_name}",
        f"- started: {ctx.started.isoformat()}",
        f"- stopped: {ctx.stopped.isoformat()}",
        f"- duration_min: {duration_min}",
        f"- tags: {', '.join(ctx.tags) if ctx.tags else ''}",
        f"- note: {ctx.note or ''}",
        "",
        f"## Commits ({len(git.commits)})",
    ]
    if git.commits:
        blocks.extend(_format_commit(c) for c in git.commits)
    else:
        blocks.append("_No commits in window._")

    if git.wip:
        blocks.append(f"\n## WIP ({len(git.wip)} repos)")
        blocks.extend(_format_wip(w) for w in git.wip)
    else:
        blocks.append("\n## WIP\n_No uncommitted changes._")

    claude_block = _format_claude(claude)
    if claude_block:
        blocks.append("")
        blocks.append(claude_block)

    if ctx.manual_notes:
        blocks.append("")
        blocks.append(
            "## Manual notes\n"
            "_Added by the contractor; cover work not visible in commits "
            "(calls, planning, manual steps). Treat as primary evidence._"
        )
        for at, text in ctx.manual_notes:
            blocks.append(f"- {at}: {text}")

    raw = "\n\n".join(blocks)
    return redact.scrub_all(raw, exclude_paths or [])


def _openai_client(cfg: Config) -> "openai.OpenAI":
    # Lazy import: the openai SDK pulls in httpx + pydantic (~200–400ms) and
    # is only needed when we're actually making a network call. Keeping it
    # out of module scope means `tk start`, `tk stop --no-summary`,
    # `tk status` etc. never pay that cost.
    import openai

    headers = {}
    if cfg.app_url:
        headers["HTTP-Referer"] = cfg.app_url
    if cfg.app_name:
        headers["X-Title"] = cfg.app_name
    return openai.OpenAI(
        api_key=auth.require_api_key(),
        base_url=cfg.api_base_url,
        default_headers=headers or None,
    )


def _cached_system_message(text: str) -> dict:
    """System message with Anthropic-style cache_control that OpenRouter
    forwards to the underlying Claude model."""
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def _chat(cfg: Config, model: str, system_prompt: str, user: str, max_tokens: int) -> str:
    client = _openai_client(cfg)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            _cached_system_message(system_prompt),
            {"role": "user", "content": user},
        ],
    )
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


def summarize_session(
    cfg: Config,
    ctx: SessionContext,
    git: GitEvidence,
    claude: list[ClaudeRecord],
    model: str | None = None,
    max_tokens: int = 1200,
) -> str:
    user = build_session_user_prompt(ctx, git, claude, cfg.privacy.exclude_paths)
    return _chat(
        cfg,
        model or cfg.session_model,
        SESSION_SYSTEM_PROMPT,
        user,
        max_tokens,
    )


def summarize_weekly(
    cfg: Config,
    week_label: str,
    session_markdowns: list[str],
    hours_line: str,
    model: str | None = None,
    max_tokens: int = 1500,
) -> str:
    user = (
        f"# Week: {week_label}\n\n"
        f"# Hours summary\n{hours_line}\n\n"
        f"# Session logs\n\n" + "\n\n---\n\n".join(session_markdowns)
    )
    return _chat(
        cfg,
        model or cfg.weekly_model,
        WEEKLY_SYSTEM_PROMPT,
        user,
        max_tokens,
    )


def render_raw_session(
    ctx: SessionContext,
    git: GitEvidence,
    claude: list[ClaudeRecord],
    exclude_paths: list[str] | None = None,
) -> str:
    """Fallback when --no-summary is set: emit the evidence as-is.

    `exclude_paths` is threaded through to `scrub_all` so the raw markdown
    on disk honors the same path-redaction rules as a summarized session —
    the file is later fed to the weekly rollup, which does go to the LLM.
    """
    duration_min = int((ctx.stopped - ctx.started).total_seconds() // 60)
    header = textwrap.dedent(
        f"""\
        ---
        client: {ctx.client_name}
        started: {ctx.started.isoformat()}
        stopped: {ctx.stopped.isoformat()}
        duration_min: {duration_min}
        tags: {', '.join(ctx.tags)}
        ---

        # Session (raw)
        """
    )
    return header + "\n" + build_session_user_prompt(
        ctx, git, claude, exclude_paths
    )
