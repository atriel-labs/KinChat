import hashlib
import json
import plistlib
import sqlite3

import pytest
from fakebin import FakeBin

from kinchat import migrate, paths
from kinchat.store import SCHEMA_VERSION, Store, schema_version

OLD_CONFIG = """# my config, with comments
shadow_mode: false
graph:
  tenant: example.com
  mailbox: mia@example.com
timezone: America/New_York
"""


def make_old_install(home, fake, python="/opt/old/python3.12"):
    old = paths.legacy_app_dir(home)
    old.mkdir(parents=True)
    (old / "config.yaml").write_text(OLD_CONFIG)
    (old / "family.md").write_text("# Family notes\n- Alex: call him Al\n")
    plist = paths.legacy_plist(home)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": paths.LEGACY_LABEL,
                                      "ProgramArguments": [python, "-m", "mia_bridge", "run"]}))
    fake.load(paths.LEGACY_LABEL)
    fake.set_secret("ai.atriel.mia-bridge", "anthropic-api-key", "sk-old-key")
    fake.set_secret("ai.atriel.mia-bridge", "dashboard-password", "old-pw")
    return old


def wal_db_with_unflushed_rows(path):
    """A v0 (pre-versioning) state.db in WAL mode whose newest rows exist only in the -wal file."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.executescript("""
        CREATE TABLE messages (guid TEXT PRIMARY KEY, chat_guid TEXT NOT NULL, sender TEXT, text TEXT,
            ts REAL NOT NULL, from_mia INTEGER NOT NULL DEFAULT 0, reply_to_guid TEXT);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """)
    db.execute("INSERT INTO kv VALUES ('chatdb_last_rowid', '4242')")
    db.commit()
    for i in range(5):
        db.execute("INSERT INTO messages (guid, chat_guid, sender, text, ts) VALUES (?, ?, 'Alex', 'hi', ?)",
                   (f"m{i}", "iMessage;+;chat1", i))
        db.commit()
    return db  # left open: closing would checkpoint the WAL into the main file


def digest(directory):
    # -shm is SQLite's shared-memory index, which every reader updates; the data (the db and its -wal) must not change.
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())
            if p.is_file() and not p.name.endswith("-shm")}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fb = FakeBin(tmp_path / "fake")
    monkeypatch.setenv("PATH", fb.path_env())
    return fb


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def test_migrate_rollback_migrate(home, fake, tmp_path):
    old = make_old_install(home, fake)
    held = wal_db_with_unflushed_rows(old / "state.db")
    assert (old / "state.db-wal").stat().st_size > 0
    before = digest(old)
    out = []

    entry = migrate.migrate(home, python="/opt/new/python3.13", out=out.append)
    new = paths.app_dir(home)
    assert digest(old) == before  # the old directory is untouched
    store = Store(new / "state.db")
    assert schema_version(store._db) == SCHEMA_VERSION
    assert [m.guid for m in store.recent_messages("imessage:iMessage;+;chat1", 10)] == [f"m{i}" for i in range(5)]
    assert store.kv_get("chatdb_last_rowid") == "4242"  # the bridge resumes at the same chat.db row
    store._db.close()
    assert (new / "family.md").read_text() == (old / "family.md").read_text()
    cfg_text = (new / "config.yaml").read_text()
    assert cfg_text.startswith("# my config, with comments") and 'handles: ["mia@example.com"]' in cfg_text
    kc = fake.keychain()
    assert kc["kinchat/anthropic-api-key"] == "sk-old-key" and kc["ai.atriel.mia-bridge/anthropic-api-key"] == "sk-old-key"
    assert entry["keychain_copied"] == ["anthropic-api-key", "dashboard-password"]
    assert fake.loaded() == {paths.LABEL}
    assert not paths.legacy_plist(home).exists()
    assert paths.legacy_plist(home).with_name("ai.atriel.mia-bridge.plist.disabled").exists()
    assert any("Full Disk Access" in w for w in entry["warnings"])  # /opt/new/... differs from /opt/old/...
    record = json.loads((new / "migration.json").read_text())
    assert record["runs"][0]["action"] == "migrate" and "sk-old-key" not in (new / "migration.json").read_text()
    assert "sk-old-key" not in "\n".join(out)

    migrate.rollback(home, out=out.append)
    assert fake.loaded() == {paths.LEGACY_LABEL}
    assert paths.legacy_plist(home).exists() and not paths.plist(home).exists()
    assert paths.plist(home).with_name("io.kinchat.bridge.plist.disabled").exists()
    assert (new / "state.db").exists() and "kinchat/anthropic-api-key" in fake.keychain()  # left in place
    assert digest(old) == before

    # The old bridge ran again for a while, then the owner migrates a second time.
    held.execute("INSERT INTO messages (guid, chat_guid, sender, text, ts) VALUES ('m9', 'iMessage;+;chat1', 'Sam', 'later', 9)")
    held.commit()
    entry = migrate.migrate(home, python="/opt/old/python3.12", out=out.append)
    store = Store(new / "state.db")
    assert "m9" in [m.guid for m in store.recent_messages("imessage:iMessage;+;chat1", 10)]
    store._db.close()
    assert any(".pre-migrate-" in p for p in entry["kept_aside"])  # the first copy was kept, not overwritten
    assert entry["warnings"] == []  # same python as before: no Full Disk Access warning
    assert fake.loaded() == {paths.LABEL}
    assert len(json.loads((new / "migration.json").read_text())["runs"]) == 3
    held.close()


def test_refuses_without_old_agent_unless_forced(home, fake):
    make_old_install(home, fake)
    migrate.migrate(home, python="/opt/old/python3.12", out=lambda *a: None)
    with pytest.raises(migrate.MigrateError, match="already migrated"):
        migrate.migrate(home, out=lambda *a: None)
    migrate.migrate(home, force=True, python="/opt/old/python3.12", out=lambda *a: None)
    assert fake.loaded() == {paths.LABEL}


def test_nothing_to_migrate(home, fake):
    with pytest.raises(migrate.MigrateError):
        migrate.migrate(home, out=lambda *a: None)
    with pytest.raises(migrate.MigrateError, match="does not exist"):
        migrate.migrate(home, force=True, out=lambda *a: None)


def test_rollback_without_old_plist(home, fake):
    with pytest.raises(migrate.MigrateError, match="nothing|no ai.atriel"):
        migrate.rollback(home, out=lambda *a: None)


def test_handles_not_overwritten(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("assistant:\n  name: Mia\n  handles: [mia@example.com]\ngraph:\n  mailbox: other@example.com\n")
    assert migrate.fill_handles(cfg) is None
    cfg.write_text("assistant:\n  name: Mia\ngraph:\n  mailbox: mia@example.com\n")
    assert migrate.fill_handles(cfg) == "mia@example.com"
    import yaml
    assert yaml.safe_load(cfg.read_text())["assistant"] == {"name": "Mia", "handles": ["mia@example.com"]}


def test_cli(home, fake, monkeypatch, capsys):
    from kinchat import __main__ as cli
    monkeypatch.setattr(migrate.Path, "home", classmethod(lambda cls: home))
    assert cli.main(["migrate"]) == 2
    assert cli.main(["migrate", "--from", "mia-bridge"]) == 1
    assert "migrate:" in capsys.readouterr().err


def test_migrate_never_puts_secrets_on_a_command_line(home, fake):
    make_old_install(home, fake)
    fake.set_secret("ai.atriel.mia-bridge", "ics-urls", "https://cal.example.com/a.ics\nhttps://cal.example.com/b.ics")
    migrate.migrate(home, python="/opt/old/python3.12", out=lambda *a: None)
    kc = fake.keychain()
    assert kc["kinchat/anthropic-api-key"] == "sk-old-key"
    assert kc["kinchat/ics-urls"] == "https://cal.example.com/a.ics https://cal.example.com/b.ics"
    for call in fake.calls():
        joined = " ".join(call)
        assert "sk-old-key" not in joined and "old-pw" not in joined and "cal.example.com" not in joined
