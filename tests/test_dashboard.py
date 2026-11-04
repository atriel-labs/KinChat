import re
from zoneinfo import ZoneInfo

import pytest

from mia_bridge.dashboard import create_app

from conftest import CHAT

BASE = "http://127.0.0.1:8770"


@pytest.fixture
def client(settings, store):
    app = create_app(settings, store, "hunter2", ZoneInfo("America/Denver"), shadow=False)
    return app.test_client()


def login(client):
    client.post("/login", data={"password": "hunter2"}, base_url=BASE)
    page = client.get("/", base_url=BASE).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_requires_login(client):
    r = client.get("/", base_url=BASE)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    client.post("/login", data={"password": "wrong"}, base_url=BASE)
    assert client.get("/", base_url=BASE).status_code == 302


def test_rejects_non_local_host(client):
    assert client.get("/login", base_url="http://evil.example").status_code == 403


def test_toggle_writes_setting_and_audit(client, settings):
    csrf = login(client)
    form = {"csrf": csrf, "mia_enabled": "on", "task_handoff": "on"}  # instant replies etc. now off
    assert client.post("/features", data=form, base_url=BASE).status_code == 302
    assert settings.feature("mia_enabled") and not settings.feature("instant_replies")
    item = next(a for a in settings.audit_log() if a["item"] == "feature instant_replies")
    assert (item["old"], item["new"]) == ("on", "off")


def test_post_without_csrf_rejected(client, settings):
    login(client)
    assert client.post("/features", data={}, base_url=BASE).status_code == 403
    assert settings.feature("mia_enabled")


def test_contacts_and_chat_mode(client, settings):
    csrf = login(client)
    client.post("/contacts", data={"csrf": csrf, "name": "Gran", "handles": "+19995550000, gran@example.com",
                                   "can_assign": "on"}, base_url=BASE)
    gran = settings.contact_for_handle("gran@example.com")
    assert gran and gran.name == "Gran" and not gran.one_on_one and gran.can_assign
    client.post(f"/contacts/{gran.id}/delete", data={"csrf": csrf}, base_url=BASE)
    assert settings.contact_for_handle("gran@example.com") is None
    client.post("/chats", data={"csrf": csrf, "chat_guid": CHAT, "mode": "listen"}, base_url=BASE)
    assert settings.get_chat(CHAT).mode == "listen"
    page = client.get("/", base_url=BASE).get_data(as_text=True)
    assert "Gran" in page and "Listen only" in page
