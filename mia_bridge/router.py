"""Decide whether Mia speaks: dashboard rules and hard filters, controls, explicit cues, window, classifier."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .llm import Classification
from .messages import Incoming
from .settings import Contact, Settings, norm_handle
from .store import Message, Store, Task

log = logging.getLogger(__name__)

MIA_RE = re.compile(r"\bmia\b", re.IGNORECASE)
SIT_OUT_RE = re.compile(r"\bmia\b.*\bsit (this|this one|it) out\b", re.IGNORECASE)
BACK_RE = re.compile(r"\bmia\b.*\byou'?re back\b|\byou'?re back\b.*\bmia\b", re.IGNORECASE)
CLOSING_RE = re.compile(
    r"^(ok(ay)?[, ]+)?(thanks|thank you|thx|ty|we'?re good|that'?s all|all set|perfect)"
    r"([, ]+(mia|thanks|thank you))*[.! ]*$",
    re.IGNORECASE,
)
URGENT_RE = re.compile(r"\b(urgent|asap|emergency)\b", re.IGNORECASE)

# Messages older than this when first seen (e.g. arrived while the bridge was down) are stored, never answered.
STALE_AFTER_S = 300

SIT_OUT_ACK = "Okay, I'll sit this one out."
BACK_ACK = "I'm back."


@dataclass
class Decision:
    respond: bool
    step: str
    reason: str
    message: Message | None = None
    classification: Classification | None = None
    contact: Contact | None = None
    ack: str | None = None  # fixed reply that needs no model call
    urgent: bool = False


Classifier = Callable[[list[Message], Message, list[Task], list[str]], Classification | None]


class Router:
    def __init__(self, cfg: Config, store: Store, settings: Settings, classify: Classifier,
                 participants: Callable[[str], list[str]], clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.settings = settings
        self.classify = classify
        self.participants = participants
        self.clock = clock

    def route(self, inc: Incoming) -> Decision:
        store, settings, r, chat = self.store, self.settings, self.cfg.routing, inc.chat_guid

        # Chat gate. Unapproved chats are dropped before anything is stored or sent anywhere.
        mode = "respond"
        if inc.is_group:
            members = self.participants(chat)
            c = settings.get_chat(chat) or settings.register_pending(chat, inc.chat_name, members)
            # Someone added to an approved group would see everything Mia says there: re-approve first.
            c = settings.check_members(c, members, ignore={norm_handle(self.cfg.graph.mailbox)})
            if c.mode in ("pending", "off"):
                return self._log(inc, Decision(False, "filter", f"chat is {c.mode}"))
            mode = c.mode

        if inc.is_from_me:
            if inc.text and not inc.is_tapback and (inc.is_group or settings.feature("one_on_one")):
                store.add_message(Message(inc.guid, chat, "Mia", inc.text, inc.ts, True, inc.thread_originator_guid))
            return Decision(False, "filter", "from Mia")

        contact = settings.contact_for_handle(inc.handle)
        if contact is None:
            return self._log(inc, Decision(False, "filter", "sender is not an approved contact"))
        if not inc.is_group and not (settings.feature("one_on_one") and contact.one_on_one):
            return self._log(inc, Decision(False, "filter", "one-on-one not allowed"))
        if inc.is_tapback or not inc.text:
            return Decision(False, "filter", "empty or tapback")

        msg = Message(inc.guid, chat, contact.name, inc.text, inc.ts, False, inc.thread_originator_guid)
        if not store.add_message(msg):
            return Decision(False, "filter", "duplicate")
        if self.clock() - inc.ts > STALE_AFTER_S:
            return self._log(inc, Decision(False, "filter", "stale message (arrived while down)"))
        if mode == "listen":
            return self._log(inc, Decision(False, "filter", "chat is listen only"))
        if not settings.feature("mia_enabled"):
            return self._log(inc, Decision(False, "filter", "master switch off"))

        urgent = bool(URGENT_RE.search(inc.text))

        # Controls work even in quiet mode.
        if BACK_RE.search(inc.text) and store.quiet(chat, self.clock()):
            store.set_quiet(chat, 0)
            return self._log(inc, Decision(True, "control", "quiet mode cleared", msg, contact=contact, ack=BACK_ACK))
        if SIT_OUT_RE.search(inc.text):
            store.set_quiet(chat, r.quiet_minutes, self.clock())
            return self._log(inc, Decision(True, "control", f"quiet mode for {r.quiet_minutes} min", msg,
                                           contact=contact, ack=SIT_OUT_ACK))
        if store.quiet(chat, self.clock()):
            return self._log(inc, Decision(False, "filter", "quiet mode"))

        if CLOSING_RE.match(inc.text.strip()):
            store.close_window(chat)
            return self._log(inc, Decision(False, "window", "closing phrase ended the window"))

        # 2. Explicit cues.
        cue = None
        if not inc.is_group:
            cue = "direct message"
        elif MIA_RE.search(inc.text):
            cue = "mentions Mia"
        elif inc.thread_originator_guid and store.is_mia_message(inc.thread_originator_guid):
            cue = "inline reply to Mia"
        if cue is None and not settings.feature("followups"):
            return self._log(inc, Decision(False, "filter", "not named; follow-ups without her name are off"))

        names = [c.name for c in settings.contacts()]
        history = store.recent_messages(chat, r.history_messages + 1)[:-1]
        cls = self.classify(history, msg, store.relevant_tasks(chat), names)

        if cue:
            store.open_window(chat, r.window_minutes, self.clock())
            return self._log(inc, Decision(True, "cue", cue, msg, cls, contact, urgent=urgent))

        # 3. Conversation window. Beyond the spec, a confident "Mia shouldn't reply" also suppresses,
        # not only a confident not_for_mia intent (e.g. family chitchat right after Mia spoke).
        if store.window_active(chat, self.clock()):
            confident_no = cls and (cls.intent == "not_for_mia" or not cls.route_to_mia)
            if confident_no and cls.confidence >= r.not_for_mia_threshold:
                if cls.close_window:
                    store.close_window(chat)
                return self._log(inc, Decision(False, "window", f"in window but not for Mia: {cls.reason}", msg, cls))
            return self._log(inc, Decision(True, "window", "active conversation window", msg, cls, contact, urgent=urgent))

        # 4. Classifier.
        if cls is None:
            return self._log(inc, Decision(False, "classifier", "classifier unavailable; staying quiet", msg))
        if cls.route_to_mia and cls.confidence >= r.respond_threshold:
            return self._log(inc, Decision(True, "classifier", cls.reason, msg, cls, contact, urgent=urgent))
        return self._log(inc, Decision(False, "classifier", cls.reason, msg, cls))

    def _log(self, inc: Incoming, d: Decision) -> Decision:
        c = d.classification
        self.store.log_decision(inc.guid, d.step, "respond" if d.respond else "quiet",
                                c.confidence if c else None, c.intent if c else None, d.reason,
                                c.route_to_mia if c else None, c.reason if c else None)
        log.info("decision %s: %s/%s (%s)", inc.guid, d.step, "respond" if d.respond else "quiet", d.reason)
        return d
