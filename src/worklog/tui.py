"""Curses-based edit TUI for sessions.

Launched by `tk edit SESSION_ID` (with no field flags). Lets the user:

- Reassign the client from a picker of configured clients.
- Edit started / stopped timestamps, planning-note, and tags.
- View, add, and delete manual notes attached to the session.

Pure-logic helpers (`load_draft`, `apply`, `_append_note_to_md`) are exposed
so tests can exercise the persistence path without running curses. The
curses loop itself is intentionally minimal — it only translates keystrokes
into state mutations and calls `apply` once on save.
"""
from __future__ import annotations

import curses
import datetime as dt
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import db
from .config import Config


SENTINEL_UNASSIGNED = "— unassigned —"


@dataclass
class EditDraft:
    """Mutable snapshot of the fields the TUI can edit.

    `client_name` uses `SENTINEL_UNASSIGNED` when no client is attached, so
    the form can render a concrete label without a None-check branch on every
    redraw. Convert back to `None` at persistence time.
    """

    client_name: str
    started: str
    stopped: str
    note: str
    tags: list[str] = field(default_factory=list)


def _row_client_name(
    con: sqlite3.Connection, client_id: int | None
) -> str:
    if client_id is None:
        return SENTINEL_UNASSIGNED
    row = con.execute(
        "SELECT name FROM clients WHERE id=?", (client_id,)
    ).fetchone()
    return row["name"] if row else SENTINEL_UNASSIGNED


def load_draft(
    con: sqlite3.Connection, session_id: int
) -> tuple[EditDraft, list[str], list[sqlite3.Row]]:
    """Read current session state + pick-list of clients + notes."""
    row = db.get_session(con, session_id)
    if row is None:
        raise ValueError(f"no session {session_id}")
    client_name = _row_client_name(con, row["client_id"])
    tags = [t for t in (row["tags"] or "").split(",") if t]
    draft = EditDraft(
        client_name=client_name,
        started=row["started_at"] or "",
        stopped=row["stopped_at"] or "",
        note=row["note"] or "",
        tags=tags,
    )
    client_names = [SENTINEL_UNASSIGNED] + [
        c["name"] for c in db.list_clients(con) if c["active"]
    ]
    if client_name not in client_names:
        # Archived clients still show as assignable so users can see current state.
        client_names.insert(1, client_name)
    notes = db.list_notes(con, session_id)
    return draft, client_names, notes


def apply(
    con: sqlite3.Connection,
    session_id: int,
    original: EditDraft,
    draft: EditDraft,
) -> bool:
    """Persist only the fields that differ. Returns True if anything changed.

    Timestamp edits trigger the duration recompute inside `db.update_session`.
    Tags are stored separately because `update_session` doesn't handle them.
    """
    changed = False

    kwargs: dict = {}
    if draft.client_name != original.client_name:
        if draft.client_name == SENTINEL_UNASSIGNED:
            # Sentinel collapses to NULL; update_session skips None, so we
            # write directly to clear the foreign key.
            con.execute(
                "UPDATE sessions SET client_id=NULL WHERE id=?", (session_id,)
            )
            changed = True
        else:
            crow = db.get_client_by_name(con, draft.client_name)
            if crow is None:
                raise ValueError(f"unknown client {draft.client_name!r}")
            kwargs["client_id"] = crow["id"]
    if draft.started != original.started:
        kwargs["started_at"] = draft.started
    if draft.stopped != original.stopped:
        kwargs["stopped_at"] = draft.stopped
    if draft.note != original.note:
        kwargs["note"] = draft.note

    if kwargs:
        db.update_session(con, session_id, **kwargs)
        changed = True

    if draft.tags != original.tags:
        db.update_session_tags(con, session_id, draft.tags)
        changed = True

    return changed


# ---------------------------------------------------------------------------
# curses front-end
# ---------------------------------------------------------------------------

_FIELD_LABELS = [
    ("client", "Client"),
    ("started", "Started"),
    ("stopped", "Stopped"),
    ("note", "Note"),
    ("tags", "Tags"),
]


def _draw(stdscr, draft: EditDraft, notes, sel: int, status: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    title = "tk edit — ↑/↓ move · Enter edit · n add note · d delete note · S save · q cancel"
    stdscr.addstr(0, 0, title[: w - 1], curses.A_BOLD)

    for i, (key, label) in enumerate(_FIELD_LABELS):
        attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
        if key == "client":
            value = draft.client_name
        elif key == "started":
            value = draft.started
        elif key == "stopped":
            value = draft.stopped
        elif key == "note":
            value = draft.note or "(none)"
        else:
            value = ", ".join(draft.tags) if draft.tags else "(none)"
        line = f"  {label:<8} {value}"[: w - 1]
        stdscr.addstr(2 + i, 0, line, attr)

    notes_y = 2 + len(_FIELD_LABELS) + 1
    stdscr.addstr(notes_y, 0, "Manual notes:", curses.A_BOLD)
    if not notes:
        stdscr.addstr(notes_y + 1, 2, "(none — press 'n' to add)")
    else:
        for j, n in enumerate(notes):
            row_y = notes_y + 1 + j
            if row_y >= h - 2:
                stdscr.addstr(row_y, 2, f"… (+{len(notes) - j} more)")
                break
            note_sel = sel == len(_FIELD_LABELS) + j
            attr = curses.A_REVERSE if note_sel else curses.A_NORMAL
            when = n["added_at"][:16].replace("T", " ")
            txt = n["text"].replace("\n", " ")
            line = f"  {when}  {txt}"[: w - 1]
            stdscr.addstr(row_y, 0, line, attr)

    if status:
        stdscr.addstr(h - 1, 0, status[: w - 1], curses.A_DIM)
    stdscr.refresh()


def _prompt_line(stdscr, label: str, initial: str) -> str | None:
    """Modal single-line editor. Returns the new value, or None on Esc."""
    h, w = stdscr.getmaxyx()
    y = h - 2
    s = list(initial)
    pos = len(s)
    curses.curs_set(1)
    try:
        while True:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            prompt = f"{label}: "
            stdscr.addstr(y, 0, prompt)
            field_w = max(1, w - len(prompt) - 1)
            view = "".join(s)[:field_w]
            stdscr.addstr(y, len(prompt), view)
            stdscr.move(y, len(prompt) + min(pos, field_w - 1))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, 13):
                return "".join(s)
            if ch == 27:  # Esc
                return None
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del s[pos - 1]
                    pos -= 1
            elif ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(s), pos + 1)
            elif ch == curses.KEY_HOME:
                pos = 0
            elif ch == curses.KEY_END:
                pos = len(s)
            elif 32 <= ch < 127:
                s.insert(pos, chr(ch))
                pos += 1
    finally:
        curses.curs_set(0)


def _pick(stdscr, title: str, options: list[str], current: str) -> str | None:
    idx = options.index(current) if current in options else 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, f"{title} — ↑/↓, Enter, Esc"[: w - 1], curses.A_BOLD)
        for i, name in enumerate(options):
            if 2 + i >= h - 1:
                break
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            stdscr.addstr(2 + i, 2, name[: w - 3], attr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            idx = (idx - 1) % len(options)
        elif ch == curses.KEY_DOWN:
            idx = (idx + 1) % len(options)
        elif ch in (10, 13):
            return options[idx]
        elif ch == 27:
            return None


def _run(stdscr, cfg: Config, session_id: int) -> bool:
    curses.curs_set(0)
    with db.connect(cfg.db_path) as con:
        draft, client_names, notes = load_draft(con, session_id)
    original = replace(draft, tags=list(draft.tags))
    sel = 0
    status = ""
    notes_touched = False

    while True:
        n_fields = len(_FIELD_LABELS)
        n_notes = len(notes)
        _draw(stdscr, draft, notes, sel, status)
        status = ""
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            sel = (sel - 1) % (n_fields + max(n_notes, 1))
        elif ch == curses.KEY_DOWN:
            sel = (sel + 1) % (n_fields + max(n_notes, 1))
        elif ch in (10, 13):
            if sel < n_fields:
                key = _FIELD_LABELS[sel][0]
                if key == "client":
                    picked = _pick(stdscr, "Select client", client_names, draft.client_name)
                    if picked is not None:
                        draft.client_name = picked
                elif key == "tags":
                    new = _prompt_line(stdscr, "Tags (comma-separated)", ", ".join(draft.tags))
                    if new is not None:
                        draft.tags = [t.strip() for t in new.split(",") if t.strip()]
                else:
                    current = getattr(draft, key)
                    new = _prompt_line(stdscr, _FIELD_LABELS[sel][1], current)
                    if new is not None:
                        setattr(draft, key, new)
        elif ch in (ord("n"), ord("N")):
            text = _prompt_line(stdscr, "New note", "")
            if text:
                with db.connect(cfg.db_path) as con:
                    db.add_note(con, session_id, text.strip())
                    notes = db.list_notes(con, session_id)
                    row = db.get_session(con, session_id)
                    if row and row["status"] == "closed" and row["summary_path"]:
                        from .cli import _rewrite_notes_section
                        _rewrite_notes_section(Path(row["summary_path"]), notes)
                status = f"+ note added ({len(notes)} total)"
                notes_touched = True
        elif ch in (ord("d"), ord("D")) and sel >= n_fields and notes:
            note_idx = sel - n_fields
            if 0 <= note_idx < len(notes):
                target = notes[note_idx]
                with db.connect(cfg.db_path) as con:
                    db.delete_note(con, target["id"])
                    notes = db.list_notes(con, session_id)
                    row = db.get_session(con, session_id)
                    if row and row["status"] == "closed" and row["summary_path"]:
                        from .cli import _rewrite_notes_section
                        _rewrite_notes_section(Path(row["summary_path"]), notes)
                sel = min(sel, n_fields + max(len(notes) - 1, 0))
                status = "- note removed"
                notes_touched = True
        elif ch in (ord("s"), ord("S")):
            try:
                with db.connect(cfg.db_path) as con:
                    changed_fields = apply(con, session_id, original, draft)
            except ValueError as e:
                status = f"! {e} — press Esc to cancel"
                continue
            return changed_fields or notes_touched
        elif ch in (ord("q"), ord("Q"), 27):
            # Notes persist immediately on add/delete; field edits are rolled
            # back by not calling apply(). Report truthfully.
            return notes_touched


def run_edit(cfg: Config, session_id: int) -> bool:
    """Entry point called by `cli.edit`. Wraps the curses loop."""
    return curses.wrapper(lambda s: _run(s, cfg, session_id))
