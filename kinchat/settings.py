"""Dashboard-managed settings (features, contacts, chats, audit) stored in state.db.

The bridge re-reads these on every message, so dashboard changes apply without a restart.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .store import Store

# key, label, default, what happens when off. {name} and {quiet_start}/{quiet_end} are filled in by feature_rows.
FEATURES = [
    ("mia_enabled", "{name} enabled (master switch)", True, "Bridge reads and logs, never replies or creates tasks"),
    ("instant_replies", "Instant replies", True, "No chat replies; tasks still go to Duet with a short acknowledgement"),
    ("task_handoff", "Task handoff to Duet", True, "{name} chats but creates no tasks"),
    ("followups", "Follow-ups without naming {name}", True, "{name} responds only when named or replied to inline"),
    ("one_on_one", "One-on-one chats", False, "{name} ignores direct messages, even from approved contacts"),
    ("quiet_hours", "Quiet hours ({quiet_start}–{quiet_end})", True, "Replies are held until {quiet_end} unless marked urgent"),
    ("heartbeat", "Hourly heartbeat email", True, "No health email; Duet can't detect an outage"),
    ("note_suggestions", "Suggested notes", True, "{name} doesn't suggest facts for family.md"),
]


def hour_label(hour: int) -> str:
    """22 -> "10 PM", 0 -> "12 AM", 12 -> "12 PM"."""
    hour %= 24
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def feature_rows(name: str = "Mia", quiet_start: int = 22, quiet_end: int = 7) -> list[tuple[str, str, bool, str]]:
    """FEATURES with labels filled in for display."""
    v = {"name": name, "quiet_start": hour_label(quiet_start), "quiet_end": hour_label(quiet_end)}
    return [(key, label.format(**v), default, off.format(**v)) for key, label, default, off in FEATURES]


CHAT_MODES = ("respond", "listen", "off", "pending")


@dataclass
class Contact:
    id: int
    name: str
    handles: list[str]
    one_on_one: bool
    can_assign: bool


@dataclass
class Chat:
    chat_guid: str
    name: str | None
    mode: str
    participants: list[str]
    first_seen: float


# Calling codes for default_country: a phone number written without "+" is read as national to that country.
CALLING_CODES = {"US": "1", "CA": "1", "GB": "44", "AU": "61", "NZ": "64", "DE": "49"}
NANP = ("US", "CA")  # ten-digit national numbers, no trunk prefix


def norm_handle(h: str, country: str = "US") -> str:
    """Normalise an iMessage handle: email addresses lower-cased, phone numbers as digits with country code.

    Numbers starting with "+" already carry their country code. Others are read as national to `country`:
    a bare 10-digit number in the US/Canada gets "1"; elsewhere a leading trunk "0" is replaced by the
    calling code, and a leading "00" means the number is international. Anything else is compared as written, so a foreign number that merely ends in the same
    digits as a contact's never matches."""
    h = h.strip().lower()
    if "@" in h:
        return h
    digits = "".join(c for c in h if c.isdigit())
    if not digits or h.startswith("+"):
        return digits
    code = CALLING_CODES.get(country.upper(), "1")
    if country.upper() in NANP:
        return code + digits if len(digits) == 10 else digits
    if digits.startswith("00"):  # international prefix outside North America, same as "+"
        return digits[2:]
    if digits.startswith("0"):
        return code + digits[1:]
    return digits


def imessage_is_group(chat_id: str) -> bool:
    """The is_group rule when no transport registry is given: only namespaced iMessage group IDs are groups."""
    prefix, _, native = chat_id.partition(":")
    return prefix == "imessage" and ";+;" in native


class Settings:
    def __init__(self, store: Store, default_country: str = "US",
                 is_group: Callable[[str], bool] = imessage_is_group):
        self._s = store
        self.country = default_country
        self._is_group = is_group  # normally TransportRegistry.is_group
        for key, _, default, _ in FEATURES:
            store._q("INSERT OR IGNORE INTO features (key, enabled) VALUES (?, ?)", (key, int(default)))

    def norm(self, handle: str) -> str:
        return norm_handle(handle, self.country)

    def _audit(self, actor: str, item: str, old, new) -> None:
        self._s._q("INSERT INTO audit (ts, actor, item, old, new) VALUES (?, ?, ?, ?, ?)",
                   (time.time(), actor, item, None if old is None else str(old), None if new is None else str(new)))

    def audit_log(self, limit: int = 100) -> list[dict]:
        rows = self._s._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- features ---------------------------------------------------------

    def features(self) -> dict[str, bool]:
        return {r["key"]: bool(r["enabled"]) for r in self._s._q("SELECT key, enabled FROM features")}

    def feature(self, key: str) -> bool:
        row = self._s._q("SELECT enabled FROM features WHERE key = ?", (key,)).fetchone()
        return bool(row and row["enabled"])

    def set_feature(self, key: str, enabled: bool, actor: str = "dashboard") -> None:
        old = self.feature(key)
        if old == enabled:
            return
        self._s._q("UPDATE features SET enabled = ? WHERE key = ?", (int(enabled), key))
        self._audit(actor, f"feature {key}", "on" if old else "off", "on" if enabled else "off")

    # --- contacts ---------------------------------------------------------

    def contacts(self) -> list[Contact]:
        rows = self._s._q("SELECT * FROM contacts ORDER BY name").fetchall()
        return [Contact(r["id"], r["name"], json.loads(r["handles"]), bool(r["one_on_one"]), bool(r["can_assign"]))
                for r in rows]

    def contact_for_handle(self, handle: str | None) -> Contact | None:
        if not handle:
            return None
        h = self.norm(handle)
        if not h:
            return None
        for c in self.contacts():
            if h in {self.norm(x) for x in c.handles}:
                return c
        return None

    def add_contact(self, name: str, handles: list[str], one_on_one: bool, can_assign: bool,
                    actor: str = "dashboard") -> None:
        self._s._q("INSERT INTO contacts (name, handles, one_on_one, can_assign) VALUES (?, ?, ?, ?)",
                   (name, json.dumps(handles), int(one_on_one), int(can_assign)))
        self._audit(actor, f"contact {name}", None, _contact_str(handles, one_on_one, can_assign))

    def update_contact(self, contact_id: int, name: str, handles: list[str], one_on_one: bool, can_assign: bool,
                       actor: str = "dashboard") -> None:
        old = next((c for c in self.contacts() if c.id == contact_id), None)
        if old is None:
            return
        self._s._q("UPDATE contacts SET name = ?, handles = ?, one_on_one = ?, can_assign = ? WHERE id = ?",
                   (name, json.dumps(handles), int(one_on_one), int(can_assign), contact_id))
        self._audit(actor, f"contact {name}", f"{old.name}: " + _contact_str(old.handles, old.one_on_one, old.can_assign),
                    _contact_str(handles, one_on_one, can_assign))

    def delete_contact(self, contact_id: int, actor: str = "dashboard") -> None:
        old = next((c for c in self.contacts() if c.id == contact_id), None)
        if old is None:
            return
        self._s._q("DELETE FROM contacts WHERE id = ?", (contact_id,))
        self._audit(actor, f"contact {old.name}", _contact_str(old.handles, old.one_on_one, old.can_assign), None)

    # --- chats ------------------------------------------------------------

    def chats(self) -> list[Chat]:
        rows = self._s._q("SELECT * FROM chats ORDER BY first_seen DESC").fetchall()
        return [_chat(r) for r in rows]

    def get_chat(self, chat_guid: str) -> Chat | None:
        row = self._s._q("SELECT * FROM chats WHERE chat_guid = ?", (chat_guid,)).fetchone()
        return _chat(row) if row else None

    def register_pending(self, chat_guid: str, name: str | None, participants: list[str]) -> Chat:
        cur = self._s._q("INSERT OR IGNORE INTO chats (chat_guid, name, mode, participants, first_seen)"
                         " VALUES (?, ?, 'pending', ?, ?)", (chat_guid, name, json.dumps(participants), time.time()))
        if cur.rowcount:
            self._audit("bridge", f"chat {name or chat_guid}", None, "pending")
        return self.get_chat(chat_guid)

    def set_chat_mode(self, chat_guid: str, mode: str, actor: str = "dashboard") -> None:
        if mode not in CHAT_MODES:
            raise ValueError(mode)
        old = self.get_chat(chat_guid)
        if old is None or old.mode == mode:
            return
        self._s._q("UPDATE chats SET mode = ? WHERE chat_guid = ?", (mode, chat_guid))
        self._audit(actor, f"chat {old.name or chat_guid}", old.mode, mode)

    def check_members(self, chat: Chat, participants: list[str], ignore: set[str] = frozenset()) -> Chat:
        """Record the group's current members; send it back to Pending if any of them isn't an approved contact."""
        if sorted(participants) != sorted(chat.participants):
            self._s._q("UPDATE chats SET participants = ? WHERE chat_guid = ?",
                       (json.dumps(participants), chat.chat_guid))
        strangers = [h for h in participants
                     if self.norm(h) not in ignore and self.contact_for_handle(h) is None]
        if strangers and chat.mode in ("respond", "listen"):
            self._s._q("UPDATE chats SET mode = 'pending' WHERE chat_guid = ?", (chat.chat_guid,))
            self._audit("bridge", f"chat {chat.name or chat.chat_guid} (new member {', '.join(strangers)})",
                        chat.mode, "pending")
        return self.get_chat(chat.chat_guid)

    def can_speak_in(self, chat_guid: str) -> bool:
        """Final check before anything is posted to a chat."""
        if not self.feature("mia_enabled"):
            return False
        if self._is_group(chat_guid):
            chat = self.get_chat(chat_guid)
            return bool(chat and chat.mode == "respond")
        return self.feature("one_on_one")


@dataclass
class Note:
    id: int
    text: str
    chat_guid: str | None
    source: str | None
    status: str
    created_ts: float


class Notes:
    """Suggested family.md notes: the assistant proposes, the owner approves or rejects in the dashboard."""

    def __init__(self, store: Store, settings: Settings, family_md: Path):
        self._s = store
        self._settings = settings
        self.family_md = family_md

    def suggest(self, text: str, chat_guid: str, source: str) -> bool:
        text = " ".join(text.split())[:300]
        if not text:
            return False
        dup = self._s._q("SELECT 1 FROM notes WHERE lower(text) = lower(?) AND status != 'rejected'", (text,)).fetchone()
        try:
            in_file = text.lower() in self.family_md.read_text().lower()
        except OSError:
            in_file = False
        if dup or in_file:
            return False
        self._s._q("INSERT INTO notes (text, chat_guid, source, created_ts) VALUES (?, ?, ?, ?)",
                   (text, chat_guid, source, time.time()))
        return True

    def pending(self) -> list[Note]:
        rows = self._s._q("SELECT * FROM notes WHERE status = 'pending' ORDER BY id").fetchall()
        return [Note(r["id"], r["text"], r["chat_guid"], r["source"], r["status"], r["created_ts"]) for r in rows]

    def approve(self, note_id: int, text: str, actor: str = "dashboard") -> None:
        row = self._s._q("SELECT * FROM notes WHERE id = ? AND status = 'pending'", (note_id,)).fetchone()
        if not row:
            return
        text = " ".join(text.split())[:300] or row["text"]  # the owner may edit the wording before approving
        self._append_to_family_md(text)
        self._s._q("UPDATE notes SET status = 'approved', text = ?, decided_ts = ? WHERE id = ?",
                   (text, time.time(), note_id))
        self._settings._audit(actor, "note approved", None, text)

    def reject(self, note_id: int, actor: str = "dashboard") -> None:
        row = self._s._q("SELECT * FROM notes WHERE id = ? AND status = 'pending'", (note_id,)).fetchone()
        if not row:
            return
        self._s._q("UPDATE notes SET status = 'rejected', decided_ts = ? WHERE id = ?", (time.time(), note_id))
        self._settings._audit(actor, "note rejected", row["text"], None)

    def _append_to_family_md(self, text: str) -> None:
        try:
            current = self.family_md.read_text()
        except FileNotFoundError:
            current = ""
        line = f"- {text} (added {datetime.now().strftime('%b %d %Y')})"
        if NOTES_HEADING not in current:
            updated = current.rstrip("\n") + f"\n\n{NOTES_HEADING}\n{line}\n"
        else:
            # Insert at the end of the learned-notes section, before any later heading the owner added.
            start = current.index(NOTES_HEADING) + len(NOTES_HEADING)
            nxt = current.find("\n## ", start)
            end = len(current) if nxt < 0 else nxt
            updated = current[:end].rstrip("\n") + f"\n{line}\n" + ("\n" + current[end:].lstrip("\n") if nxt >= 0 else "")
        tmp = self.family_md.with_suffix(".md.tmp")
        tmp.write_text(updated)
        tmp.replace(self.family_md)


NOTES_HEADING = "## Learned from chat (approved in the dashboard)"


def _contact_str(handles: list[str], one_on_one: bool, can_assign: bool) -> str:
    return f"{', '.join(handles)}; one-on-one {'yes' if one_on_one else 'no'}; tasks {'yes' if can_assign else 'no'}"


def _chat(r) -> Chat:
    return Chat(r["chat_guid"], r["name"], r["mode"], json.loads(r["participants"] or "[]"), r["first_seen"])
