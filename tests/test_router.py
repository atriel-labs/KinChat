import time

from conftest import CHAT, DM_ALEX, SAM, cls, incoming

from kinchat.router import BACK_ACK, SIT_OUT_ACK, Router


class FakeClassifier:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def __call__(self, history, msg, tasks, names):
        self.calls += 1
        return self.result


def make(cfg, store, settings, result=None, members=("+13035550100", SAM)):
    fc = FakeClassifier(result)
    return Router(cfg, store, settings, fc, lambda guid: list(members)), fc


# --- dashboard rules --------------------------------------------------------

def test_new_group_shows_pending_and_is_dropped(cfg, store, settings):  # acceptance 9 + dashboard
    r, fc = make(cfg, store, settings, cls())
    d = r.route(incoming("Mia, hi", chat="iMessage;+;chatNEW"))
    assert not d.respond and fc.calls == 0
    assert settings.get_chat("iMessage;+;chatNEW").mode == "pending"
    assert store.recent_messages("iMessage;+;chatNEW", 10) == []
    assert any(a["new"] == "pending" for a in settings.audit_log())


def test_off_chat_dropped(cfg, store, settings):
    settings.set_chat_mode(CHAT, "off")
    r, fc = make(cfg, store, settings, cls())
    assert not r.route(incoming("Mia, hi")).respond and fc.calls == 0


def test_listen_only_stores_context_never_speaks(cfg, store, settings):
    settings.set_chat_mode(CHAT, "listen")
    r, fc = make(cfg, store, settings, cls())
    assert not r.route(incoming("Mia, hi")).respond and fc.calls == 0
    assert len(store.recent_messages(CHAT, 10)) == 1


def test_unapproved_sender_ignored(cfg, store, settings):
    r, fc = make(cfg, store, settings, cls())
    d = r.route(incoming("Mia, book a table", handle="+19995550000"))
    assert not d.respond and fc.calls == 0 and "approved contact" in d.reason


def test_stranger_added_to_approved_group_sends_it_back_to_pending(cfg, store, settings):
    r, fc = make(cfg, store, settings, cls(), members=("+13035550100", SAM, "+19995550000"))
    d = r.route(incoming("Mia, are we free Saturday?"))
    assert not d.respond and fc.calls == 0
    chat = settings.get_chat(CHAT)
    assert chat.mode == "pending" and "+19995550000" in chat.participants
    assert not settings.can_speak_in(CHAT)
    assert any("new member +19995550000" in a["item"] for a in settings.audit_log())


def test_mia_own_handle_is_not_a_stranger(cfg, store, settings):
    cfg.graph.mailbox = "mia@example.com"  # the bridge's own handle; becomes assistant.handles in Phase 1
    r, _ = make(cfg, store, settings, cls(), members=("+13035550100", SAM, "mia@example.com"))
    assert r.route(incoming("Mia, hi")).respond


def test_contact_change_applies_immediately(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(), members=("+13035550100", SAM, "+19995550000"))
    settings.add_contact("Gran", ["+19995550000"], one_on_one=False, can_assign=False)
    assert r.route(incoming("Mia, hi", handle="+1 (999) 555-0000")).respond


def test_master_switch_off(cfg, store, settings):
    settings.set_feature("mia_enabled", False)
    r, fc = make(cfg, store, settings, cls())
    assert not r.route(incoming("Mia, hi")).respond and fc.calls == 0
    assert len(store.recent_messages(CHAT, 10)) == 1  # still reads and logs


def test_followups_off_requires_name(cfg, store, settings):
    settings.set_feature("followups", False)
    r, fc = make(cfg, store, settings, cls())
    assert r.route(incoming("Mia, are we free Saturday?")).respond
    assert not r.route(incoming("what about Sunday?")).respond
    assert fc.calls == 1


def test_one_on_one_rules(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.1))
    assert not r.route(incoming("hey", chat=DM_ALEX)).respond  # toggle off by default
    settings.set_feature("one_on_one", True)
    d = r.route(incoming("hey", chat=DM_ALEX))
    assert d.respond and d.reason == "direct message"
    contact = settings.contact_for_handle(SAM)
    settings.update_contact(contact.id, "Sam", [SAM], one_on_one=False, can_assign=True)
    assert not r.route(incoming("hey", handle=SAM, chat=f"iMessage;-;{SAM}")).respond


# --- routing ------------------------------------------------------------------

def test_own_messages_stored_not_answered(cfg, store, settings):
    r, fc = make(cfg, store, settings, cls())
    assert not r.route(incoming("On it", from_me=True, guid="mia-1")).respond and fc.calls == 0
    assert store.is_mia_message("mia-1")


def test_own_reactions_not_stored(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls())
    r.route(incoming("", from_me=True, guid="mia-empty"))
    r.route(incoming("Loved “x”", from_me=True, tapback=True, guid="mia-tap"))
    assert store.recent_messages(CHAT, 10) == []


def test_tapback_and_empty_dropped(cfg, store, settings):
    r, fc = make(cfg, store, settings, cls())
    assert not r.route(incoming("Loved “x”", tapback=True)).respond
    assert not r.route(incoming("")).respond
    assert fc.calls == 0


def test_mention_responds_and_opens_window(cfg, store, settings):  # acceptance 1
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.1))
    d = r.route(incoming("Mia, are we free Saturday night?"))
    assert d.respond and d.step == "cue" and d.contact.name == "Alex"
    assert store.window_active(CHAT)


def test_mention_is_word_match(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.1))
    assert not r.route(incoming("Ask Amiable about it")).respond


def test_follow_up_in_window_responds(cfg, store, settings):  # acceptance 2
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.3))
    r.route(incoming("Mia, are we free Saturday night?"))
    d = r.route(incoming("what about Sunday?"))
    assert d.respond and d.step == "window"


def test_window_suppressed_when_clearly_not_for_mia(cfg, store, settings):
    r, fc = make(cfg, store, settings)
    fc.result = cls()
    r.route(incoming("Mia, are we free Saturday?"))
    fc.result = cls(route=False, conf=0.85, intent="not_for_mia")
    assert not r.route(incoming("what time are you home?", handle=SAM)).respond
    fc.result = cls(route=False, conf=0.6, intent="not_for_mia")
    assert r.route(incoming("hmm ok")).respond  # below 0.8 does not suppress
    fc.result = cls(route=False, conf=0.85, intent="chitchat")
    assert not r.route(incoming("Pretty cool right?")).respond  # confident "don't reply", any intent
    fc.result = cls(route=True, conf=0.85, intent="chitchat")
    assert r.route(incoming("haha Mia that's great")).respond


def test_classifier_verdict_logged(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.9, intent="not_for_mia"))
    m = incoming("what time are you home?")
    r.route(m)
    row = store.decisions_for(m.guid)[0]
    assert row["cls_route"] == 0 and row["cls_reason"] == "test"


def test_couple_talk_stays_quiet(cfg, store, settings):  # acceptance 3
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.9, intent="not_for_mia"))
    assert not r.route(incoming("what time are you home?")).respond


def test_classifier_threshold(cfg, store, settings):
    r, fc = make(cfg, store, settings, cls(route=True, conf=0.74))
    assert not r.route(incoming("can someone check the weather")).respond
    fc.result = cls(route=True, conf=0.75)
    assert r.route(incoming("can someone check the weather?")).respond


def test_classifier_failure_stays_quiet(cfg, store, settings):
    r, _ = make(cfg, store, settings, None)
    assert not r.route(incoming("book us a table")).respond


def test_inline_reply_to_mia_is_a_cue(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.1))
    r.route(incoming("Booked!", from_me=True, guid="mia-9"))
    store.close_window(CHAT)
    d = r.route(incoming("perfect, what time again", reply_to="mia-9"))
    assert d.respond and d.reason == "inline reply to Mia"


def test_closing_phrase_ends_window(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls())
    r.route(incoming("Mia, are we free Saturday?"))
    assert not r.route(incoming("thanks Mia!")).respond
    assert not store.window_active(CHAT)


def test_quiet_mode(cfg, store, settings):  # acceptance 8
    r, _ = make(cfg, store, settings, cls())
    d = r.route(incoming("Mia, sit this one out"))
    assert d.respond and d.ack == SIT_OUT_ACK
    assert not r.route(incoming("Mia, are we free Saturday?")).respond
    d = r.route(incoming("Mia, you're back"))
    assert d.respond and d.ack == BACK_ACK
    assert r.route(incoming("Mia, are we free Saturday?")).respond


def test_duplicate_and_stale(cfg, store, settings):  # acceptance 10
    r, _ = make(cfg, store, settings, cls())
    m = incoming("Mia, hi", guid="dup")
    assert r.route(m).respond
    assert not r.route(m).respond
    assert not r.route(incoming("Mia, hi", ts=time.time() - 3600)).respond


def test_urgent_flag(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls())
    assert r.route(incoming("Mia, urgent: what's the pharmacy number?")).urgent


def test_every_decision_logged_with_reason(cfg, store, settings):
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.2))
    m = incoming("random chatter")
    r.route(m)
    rows = store.decisions_for(m.guid)
    assert len(rows) == 1 and rows[0]["route"] == "quiet" and rows[0]["reason"]
