import sqlite3
import subprocess
import sys

import pytest

from kinchat import secrets
from kinchat.app import Bridge
from kinchat.config import Config

BASE = "http://127.0.0.1:8770"


@pytest.fixture
def no_secrets(monkeypatch):
    monkeypatch.setattr(secrets, "_keychain", lambda service, account: None)
    for prefix in ("KINCHAT_", "MIA_BRIDGE_"):
        for account in ("ANTHROPIC_API_KEY", "DASHBOARD_PASSWORD", "GRAPH_CERT_KEY_B64"):
            monkeypatch.delenv(prefix + account, raising=False)


@pytest.fixture
def bare_cfg(tmp_path):
    cfg = Config(shadow_mode=True)
    cfg.db_path = tmp_path / "state.db"
    cfg.family_md = tmp_path / "family.md"
    cfg.messages.chat_db = str(tmp_path / "missing-chat.db")
    return cfg


def states(bridge):
    return {s.name: s.state for s in bridge.health.snapshot()}


def test_starts_without_secrets_or_graph(no_secrets, bare_cfg):
    bridge = Bridge(bare_cfg)
    st = states(bridge)
    assert st["llm"] == st["mail"] == st["calendar"] == st["dashboard"] == "failed"
    assert bridge.llm is None and bridge.graph is None and bridge.dashboard_password is None
    errors = {s.name: s.last_error for s in bridge.health.snapshot()}
    assert "anthropic-api-key" in errors["llm"] and "not configured" in errors["mail"]

    client = bridge.create_dashboard().test_client()
    page = client.get("/login", base_url=BASE).get_data(as_text=True)
    assert "No dashboard password is set" in page
    client.post("/login", data={"password": ""}, base_url=BASE)
    assert client.get("/", base_url=BASE).status_code == 302  # nobody can sign in


def test_degraded_paths_do_not_raise(no_secrets, bare_cfg):
    bridge = Bridge(bare_cfg)
    bridge._heartbeat()  # no Graph: skipped
    assert bridge._classify([], None, [], []) is None
    with pytest.raises(sqlite3.OperationalError):
        bridge._start_rowid()  # chat.db missing: the watcher catches this and reports it
    bridge.h_chatdb.degraded(sqlite3.OperationalError("unable to open database file"))
    assert states(bridge)["chatdb"] == "degraded"


def test_health_shown_on_dashboard(no_secrets, bare_cfg, monkeypatch):
    monkeypatch.setenv("KINCHAT_DASHBOARD_PASSWORD", "pw")
    bridge = Bridge(bare_cfg)
    assert states(bridge)["dashboard"] == "ok"
    client = bridge.create_dashboard().test_client()
    client.post("/login", data={"password": "pw"}, base_url=BASE)
    page = client.get("/", base_url=BASE).get_data(as_text=True)
    assert "<h2>Health</h2>" in page and "Claude API" in page and "graph not configured" in page


def test_graph_and_msal_imported_lazily():
    code = "import sys, kinchat.app; print('msal' in sys.modules, 'kinchat.graph' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]
