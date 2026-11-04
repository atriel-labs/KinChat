from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import CHAT, DM_ALEX

from kinchat.outbound import Outbound, PostStatus

TZ = ZoneInfo("America/Denver")


class Clock:
    def __init__(self, hour=12):
        self.t = datetime(2026, 11, 3, hour, 0, tzinfo=TZ).timestamp()

    def __call__(self):
        return self.t


def make(store, settings, clock, sent, shadow=False):
    return Outbound(store, settings, lambda c, t: sent.append((c, t)), 6, 12, shadow, TZ, 22, 7, clock)


def test_rate_limit_queues_and_combines(store, settings):
    sent, clock = [], Clock()
    out = make(store, settings, clock, sent)
    statuses = [out.post(CHAT, f"m{i}") for i in range(8)]
    assert statuses == [PostStatus.SENT] * 6 + [PostStatus.QUEUED] * 2
    assert [t for _, t in sent] == [f"m{i}" for i in range(6)]
    out.tick()
    assert len(sent) == 6
    clock.t += 601
    out.tick()
    assert sent[-1] == (CHAT, "m6\n\nm7")
    assert store.window_active(CHAT, clock.t)


def test_quiet_hours_hold_until_morning(store, settings):
    sent, clock = [], Clock(hour=23)
    out = make(store, settings, clock, sent)
    assert out.post(CHAT, "Booked for Saturday!") == PostStatus.HELD
    assert out.post(CHAT, "Also, the dentist moved.") == "held"  # a StrEnum compares equal to its value
    out.tick()
    assert sent == []
    clock.t += 8 * 3600  # 7 AM
    out.tick()
    assert sent == [(CHAT, "Booked for Saturday!\n\nAlso, the dentist moved.")]


def test_urgent_bypasses_quiet_hours(store, settings):
    sent, clock = [], Clock(hour=2)
    assert make(store, settings, clock, sent).post(CHAT, "The pharmacy is 303-555-0199", urgent=True) == "sent"
    assert sent == [(CHAT, "The pharmacy is 303-555-0199")]


def test_quiet_hours_toggle_off(store, settings):
    settings.set_feature("quiet_hours", False)
    sent, clock = [], Clock(hour=23)
    assert make(store, settings, clock, sent).post(CHAT, "hi") == PostStatus.SENT
    assert sent == [(CHAT, "hi")]


def test_dashboard_blocks_posting(store, settings):
    sent, clock = [], Clock()
    out = make(store, settings, clock, sent)
    assert out.post(DM_ALEX, "hi") == PostStatus.REFUSED  # one-on-one toggle off
    settings.set_chat_mode(CHAT, "listen")
    assert out.post(CHAT, "hi") == PostStatus.REFUSED
    settings.set_chat_mode(CHAT, "respond")
    settings.set_feature("mia_enabled", False)
    assert out.post(CHAT, "hi") == PostStatus.REFUSED
    assert out.post(CHAT, "   ") == PostStatus.REFUSED
    assert sent == []


def test_shadow_mode_never_sends(store, settings):
    sent = []
    assert make(store, settings, Clock(), sent, shadow=True).post(CHAT, "hi") == PostStatus.SHADOW
    assert sent == []


def test_send_failure_reported(store, settings):
    def boom(chat, text):
        raise RuntimeError("osascript failed")
    out = Outbound(store, settings, boom, 6, 12, False, TZ, 22, 7, Clock())
    assert out.post(CHAT, "hi") == PostStatus.FAILED
    assert not store.window_active(CHAT)
