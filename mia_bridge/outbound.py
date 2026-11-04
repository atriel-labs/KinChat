"""Everything Mia posts goes through here: dashboard checks, quiet hours, rate limit, shadow mode."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

RATE_WINDOW_S = 600


class Outbound:
    def __init__(self, store: Store, settings: Settings, send_fn: Callable[[str, str], None],
                 max_per_window: int, window_minutes: float, shadow: bool, tz: ZoneInfo,
                 quiet_start: int = 22, quiet_end: int = 7, clock: Callable[[], float] = time.time):
        self._store = store
        self._settings = settings
        self._send_fn = send_fn
        self._max = max_per_window
        self._window_minutes = window_minutes
        self._shadow = shadow
        self._tz = tz
        self._quiet = (quiet_start, quiet_end)
        self._clock = clock
        self._sent: deque[float] = deque()
        self._pending: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def in_quiet_hours(self) -> bool:
        if not self._settings.feature("quiet_hours"):
            return False
        hour = datetime.fromtimestamp(self._clock(), self._tz).hour
        start, end = self._quiet
        return hour >= start or hour < end if start > end else start <= hour < end

    def post(self, chat_guid: str, text: str, urgent: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        if not self._settings.can_speak_in(chat_guid):
            log.info("not posting to %s: dashboard does not allow it", chat_guid)
            return
        if self.in_quiet_hours() and not urgent:
            self._store.hold(chat_guid, text)
            log.info("quiet hours: held message for %s", chat_guid)
            return
        with self._lock:
            self._expire()
            if self._pending.get(chat_guid) or len(self._sent) >= self._max:
                self._pending.setdefault(chat_guid, []).append(text)
                log.info("rate limit reached; queued message for %s", chat_guid)
                return
            self._sent.append(self._clock())
        self._deliver(chat_guid, text)

    def tick(self) -> None:
        """Release held messages after quiet hours and flush rate-limited queues. Called every few seconds."""
        if not self.in_quiet_hours():
            for chat, texts in self._store.take_held().items():
                if self._settings.can_speak_in(chat):
                    with self._lock:
                        self._pending.setdefault(chat, []).extend(texts)
        while True:
            with self._lock:
                self._expire()
                chat = next((c for c, t in self._pending.items() if t), None)
                if chat is None or len(self._sent) >= self._max:
                    return
                text = "\n\n".join(self._pending.pop(chat))
                self._sent.append(self._clock())
            self._deliver(chat, text)

    def _expire(self) -> None:
        cutoff = self._clock() - RATE_WINDOW_S
        while self._sent and self._sent[0] <= cutoff:
            self._sent.popleft()

    def _deliver(self, chat_guid: str, text: str) -> None:
        if not self._settings.can_speak_in(chat_guid):  # settings may have changed while queued
            log.info("dropped queued message for %s: dashboard no longer allows it", chat_guid)
            return
        if self._shadow:
            log.info("[shadow] would send to %s: %s", chat_guid, text)
            return
        try:
            self._send_fn(chat_guid, text)
        except Exception:
            log.exception("send to %s failed", chat_guid)
            return
        # The sent message comes back through chat.db as is_from_me and is stored then.
        self._store.open_window(chat_guid, self._window_minutes, self._clock())
