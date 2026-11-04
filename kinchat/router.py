"""Decide whether the assistant speaks: dashboard rules and hard filters, controls, explicit cues, window, classifier."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .llm import Classification
from .messages import Incoming
from .settings import Contact, Settings, norm_handle
from .store import Message, Store, Task

log = logging.getLogger(__name__)


def _names_alt(names: list[str]) -> str:
    """Regex alternation matching any of the names as a whole word, longest first."""
    names = sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True)
    return "(?:" + "|".join(re.escape(n) for n in names) + ")"


@dataclass(frozen=True)
class Cues:
    """The name-dependent patterns: a mention, the quiet-mode controls and closing phrases."""
    mention: re.Pattern
    sit_out: re.Pattern
    back: re.Pattern
    closing: re.Pattern


def build_cues(names: list[str]) -> Cues:
    n = rf"(?<!\w){_names_alt(names)}(?!\w)"
    return Cues(
        mention=re.compile(n, re.IGNORECASE),
        sit_out=re.compile(rf"{n}.*\bsit (this|this one|it) out\b", re.IGNORECASE),
        back=re.compile(rf"{n}.*\byou'?re back\b|\byou'?re back\b.*{n}", re.IGNORECASE),
        closing=re.compile(
            r"^(ok(ay)?[, ]+)?(thanks|thank you|thx|ty|we'?re good|that'?s all|all set|perfect)"
            rf"([, ]+({_names_alt(names)}|thanks|thank you))*[.! ]*$",
            re.IGNORECASE,
        ),
    )


_DEFAULT_CUES = build_cues(["Mia"])
MIA_RE = _DEFAULT_CUES.mention
SIT_OUT_RE = _DEFAULT_CUES.sit_out
BACK_RE = _DEFAULT_CUES.back
CLOSING_RE = _DEFAULT_CUES.closing
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
        self.name = cfg.assistant.name
        self.cues = build_cues(cfg.assistant.names)
        self.own_handles = {norm_handle(h) for h in cfg.assistant.handles} - {""}

    def route(self, inc: Incoming) -> Decision:
        store, settings, r, chat = self.store, self.settings, self.cfg.routing, inc.chat_guid

        # Chat gate. Unapproved chats are dropped before anything is stored or sent anywhere.
        mode = "respond"
        if inc.is_group:
            members = self.participants(chat)
            c = settings.get_chat(chat) or settings.register_pending(chat, inc.chat_name, members)
            # Someone added to an approved group would see everything the assistant says there: re-approve first.
            c = settings.check_members(c, members, ignore=self.own_handles)
            if c.mode in ("pending", "off"):
                return self._log(inc, Decision(False, "filter", f"chat is {c.mode}"))
            mode = c.mode

        if inc.is_from_me:
            if inc.text and not inc.is_tapback and (inc.is_group or settings.feature("one_on_one")):
                store.add_message(Message(inc.guid, chat, self.name, inc.text, inc.ts, True, inc.thread_originator_guid))
            return Decision(False, "filter", f"from {self.name}")

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
        if self.cues.back.search(inc.text) and store.quiet(chat, self.clock()):
            store.set_quiet(chat, 0)
            return self._log(inc, Decision(True, "control", "quiet mode cleared", msg, contact=contact, ack=BACK_ACK))
        if self.cues.sit_out.search(inc.text):
            store.set_quiet(chat, r.quiet_minutes, self.clock())
            return self._log(inc, Decision(True, "control", f"quiet mode for {r.quiet_minutes} min", msg,
                                           contact=contact, ack=SIT_OUT_ACK))
        if store.quiet(chat, self.clock()):
            return self._log(inc, Decision(False, "filter", "quiet mode"))

        if self.cues.closing.match(inc.text.strip()):
            store.close_window(chat)
            return self._log(inc, Decision(False, "window", "closing phrase ended the window"))

        # 2. Explicit cues.
        cue = None
        if not inc.is_group:
            cue = "direct message"
        elif self.cues.mention.search(inc.text):
            cue = f"mentions {self.name}"
        elif inc.thread_originator_guid and store.is_mia_message(inc.thread_originator_guid):
            cue = f"inline reply to {self.name}"
        if cue is None and not settings.feature("followups"):
            return self._log(inc, Decision(False, "filter", "not named; follow-ups without the name are off"))

        names = [c.name for c in settings.contacts()]
        history = store.recent_messages(chat, r.history_messages + 1)[:-1]
        cls = self.classify(history, msg, store.relevant_tasks(chat), names)

        if cue:
            store.open_window(chat, r.window_minutes, self.clock())
            return self._log(inc, Decision(True, "cue", cue, msg, cls, contact, urgent=urgent))

        # 3. Conversation window. Beyond the spec, a confident "the assistant shouldn't reply" also suppresses,
        # not only a confident not_for_mia intent (e.g. family chitchat right after the assistant spoke).
        if store.window_active(chat, self.clock()):
            confident_no = cls and (cls.intent == "not_for_mia" or not cls.route_to_mia)
            if confident_no and cls.confidence >= r.not_for_mia_threshold:
                if cls.close_window:
                    store.close_window(chat)
                return self._log(inc, Decision(False, "window", f"in window but not for {self.name}: {cls.reason}", msg, cls))
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
