"""Per-subsystem health: each part of the bridge starts on its own and reports here instead of crashing startup."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Literal

log = logging.getLogger(__name__)

State = Literal["ok", "degraded", "failed"]


@dataclass(frozen=True)
class Status:
    name: str
    label: str
    state: State
    last_error: str | None = None
    last_ok: float | None = None
    since: float = 0.0  # when the state last changed


class Health:
    """Health record for one subsystem.

    failed: it could not start (missing secret, missing config, no permission), so it is switched off.
    degraded: it started, but its most recent operation failed; it keeps retrying.
    """

    def __init__(self, name: str, label: str, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._s = Status(name, label, "failed", "not started yet", None, clock())

    @property
    def status(self) -> Status:
        with self._lock:
            return self._s

    @property
    def state(self) -> State:
        return self.status.state

    def ok(self) -> None:
        now = self._clock()
        with self._lock:
            changed = self._s.state != "ok"
            self._s = replace(self._s, state="ok", last_ok=now, since=now if changed else self._s.since)
        if changed:
            log.info("%s: ok", self._s.label)

    def degraded(self, error: BaseException | str) -> None:
        self._set("degraded", error)

    def failed(self, error: BaseException | str) -> None:
        self._set("failed", error)

    def _set(self, state: State, error: BaseException | str) -> None:
        msg = error if isinstance(error, str) else (f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
        msg = " ".join(msg.split())[:300]
        now = self._clock()
        with self._lock:
            changed = (self._s.state, self._s.last_error) != (state, msg)
            self._s = replace(self._s, state=state, last_error=msg,
                              since=now if self._s.state != state else self._s.since)
        if changed:
            (log.error if state == "failed" else log.warning)("%s %s: %s", self._s.label, state, msg)


class HealthBoard:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._items: dict[str, Health] = {}

    def add(self, name: str, label: str) -> Health:
        h = self._items[name] = Health(name, label, self._clock)
        return h

    def __getitem__(self, name: str) -> Health:
        return self._items[name]

    def snapshot(self) -> list[Status]:
        return [h.status for h in self._items.values()]
