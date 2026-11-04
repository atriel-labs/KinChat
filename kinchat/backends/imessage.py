"""iMessage transport: read new rows from chat.db, send text to a chat ID by AppleScript.

Reading chat.db needs Full Disk Access for the Python binary the launchd agent runs;
sending needs Automation permission to control Messages. Both are granted once,
logged in as the assistant's macOS user.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path

from ..health import Health
from .base import Incoming, make_id, split_id

log = logging.getLogger(__name__)

NAME = "imessage"
BOOKMARK_KEY = "chatdb_last_rowid"  # kv key; unchanged from before transports so a live install resumes in place

APPLE_EPOCH = 978307200  # 2001-01-01 in Unix time
STYLE_GROUP = 43
OBJECT_REPLACEMENT = "￼"  # placeholder text for attachments

SEND_SCRIPT = """
on run argv
    tell application "Messages"
        send (item 1 of argv) to chat id (item 2 of argv)
    end tell
end run
"""

NEW_MESSAGES_SQL = """
SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.is_from_me, m.date,
       m.associated_message_type, m.thread_originator_guid, m.item_type,
       h.id AS handle, c.guid AS chat_guid, c.style, c.display_name
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.ROWID > ?
ORDER BY m.ROWID
LIMIT 200
"""


def apple_ts(date: int | None) -> float:
    if not date:
        return 0.0
    # Since High Sierra, chat.db stores nanoseconds; older rows use seconds.
    return (date / 1e9 if date > 1e11 else float(date)) + APPLE_EPOCH


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Pull the plain string out of an NSAttributedString typedstream (newer macOS leaves `text` NULL)."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i + len(b"NSString"), i + len(b"NSString") + 12)
    if j < 0:
        return None
    i = j + 1
    length = blob[i]
    i += 1
    if length == 0x81:
        length = int.from_bytes(blob[i:i + 2], "little")
        i += 2
    elif length == 0x82:
        length = int.from_bytes(blob[i:i + 3], "little")
        i += 3
    return blob[i:i + length].decode("utf-8", errors="replace")


class ChatDB:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self._db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        return self._db

    def reset(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def max_rowid(self) -> int:
        return self._conn().execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def new_messages(self, after_rowid: int) -> list[Incoming]:
        out = []
        for r in self._conn().execute(NEW_MESSAGES_SQL, (after_rowid,)):
            if r["item_type"]:  # group renames, member changes and other system rows
                continue
            text = r["text"] or decode_attributed_body(r["attributedBody"]) or ""
            out.append(Incoming(
                rowid=r["rowid"],
                guid=r["guid"],
                chat_guid=make_id(NAME, r["chat_guid"]),
                is_group=r["style"] == STYLE_GROUP,
                chat_name=r["display_name"] or None,
                handle=r["handle"],
                text=text.replace(OBJECT_REPLACEMENT, "").strip(),
                is_from_me=bool(r["is_from_me"]),
                ts=apple_ts(r["date"]),
                thread_originator_guid=r["thread_originator_guid"] or None,
                is_tapback=bool(r["associated_message_type"]),
                transport=NAME,
            ))
        return out

    def participants(self, chat_guid: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT h.id FROM chat c JOIN chat_handle_join chj ON chj.chat_id = c.ROWID"
            " JOIN handle h ON h.ROWID = chj.handle_id WHERE c.guid = ? ORDER BY h.id", (chat_guid,))
        return [r[0] for r in rows]

    def list_chats(self) -> list[dict]:
        rows = self._conn().execute("SELECT guid, display_name, style FROM chat ORDER BY ROWID DESC").fetchall()
        return [{"guid": r["guid"], "name": r["display_name"], "is_group": r["style"] == STYLE_GROUP,
                 "participants": self.participants(r["guid"])} for r in rows]


def send_text(chat_guid: str, text: str, timeout: float = 20) -> None:
    """Send a message through Messages. Raises on failure."""
    r = subprocess.run(["osascript", "-e", SEND_SCRIPT, text, chat_guid],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"osascript failed: {r.stderr.strip()}")


def messages_scriptable(timeout: float = 20) -> int:
    """Return how many chats Messages reports; triggers the Automation permission prompt the first time."""
    r = subprocess.run(["osascript", "-e", 'tell application "Messages" to count of chats'],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return int(r.stdout.strip() or 0)


def is_group_id(native_guid: str) -> bool:
    """Group chat GUIDs look like "iMessage;+;chat123"; one-on-one ones like "iMessage;-;+18005550100"."""
    return ";+;" in native_guid


class IMessageTransport:
    """chat.db + Messages.app. Owns the chat.db bookmark (the last handled ROWID)."""

    name = NAME

    def __init__(self, chat_db: Path | str, store, send=send_text, scriptable=messages_scriptable):
        self.db = ChatDB(chat_db)
        self._store = store
        self._send = send
        self._scriptable = scriptable
        self._rowid: int | None = None
        self.health = Health("chatdb", "chat.db watcher")
        self.send_health = Health("messages", "Messages sending (Automation)")
        self.health.degraded("not read yet")
        self.send_health.degraded("not checked yet")

    def _bookmark(self) -> int:
        if self._rowid is None:
            last = self._store.kv_get(BOOKMARK_KEY)
            if last is None:
                last = str(self.db.max_rowid())  # first run: start from now, not from history
                self._store.kv_set(BOOKMARK_KEY, last)
            self._rowid = int(last)
        return self._rowid

    def poll(self) -> list[Incoming]:
        try:
            msgs = self.db.new_messages(self._bookmark())
        except Exception as e:
            self.health.degraded(e)
            self.db.reset()
            raise
        self.health.ok()
        return msgs

    def ack(self, inc: Incoming) -> None:
        if self._rowid is None or inc.rowid > self._rowid:
            self._rowid = inc.rowid
            self._store.kv_set(BOOKMARK_KEY, str(inc.rowid))

    def send(self, chat_id: str, text: str) -> None:
        _, native = split_id(chat_id)
        try:
            self._send(native, text)
        except Exception as e:
            self.send_health.degraded(e)
            raise
        self.send_health.ok()

    def participants(self, chat_id: str) -> list[str]:
        return self.db.participants(split_id(chat_id)[1])

    def is_group(self, chat_id: str) -> bool:
        return is_group_id(split_id(chat_id)[1])

    def check_scriptable(self) -> None:
        """Ask Messages for its chat count once, so macOS shows the Automation prompt at startup."""
        try:
            n = self._scriptable()
        except Exception as e:
            self.send_health.failed(e)
            return
        self.send_health.ok()
        log.info("Messages is scriptable (%d chats)", n)
