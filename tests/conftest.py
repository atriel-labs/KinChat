import time

import pytest

from kinchat import secrets
from kinchat.backends.base import Incoming
from kinchat.config import Config
from kinchat.llm import Classification
from kinchat.settings import Settings
from kinchat.store import Store

CHAT = "imessage:iMessage;+;chat123"
ALEX = "+18005550100"
SAM = "+18005550101"
DM_ALEX = f"imessage:iMessage;-;{ALEX}"


@pytest.fixture
def cfg():
    return Config(shadow_mode=False)


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def settings(store):
    s = Settings(store)
    s.add_contact("Alex", [ALEX, "alex@example.com"], one_on_one=True, can_assign=True)
    s.add_contact("Sam", [SAM], one_on_one=True, can_assign=True)
    s.register_pending(CHAT, "Family", [ALEX, SAM])
    s.set_chat_mode(CHAT, "respond")
    return s


_n = 0


def incoming(text, handle=ALEX, chat=CHAT, from_me=False, reply_to=None, tapback=False, ts=None, guid=None,
             group=None):
    global _n
    _n += 1
    return Incoming(rowid=_n, guid=guid or f"msg-{_n}", chat_guid=chat,
                    is_group=(";+;" in chat) if group is None else group, chat_name=None, handle=handle,
                    text=text, is_from_me=from_me, ts=ts or time.time(), thread_originator_guid=reply_to,
                    is_tapback=tapback)


def cls(route=True, conf=0.9, intent="question", close=False):
    return Classification(route_to_mia=route, confidence=conf, intent=intent, action_needed=False,
                          task_summary=None, reason="test", close_window=close)


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
