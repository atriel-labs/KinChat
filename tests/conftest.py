import time

import pytest

from kinchat.config import Config
from kinchat.llm import Classification
from kinchat.messages import Incoming
from kinchat.settings import Settings
from kinchat.store import Store

CHAT = "iMessage;+;chat123"
ALEX = "+18005550100"
SAM = "+18005550101"
DM_ALEX = f"iMessage;-;{ALEX}"


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
