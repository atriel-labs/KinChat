import sqlite3

import pytest
from conftest import ALEX, SAM
from test_messages import make_db

from kinchat.backends.base import TransportRegistry, split_id
from kinchat.backends.fake import FakeTransport
from kinchat.backends.imessage import BOOKMARK_KEY, IMessageTransport
from kinchat.config import Config
from kinchat.llm import Classification, Reply
from kinchat.outbound import Outbound, PostStatus
from kinchat.router import Router
from kinchat.settings import Settings, imessage_is_group


def test_split_id():
    assert split_id("imessage:iMessage;+;x") == ("imessage", "iMessage;+;x")
    for bad in ("iMessage;+;x", ":x", "fake:"):
        with pytest.raises(ValueError):
            split_id(bad)


def test_registry_routes_by_prefix():
    a, b = FakeTransport("alpha"), FakeTransport("beta")
    reg = TransportRegistry([a, b])
    a.add_group("g", [ALEX])
    reg.send("beta:dm", "hi")
    assert b.sent == [("beta:dm", "hi")] and a.sent == []
    assert reg.is_group("alpha:g") and not reg.is_group("beta:g")
    assert not reg.is_group("nope:g") and not reg.is_group("unprefixed")
    with pytest.raises(KeyError):
        reg.send("nope:x", "hi")
    with pytest.raises(ValueError):
        reg.add(FakeTransport("alpha"))


def test_default_is_group_rule():
    assert imessage_is_group("imessage:iMessage;+;chat1")
    assert not imessage_is_group("imessage:iMessage;-;+18005550100")
    assert not imessage_is_group("iMessage;+;chat1")  # un-namespaced IDs are never groups
    assert not imessage_is_group("fake:iMessage;+;chat1")


def test_imessage_transport_owns_bookmark(tmp_path, store):
    path = tmp_path / "chat.db"
    make_db(path)
    sent = []
    t = IMessageTransport(path, store, send=lambda guid, text: sent.append((guid, text)))
    assert t.poll() == [] and store.kv_get(BOOKMARK_KEY) == "5"  # first run starts from the newest row
    store.kv_set(BOOKMARK_KEY, "0")
    t = IMessageTransport(path, store, send=lambda guid, text: sent.append((guid, text)))
    msgs = t.poll()
    assert [m.guid for m in msgs] == ["g1", "g2", "g3", "g5"] and t.health.state == "ok"
    t.ack(msgs[1])
    assert store.kv_get(BOOKMARK_KEY) == "2"
    assert [m.guid for m in IMessageTransport(path, store).poll()] == ["g3", "g5"]  # a restart resumes there
    t.send("imessage:iMessage;+;chat123", "hello")
    assert sent == [("iMessage;+;chat123", "hello")] and t.send_health.state == "ok"
    assert t.participants("imessage:iMessage;+;chat123") == [ALEX, SAM]
    assert t.is_group("imessage:iMessage;+;chat123") and not t.is_group("imessage:iMessage;-;+18005550100")


def test_imessage_transport_reports_unreadable_db(tmp_path, store):
    t = IMessageTransport(tmp_path / "missing.db", store)
    with pytest.raises(sqlite3.OperationalError):
        t.poll()
    assert t.health.state == "degraded"


# --- end to end through the fake transport ----------------------------------

class FakeLLM:
    def __init__(self):
        self.replies = []

    def classify(self, history, newest, tasks, names, tz):
        return Classification(route_to_mia=True, confidence=0.9, intent="question", action_needed=False,
                              task_summary=None, reason="test", close_window=False)

    def reply(self, history, *args):
        self.replies.append(history)
        return Reply(message="Yes, you're free Saturday.", task=None)


def test_router_and_outbound_through_fake_transport(store):
    fake = FakeTransport()
    reg = TransportRegistry([fake])
    chat = fake.add_group("family", [ALEX, SAM])
    settings = Settings(store, is_group=reg.is_group)
    settings.add_contact("Alex", [ALEX], one_on_one=False, can_assign=True)
    settings.add_contact("Sam", [SAM], one_on_one=False, can_assign=True)
    settings.register_pending(chat, "Family", [ALEX, SAM])
    settings.set_chat_mode(chat, "respond")
    settings.set_feature("quiet_hours", False)
    cfg = Config(shadow_mode=False)
    router = Router(cfg, store, settings, lambda *a: None, reg.participants)
    out = Outbound(store, settings, reg.send, 6, 12, False, cfg.tz)

    fake.deliver("family", ALEX, "Mia, are we free Saturday?")
    (inc,) = fake.poll()
    d = router.route(inc)
    assert d.respond and d.step == "cue"
    assert out.post(inc.chat_guid, "Yes!") == PostStatus.SENT
    assert fake.sent == [("fake:family", "Yes!")]
    fake.ack(inc)
    (echo,) = fake.poll()  # our own message comes back through the transport
    assert not router.route(echo).respond and store.is_mia_message(echo.guid)
    assert out.post("fake:dm", "hi") == PostStatus.REFUSED  # one-on-one is off; fake:dm is not a group


def test_bridge_end_to_end_with_fake_transport(no_secrets, bare_cfg):
    from kinchat.app import Bridge
    bare_cfg.chat.transports = ["fake"]
    bare_cfg.shadow_mode = False
    bridge = Bridge(bare_cfg)
    fake = bridge.transports.get("fake")
    assert {s.name for s in bridge.health.snapshot()} >= {"fake-transport", "llm"}
    bridge.llm = FakeLLM()
    chat = fake.add_group("family", [ALEX])
    bridge.settings.add_contact("Alex", [ALEX], one_on_one=False, can_assign=True)
    bridge.settings.register_pending(chat, "Family", [ALEX])
    bridge.settings.set_chat_mode(chat, "respond")
    bridge.settings.set_feature("quiet_hours", False)
    fake.deliver("family", ALEX, "Mia, are we free Saturday?")
    assert bridge.poll_once(fake) == 1
    assert fake.sent == [("fake:family", "Yes, you're free Saturday.")]
    assert bridge.poll_once(fake) == 1  # the echo of our reply, stored, not answered
    assert len(fake.sent) == 1
    assert [m.sender for m in bridge.store.recent_messages(chat, 5)] == ["Alex", "Mia"]


def test_default_config_uses_imessage(no_secrets, bare_cfg):
    from kinchat.app import Bridge
    assert Config().chat.transports == ["imessage"]
    bridge = Bridge(bare_cfg)
    assert [t.name for t in bridge.transports] == ["imessage"]
    names = {s.name: s.label for s in bridge.health.snapshot()}
    assert names["chatdb"] == "chat.db watcher" and names["messages"] == "Messages sending (Automation)"
