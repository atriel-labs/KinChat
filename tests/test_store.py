import sqlite3

import pytest

from kinchat.store import SCHEMA_VERSION, Store, schema_version

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
    assert schema_version(store._db) == SCHEMA_VERSION == 1
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
    assert schema_version(store._db) == 1
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
