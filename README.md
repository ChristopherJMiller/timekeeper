# timekeeper (`tk`)

A personal contractor work-tracker that turns **git commits** and **Claude
Code sessions** into an impact-framed weekly report you can send to your
manager.

- **Local-first.** One SQLite file, one config file, markdown artifacts. No
  daemon, no cloud sync.
- **LLM-optional.** Summarization runs through OpenRouter (the OpenAI
  SDK pointed at `openrouter.ai`), so you can pick any model. Prompt caching
  is on by default for Anthropic-hosted models.
- **Keychain-backed auth.** API key lives in your system keychain (macOS
  Keychain / libsecret / Windows Credential Locker) via `keyring`, or in
  `$OPENROUTER_API_KEY` if you prefer env vars.

## Install

### Nix (recommended for daily use)

```sh
nix run .                          # one-shot
nix profile install .              # install `tk` into your profile
nix develop                        # dev shell with pytest + source on PYTHONPATH
```

The flake pins `git` and `jq` onto `tk`'s `PATH` so it works regardless of
the caller's environment. The openai and keyring SDKs are lazy-imported, so
hot-path commands (`tk start`, `tk status`, `tk stop --no-summary`) avoid
~300 ms of import cost.

### pip

```sh
pip install -e .
```

Requires Python 3.11+.

## Quick start

```sh
tk auth set                        # stores key in keychain (hidden prompt)
$EDITOR ~/.config/worklog/config.toml   # add your author email + repo list
tk clients add acme --budget 10 --rate 150
tk doctor                          # sanity check

tk start -c acme -n "payment webhook retries"
# … do work, make commits …
tk stop                            # writes ~/worklog/sessions/…md

tk report                          # current ISO week → ~/worklog/weekly/…md
```

## Command reference

| Command | What it does |
|---|---|
| `tk start [-c CLIENT] [-n NOTE] [-t TAG]… [--at HH:MM]` | Open a session |
| `tk stop [--at HH:MM] [--no-summary] [--model M] [--force]` | Close and summarize |
| `tk status` | Active session + today's closed total |
| `tk list [--limit N]` | Recent sessions, one per line |
| `tk show <id>` | Print a session's markdown |
| `tk edit <id> [--started T] [--stopped T] [--client N] [--note T]` | Backfill / fix |
| `tk abandon` | Drop the active session without summarizing |
| `tk report [--week YYYY-Www] [--client NAME] [--regenerate]` | Weekly rollup |
| `tk clients {add,list,update,archive}` | Manage clients |
| `tk auth {set,status,clear}` | Manage the API key in the keychain |
| `tk doctor [--install-hook]` | Verify config + optionally install the Claude Code Stop hook |

## How evidence is collected

At `tk stop`, the tool gathers, for the session's time window:

1. **Git commits** across every repo in `config.toml`'s `repos = [...]` —
   `git log --all --no-merges --author=<email> --since --until`, with the
   diff inlined for commits ≤200 changed lines.
2. **Work in progress** — `git diff HEAD` in each repo, so a session that
   ends mid-refactor isn't invisible.
3. **Claude Code session signals** (optional) — records appended to
   `~/.local/share/worklog/hooks.jsonl` by the installed Stop hook: files
   touched, prompt count, duration.

Everything is run through `redact.py` (AWS keys, GH tokens, private key
blocks, `.env`-style secrets, configurable path names) **before** it
crosses the network.

The result goes to OpenRouter with an impact-framing system prompt; the
reply is written as `~/worklog/sessions/YYYY-MM-DD-HHMM.md`. The weekly
report (`tk report`) reads those session markdowns and makes a single
additional LLM call to consolidate them.

## Config (`~/.config/worklog/config.toml`)

```toml
author = "you@example.com"
output_dir = "~/worklog"

api_base_url  = "https://openrouter.ai/api/v1"
session_model = "anthropic/claude-haiku-4.5"
weekly_model  = "anthropic/claude-sonnet-4.5"

app_name = "timekeeper"
app_url  = ""                           # sent as HTTP-Referer for attribution

repos = [
    "~/code/acme-backend",
    "~/code/acme-frontend",
]

[privacy]
redact_secrets = true
exclude_paths = [".env", "secrets/"]
```

## API key resolution

When `tk stop` or `tk report` needs to call the LLM, the key is looked up
in this order:

1. `$OPENROUTER_API_KEY`
2. `$WORKLOG_API_KEY`
3. System keychain (service=`timekeeper`, user=`openrouter`)

Check with `tk auth status`. Store with `tk auth set`. Remove with
`tk auth clear`.

## Claude Code hook

`tk doctor --install-hook` copies `hooks/claude-stop-hook.sh` to
`~/.claude/worklog-stop-hook.sh` with the log path hardcoded, and prints
the one-line `settings.json` entry to paste. The hook reads the Claude
Code Stop event from stdin, extracts a small JSON record, and appends it
to `~/.local/share/worklog/hooks.jsonl`. If `jq` is missing, it falls
back to a minimal record.

## Development

```sh
nix develop                 # or: python -m venv .venv && pip install -e '.[dev]'
pytest                      # 24 tests covering redact, db, git, claude, auth, e2e
python -m worklog.cli --help
```

Hot paths are kept fast by lazy-importing `openai` (pulls pydantic + httpx)
and `keyring` only inside the functions that need them. Keep it that way —
`grep -n "^import openai\|^import keyring" src/worklog/` must stay empty.

## Design notes

Guiding choices:

- **Git is the primary evidence source, not Claude Code transcripts.** The
  Claude JSONL format is internal and version-dependent; we read our own
  Stop-hook log instead.
- **Clients are first-class.** Free-text tags can't carry hours-vs-budget
  for a contractor with 2+ engagements.
- **The weekly rollup re-reads per-session markdown.** One LLM call per
  weekly report, not N.
- **Hot-path commands never import `openai` or `keyring`.** Both are lazy
  to keep the shell experience snappy.
