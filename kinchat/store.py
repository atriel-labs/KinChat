"""SQLite state store: chat history, routing decisions, window, tasks, posted results."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Schema version 1. Later changes are new entries in MIGRATIONS below, never edits to this script.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    guid TEXT PRIMARY KEY,
    chat_guid TEXT NOT NULL,
    sender TEXT,
    text TEXT,
    ts REAL NOT NULL,
    from_mia INTEGER NOT NULL DEFAULT 0,
    reply_to_guid TEXT
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_guid, ts);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_guid TEXT NOT NULL,
    step TEXT NOT NULL,
    route TEXT NOT NULL,
    confidence REAL,
    intent TEXT,
    reason TEXT,
    ts REAL NOT NULL,
    cls_route INTEGER,
    cls_reason TEXT
);

CREATE TABLE IF NOT EXISTS window (
    chat_guid TEXT PRIMARY KEY,
    active_until REAL NOT NULL DEFAULT 0,
    quiet_until REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    chat_guid TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL,
    requested_by TEXT,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    email_msg_id TEXT
);

CREATE TABLE IF NOT EXISTS posted_results (
    graph_message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Messages held during quiet hours, sent at the end of them.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_guid TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);

-- Dashboard settings, re-read on every message.
CREATE TABLE IF NOT EXISTS features (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    handles TEXT NOT NULL,
    one_on_one INTEGER NOT NULL DEFAULT 0,
    can_assign INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS chats (
    chat_guid TEXT PRIMARY KEY,
    name TEXT,
    mode TEXT NOT NULL DEFAULT 'pending',
    participants TEXT,
    first_seen REAL NOT NULL
);
-- Facts Mia suggests for family.md; nothing is written until the owner approves.
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    chat_guid TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_ts REAL NOT NULL,
    decided_ts REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    item TEXT NOT NULL,
    old TEXT,
    new TEXT
);
"""

OPEN_STATUSES = ("open", "needs_input")


def _v1(db: sqlite3.Connection) -> None:
    """Today's schema. Databases from before versioning (user_version 0) may already have these tables,
    some without the decisions.cls_* columns, so the tables are created only if missing and the columns
    added only if missing."""
    for stmt in _statements(SCHEMA_V1):
        db.execute(stmt)
    cols = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
    for col, kind in (("cls_route", "INTEGER"), ("cls_reason", "TEXT")):
        if col not in cols:
            db.execute(f"ALTER TABLE decisions ADD COLUMN {col} {kind}")


# Every table that stores a chat ID. Chat IDs are namespaced "<transport>:<native id>" from v2 on.
CHAT_ID_COLUMNS = (("chats", "chat_guid"), ("messages", "chat_guid"), ("window", "chat_guid"),
                   ("tasks", "chat_guid"), ("outbox", "chat_guid"), ("notes", "chat_guid"))


def _v2(db: sqlite3.Connection) -> None:
    """Namespace chat IDs: everything stored so far came from iMessage, so "iMessage;+;x" becomes
    "imessage:iMessage;+;x". Native iMessage GUIDs never contain ":", so already-prefixed rows are left alone."""
    for table, col in CHAT_ID_COLUMNS:
        db.execute(f"UPDATE {table} SET {col} = 'imessage:' || {col} WHERE {col} IS NOT NULL AND instr({col}, ':') = 0")


def _v3(db: sqlite3.Connection) -> None:
    """Tasks remember which backend owns them and, for session-based backends, the session ID."""
    db.execute("ALTER TABLE tasks ADD COLUMN backend TEXT NOT NULL DEFAULT 'mailbox'")
    db.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")


def _v4(db: sqlite3.Connection) -> None:
    """Managed Agents session events already handled, so each is acted on once."""
    db.execute("CREATE TABLE IF NOT EXISTS agent_events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
               " ts REAL NOT NULL)")


# MIGRATIONS[i] takes a database from user_version i to i + 1. Append only.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_v1, _v2, _v3, _v4]
SCHEMA_VERSION = len(MIGRATIONS)


def _statements(script: str) -> list[str]:
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


def schema_version(db: sqlite3.Connection) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def migrate(db: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION, one migration per transaction. Returns the final version."""
    version = schema_version(db)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"state.db is schema version {version}; this kinchat only knows up to {SCHEMA_VERSION}")
    for target in range(version + 1, SCHEMA_VERSION + 1):
        db.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[target - 1](db)
            db.execute(f"PRAGMA user_version = {target}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return SCHEMA_VERSION


@dataclass
class Message:
    guid: str
    chat_guid: str
    sender: str | None
    text: str
    ts: float
    from_mia: bool
    reply_to_guid: str | None = None


@dataclass
class Task:
    task_id: str
    chat_guid: str
    summary: str
    details: str | None
    status: str
    requested_by: str | None
    created_ts: float
    updated_ts: float
    email_msg_id: str | None
    backend: str = "mailbox"
    session_id: str | None = None


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            migrate(self._db)

    def _q(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # --- messages ---------------------------------------------------------

    def add_message(self, m: Message) -> bool:
        """Insert a message. Returns False if the GUID was already stored (dedup)."""
        cur = self._q(
            "INSERT OR IGNORE INTO messages (guid, chat_guid, sender, text, ts, from_mia, reply_to_guid)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (m.guid, m.chat_guid, m.sender, m.text, m.ts, int(m.from_mia), m.reply_to_guid),
        )
        return cur.rowcount == 1

    def recent_messages(self, chat_guid: str, limit: int) -> list[Message]:
        rows = self._q(
            "SELECT * FROM messages WHERE chat_guid = ? ORDER BY ts DESC LIMIT ?", (chat_guid, limit)
        ).fetchall()
        return [_msg(r) for r in reversed(rows)]

    def is_mia_message(self, guid: str) -> bool:
        row = self._q("SELECT from_mia FROM messages WHERE guid = ?", (guid,)).fetchone()
        return bool(row and row["from_mia"])

    def prune_messages(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        # Suggestions come from chat, so unapproved ones follow the chat retention too.
        self._q("DELETE FROM notes WHERE status != 'approved' AND created_ts < ?", (cutoff,))
        return self._q("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount

    # --- decisions --------------------------------------------------------

    def log_decision(self, message_guid: str, step: str, route: str, confidence: float | None = None,
                     intent: str | None = None, reason: str | None = None, cls_route: bool | None = None,
                     cls_reason: str | None = None) -> None:
        self._q(
            "INSERT INTO decisions (message_guid, step, route, confidence, intent, reason, ts, cls_route, cls_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_guid, step, route, confidence, intent, reason, time.time(),
             None if cls_route is None else int(cls_route), cls_reason),
        )

    def decisions_for(self, message_guid: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM decisions WHERE message_guid = ? ORDER BY id", (message_guid,)).fetchall()

    # --- window / quiet mode ---------------------------------------------

    def _window(self, chat_guid: str) -> sqlite3.Row:
        self._q("INSERT OR IGNORE INTO window (chat_guid) VALUES (?)", (chat_guid,))
        return self._q("SELECT * FROM window WHERE chat_guid = ?", (chat_guid,)).fetchone()

    def window_active(self, chat_guid: str, now: float | None = None) -> bool:
        return self._window(chat_guid)["active_until"] > (now or time.time())

    def open_window(self, chat_guid: str, minutes: float, now: float | None = None) -> None:
        self._window(chat_guid)
        self._q("UPDATE window SET active_until = ? WHERE chat_guid = ?",
                ((now or time.time()) + minutes * 60, chat_guid))

    def close_window(self, chat_guid: str) -> None:
        self._window(chat_guid)
        self._q("UPDATE window SET active_until = 0 WHERE chat_guid = ?", (chat_guid,))

    def quiet(self, chat_guid: str, now: float | None = None) -> bool:
        return self._window(chat_guid)["quiet_until"] > (now or time.time())

    def set_quiet(self, chat_guid: str, minutes: float, now: float | None = None) -> None:
        self._window(chat_guid)
        until = (now or time.time()) + minutes * 60 if minutes > 0 else 0
        self._q("UPDATE window SET quiet_until = ?, active_until = 0 WHERE chat_guid = ?", (until, chat_guid))

    # --- tasks ------------------------------------------------------------

    def next_task_id(self) -> str:
        with self._lock:
            row = self._q("SELECT value FROM kv WHERE key = 'task_seq'").fetchone()
            n = int(row["value"]) + 1 if row else 1
            self._q("INSERT OR REPLACE INTO kv (key, value) VALUES ('task_seq', ?)", (str(n),))
            return f"T-{n:04d}"

    def create_task(self, task_id: str, chat_guid: str, summary: str, details: str | None,
                    requested_by: str | None, backend: str = "mailbox", session_id: str | None = None) -> Task:
        now = time.time()
        self._q(
            "INSERT INTO tasks (task_id, chat_guid, summary, details, status, requested_by, created_ts, updated_ts,"
            " backend, session_id) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (task_id, chat_guid, summary, details, requested_by, now, now, backend, session_id),
        )
        return self.get_task(task_id)

    def update_task(self, task_id: str, **fields) -> None:
        fields["updated_ts"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._q(f"UPDATE tasks SET {cols} WHERE task_id = ?", (*fields.values(), task_id))

    def get_task(self, task_id: str) -> Task | None:
        row = self._q("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return Task(**dict(row)) if row else None

    def task_is_open(self, task_id: str) -> bool:
        t = self.get_task(task_id)
        return bool(t and t.status in OPEN_STATUSES)

    def relevant_tasks(self, chat_guid: str, finished_within_days: int = 3) -> list[Task]:
        cutoff = time.time() - finished_within_days * 86400
        rows = self._q(
            "SELECT * FROM tasks WHERE chat_guid = ? AND (status IN ('open', 'needs_input', 'paused_budget')"
            " OR updated_ts >= ?)"
            " ORDER BY created_ts",
            (chat_guid, cutoff),
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def open_task_count(self) -> int:
        return self._q("SELECT COUNT(*) FROM tasks WHERE status IN ('open', 'needs_input')").fetchone()[0]

    # --- outbox (quiet hours) ---------------------------------------------

    def hold(self, chat_guid: str, text: str) -> None:
        self._q("INSERT INTO outbox (chat_guid, text, ts) VALUES (?, ?, ?)", (chat_guid, text, time.time()))

    def take_held(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._q("SELECT chat_guid, text FROM outbox ORDER BY id").fetchall()
            self._q("DELETE FROM outbox")
        held: dict[str, list[str]] = {}
        for r in rows:
            held.setdefault(r["chat_guid"], []).append(r["text"])
        return held

    def recent_decisions(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- posted results ---------------------------------------------------

    def already_posted(self, graph_message_id: str) -> bool:
        return self._q("SELECT 1 FROM posted_results WHERE graph_message_id = ?", (graph_message_id,)).fetchone() is not None

    def mark_posted(self, graph_message_id: str, task_id: str) -> None:
        self._q("INSERT OR IGNORE INTO posted_results (graph_message_id, task_id, ts) VALUES (?, ?, ?)",
                (graph_message_id, task_id, time.time()))

    # --- kv ---------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self._q("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._q("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))


def _msg(r: sqlite3.Row) -> Message:
    return Message(guid=r["guid"], chat_guid=r["chat_guid"], sender=r["sender"], text=r["text"] or "",
                   ts=r["ts"], from_mia=bool(r["from_mia"]), reply_to_guid=r["reply_to_guid"])
