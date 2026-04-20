#!/usr/bin/env bash
# Claude Code Stop hook — appends a one-line JSON record to hooks.jsonl.
#
# Claude Code pipes a JSON event to stdin on Stop. We extract the fields we
# care about with `jq`, skip the rest. If jq is unavailable, fall back to a
# minimal record with just the timestamp and cwd.
#
# Install: `tk doctor --install-hook` copies this script and wires it into
# ~/.claude/settings.json for you.

set -euo pipefail

HOOKS_LOG="${WORKLOG_HOOKS_LOG:-__HOOKS_LOG__}"
mkdir -p "$(dirname "$HOOKS_LOG")"

TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PAYLOAD="$(cat || true)"

if command -v jq >/dev/null 2>&1 && [ -n "$PAYLOAD" ]; then
    RECORD="$(printf '%s' "$PAYLOAD" | jq -c --arg ts "$TIMESTAMP" '
        {
            timestamp: $ts,
            cwd: (.cwd // env.PWD // ""),
            session_id: (.session_id // ""),
            prompt_count: (.prompt_count // 0),
            duration_s: (.duration_s // 0),
            files: (.files // [])
        }
    ' 2>/dev/null || true)"
fi

if [ -z "${RECORD:-}" ]; then
    RECORD="$(printf '{"timestamp":"%s","cwd":"%s","session_id":"","prompt_count":0,"duration_s":0,"files":[]}' \
        "$TIMESTAMP" "${PWD//\"/\\\"}")"
fi

printf '%s\n' "$RECORD" >> "$HOOKS_LOG"
