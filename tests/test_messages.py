import sqlite3

from mia_bridge.messages import APPLE_EPOCH, ChatDB, apple_ts, decode_attributed_body

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER, display_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, handle_id INTEGER,
    is_from_me INTEGER, date INTEGER, associated_message_type INTEGER, thread_originator_guid TEXT, item_type INTEGER);
"""


def typedstream(s: str) -> bytes:
    raw = s.encode()
    n = len(raw)
    length = bytes([n]) if n < 0x80 else b"\x81" + n.to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject" \
           b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + raw + b"\x86\x84\x02iI"


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO handle VALUES (?, ?)", [(1, "+13035550100"), (2, "+13035550101")])
    db.executemany("INSERT INTO chat VALUES (?, ?, ?, ?)",
                   [(1, "iMessage;+;chat123", 43, "Family"), (2, "iMessage;-;+13035550100", 45, "")])
    db.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (1, 2), (2, 1)])
    ns = (1_762_000_000 - APPLE_EPOCH) * 1_000_000_000
    rows = [
        (1, "g1", "Mia, are we free?", None, 1, 0, ns, 0, None, 0, 1),
        (2, "g2", None, typedstream("from attributedBody " + "x" * 200), 2, 0, ns, 0, "g1", 0, 1),
        (3, "g3", "Loved “x”", None, 2, 0, ns, 2000, None, 0, 1),
        (4, "g4", None, None, 1, 0, ns, 0, None, 1, 1),  # member added
        (5, "g5", "hi", None, 1, 0, ns, 0, None, 0, 2),
    ]
    for r in rows:
        db.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r[:10])
        db.execute("INSERT INTO chat_message_join VALUES (?, ?)", (r[10], r[0]))
    db.commit()
    db.close()


def test_reads_new_messages(tmp_path):
    p = tmp_path / "chat.db"
    make_db(p)
    db = ChatDB(p)
    msgs = db.new_messages(0)
    assert [m.guid for m in msgs] == ["g1", "g2", "g3", "g5"]  # system row skipped
    g1, g2, g3, g5 = msgs
    assert g1.is_group and g1.chat_name == "Family" and g1.handle == "+13035550100"
    assert g1.ts == 1_762_000_000
    assert g2.text.startswith("from attributedBody") and len(g2.text) == 220 and g2.thread_originator_guid == "g1"
    assert g3.is_tapback
    assert not g5.is_group
    assert db.new_messages(5) == []
    assert db.max_rowid() == 5
    assert db.participants("iMessage;+;chat123") == ["+13035550100", "+13035550101"]


def test_decode_helpers():
    assert decode_attributed_body(typedstream("hello")) == "hello"
    assert decode_attributed_body(None) is None
    assert apple_ts(0) == 0.0
    assert apple_ts(100) == 100 + APPLE_EPOCH
