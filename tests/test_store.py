import sqlite3

import pytest

from kinchat.store import MIGRATIONS, SCHEMA_VERSION, Store, schema_version

# The decisions and kv tables as they were before the classifier columns and before schema versioning.
OLD_SCHEMA = """
CREATE TABLE messages (guid TEXT PRIMARY KEY, chat_guid TEXT NOT NULL, sender TEXT, text TEXT, ts REAL NOT NULL,
    from_mia INTEGER NOT NULL DEFAULT 0, reply_to_guid TEXT);
CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, message_guid TEXT NOT NULL, step TEXT NOT NULL,
    route TEXT NOT NULL, confidence REAL, intent TEXT, reason TEXT, ts REAL NOT NULL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
"""


def test_fresh_database_is_current_version(tmp_path):
    store = Store(tmp_path / "state.db")
    assert schema_version(store._db) == SCHEMA_VERSION == len(MIGRATIONS)
    Store(tmp_path / "state.db")  # reopening is a no-op
    assert schema_version(store._db) == SCHEMA_VERSION


def test_old_database_upgrades_in_place(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO decisions (message_guid, step, route, reason, ts) VALUES ('m1', 'cue', 'respond', 'r', 1)")
    db.execute("INSERT INTO kv VALUES ('chatdb_last_rowid', '42')")
    db.commit()
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    db.close()

    store = Store(path)
    assert schema_version(store._db) == SCHEMA_VERSION
    cols = {r["name"] for r in store._db.execute("PRAGMA table_info(decisions)")}
    assert {"cls_route", "cls_reason"} <= cols
    assert store.kv_get("chatdb_last_rowid") == "42"  # existing data survives
    old = store.decisions_for("m1")[0]
    assert old["reason"] == "r" and old["cls_route"] is None
    store.log_decision("m2", "classifier", "quiet", 0.9, "chitchat", "r", False, "cls")
    assert store.decisions_for("m2")[0]["cls_reason"] == "cls"
    store.open_window("chat", 5)  # tables missing from the old file were created
    assert store.window_active("chat")


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="schema version"):
        Store(path)


V1_ROWS = [
    "INSERT INTO messages (guid, chat_guid, sender, text, ts) VALUES ('m1', 'iMessage;+;chat1', 'Alex', 'hi', 1)",
    "INSERT INTO chats (chat_guid, name, mode, participants, first_seen) VALUES ('iMessage;+;chat1', 'Family', 'respond', '[]', 1)",
    "INSERT INTO window (chat_guid, active_until) VALUES ('iMessage;+;chat1', 99)",
    "INSERT INTO tasks (task_id, chat_guid, summary, status, created_ts, updated_ts)"
    " VALUES ('T-0001', 'iMessage;-;+18005550100', 's', 'open', 1, 1)",
    "INSERT INTO outbox (chat_guid, text, ts) VALUES ('iMessage;+;chat1', 'held', 1)",
    "INSERT INTO notes (text, chat_guid, created_ts) VALUES ('n', 'iMessage;+;chat1', 1)",
    "INSERT INTO notes (text, chat_guid, created_ts) VALUES ('no chat', NULL, 1)",
]


def test_v2_namespaces_existing_chat_ids(tmp_path):
    from kinchat.store import SCHEMA_V1, _statements
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    for stmt in _statements(SCHEMA_V1):
        db.execute(stmt)
    for stmt in V1_ROWS:
        db.execute(stmt)
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()

    store = Store(path)
    assert schema_version(store._db) == SCHEMA_VERSION
    chat = "imessage:iMessage;+;chat1"
    assert [m.guid for m in store.recent_messages(chat, 5)] == ["m1"]
    assert store._q("SELECT mode FROM chats WHERE chat_guid = ?", (chat,)).fetchone()[0] == "respond"
    assert store.window_active(chat, now=50)
    assert store.get_task("T-0001").chat_guid == "imessage:iMessage;-;+18005550100"
    assert store.take_held() == {chat: ["held"]}
    notes = [tuple(r) for r in store._q("SELECT text, chat_guid FROM notes ORDER BY id")]
    assert notes == [("n", chat), ("no chat", None)]
    Store(path)  # idempotent: nothing is prefixed twice
    t = store.get_task("T-0001")
    assert t.chat_guid == "imessage:iMessage;-;+18005550100"
    assert (t.backend, t.session_id) == ("mailbox", None)  # v3: existing tasks belong to the mailbox backend
