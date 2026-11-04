"""Messages adapter: read new rows from chat.db, send text to a chat ID by AppleScript.

Reading chat.db needs Full Disk Access for the Python binary the launchd agent runs;
sending needs Automation permission to control Messages. Both are granted once,
logged in as the assistant's macOS user.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

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


@dataclass
class Incoming:
    rowid: int
    guid: str
    chat_guid: str
    is_group: bool
    chat_name: str | None
    handle: str | None
    text: str
    is_from_me: bool
    ts: float
    thread_originator_guid: str | None
    is_tapback: bool


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
                chat_guid=r["chat_guid"],
                is_group=r["style"] == STYLE_GROUP,
                chat_name=r["display_name"] or None,
                handle=r["handle"],
                text=text.replace(OBJECT_REPLACEMENT, "").strip(),
                is_from_me=bool(r["is_from_me"]),
                ts=apple_ts(r["date"]),
                thread_originator_guid=r["thread_originator_guid"] or None,
                is_tapback=bool(r["associated_message_type"]),
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
