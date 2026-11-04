import sqlite3
import subprocess
import sys

import pytest

from kinchat.app import Bridge

BASE = "http://127.0.0.1:8770"


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
    imessage = bridge.transports.get("imessage")
    with pytest.raises(sqlite3.OperationalError):
        bridge.poll_once(imessage)  # chat.db missing: the watcher catches this; the transport reports it
    assert states(bridge)["chatdb"] == "degraded" and "unable to open" in imessage.health.status.last_error


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


class FakeGraphMail:
    name = "graph"
    address = "mia@example.com"

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return "<id>"

    def fetch_results(self, subject_re, tag):
        return []

    def commit(self):
        pass


def test_default_task_backend_is_mailbox_with_heartbeat(no_secrets, bare_cfg):
    bare_cfg.shadow_mode = False
    bridge = Bridge(bare_cfg)
    assert bridge.tasks.name == "mailbox" and bridge.tasks.health.status.name == "mail"
    bridge._heartbeat()  # no mail transport (Graph not configured): skipped
    bridge.tasks.mail = FakeGraphMail()
    bridge._heartbeat()
    (subject, body), = bridge.tasks.mail.sent
    assert subject.startswith("[MIA-HEARTBEAT] ") and "Mia enabled: yes" in body and "Open tasks: 0" in body


def test_none_task_backend(no_secrets, bare_cfg):
    bare_cfg.tasks.backend = "none"
    bare_cfg.shadow_mode = False
    bridge = Bridge(bare_cfg)
    assert bridge.tasks.name == "none" and not bridge.tasks.accepts_tasks
    bridge._heartbeat()  # no heartbeat without the mailbox backend
    assert "mail" not in {s.name for s in bridge.health.snapshot()}
