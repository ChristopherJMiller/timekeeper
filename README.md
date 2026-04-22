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
| `tk edit <id>` | Open a curses TUI to reassign client, fix times, edit note/tags |
| `tk edit <id> [--started T] [--stopped T] [--client N] [--note T]` | Non-interactive edit (scriptable) |
| `tk abandon` | Drop the active session without summarizing |
| `tk note add <text> [--session ID]` | Attach a note (calls, planning, manual work not in commits) |
| `tk note list [--session ID]` | List notes attached to a session |
| `tk note rm <note_id>` | Delete a note |
| `tk report [--week YYYY-Www] [--client NAME] [--regenerate]` | Weekly rollup |
| `tk clients {add,list,update,archive}` | Manage clients |
| `tk auth {set,status,clear}` | Manage the API key in the keychain |
| `tk completion {bash,zsh,fish}` | Print a tab-completion script for the shell |
| `tk doctor [--install-hook]` | Verify config + optionally install the Claude Code Stop hook |

## Tab completion

The Nix build installs shell completion scripts automatically:

- **bash:** `~/.nix-profile/share/bash-completion/completions/tk.bash`
- **zsh:** `~/.nix-profile/share/zsh/site-functions/_tk`
- **fish:** `~/.nix-profile/share/fish/vendor_completions.d/tk.fish`

For pip installs (or ad-hoc sourcing), emit the script yourself:

```sh
# bash
eval "$(tk completion bash)"

# zsh
eval "$(tk completion zsh)"

# fish
tk completion fish > ~/.config/fish/completions/tk.fish
```

Completion pulls live values from your db: session IDs on `tk show`, `tk
edit`, `tk note add --session`; client names on `tk start -c`, `tk clients
update/archive`, `tk report --client`; and existing weekly labels on `tk
report --week`.

## Editing a session (TUI)

```sh
tk edit 42                 # opens the curses form
```

Keys: ↑/↓ to move, Enter to edit a field (Enter on "Client" opens a picker
populated from `tk clients`), `n` to add a manual note, `d` to delete the
highlighted note, `S` to save, `q`/Esc to cancel.

Note adds/deletes persist immediately; field edits (client, times, note,
tags) only persist on `S`.

Pass any of `--started`, `--stopped`, `--client`, `--note` to skip the TUI
and edit non-interactively (scriptable).

## Manual notes

`tk note add` captures work that isn't in your commits — phone calls,
planning sessions, manual deploys, whiteboarding. Notes are attached to a
session and surface in three places:

1. **At `tk stop`:** they're passed to the LLM as primary evidence, so the
   session summary reflects the full scope of work, not just code diffs.
2. **On closed sessions:** added notes are appended to the session's
   markdown file under `## Additional notes`, so the weekly rollup (which
   re-reads session markdown) picks them up on its next run.
3. **Via `tk show`:** active sessions without a summary yet display their
   accumulated notes.

```sh
tk start -c acme -n "ship the webhook retry work"
tk note add "45-min planning call with platform team — agreed on retry budget 3"
# … commits, more work …
tk note add "manually replayed 142 stuck events from staging queue"
tk stop             # LLM sees both notes alongside the git evidence
```

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
