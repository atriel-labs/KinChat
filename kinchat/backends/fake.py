"""In-memory chat transport for tests and for trying the bridge without a chat network.

    fake = FakeTransport()
    fake.add_group("family", ["+18005550100", "+18005550101"])
    fake.deliver("family", "+18005550100", "Mia, are we free Saturday?")
    ... bridge.handle(inc) for inc in fake.poll() ...
    fake.sent  # [("fake:family", "...")]
"""
from __future__ import annotations

import itertools
import time

from ..health import Health
from .base import Incoming, make_id, split_id

NAME = "fake"


class FakeTransport:
    name = NAME

    def __init__(self, name: str = NAME, clock=time.time):
        self.name = name
        self._clock = clock
        self._seq = itertools.count(1)
        self._inbox: list[Incoming] = []
        self._acked = 0
        self._groups: dict[str, list[str]] = {}
        self.sent: list[tuple[str, str]] = []
        self.fail_sends = False
        self.health = Health(f"{name}-transport", f"{name} transport")
        self.health.ok()

    # --- test helpers -------------------------------------------------------

    def chat_id(self, native: str) -> str:
        return make_id(self.name, native)

    def add_group(self, native: str, members: list[str]) -> str:
        self._groups[native] = list(members)
        return self.chat_id(native)

    def deliver(self, native_chat: str, handle: str | None, text: str, from_me: bool = False,
                reply_to: str | None = None, tapback: bool = False, name: str | None = None) -> Incoming:
        n = next(self._seq)
        inc = Incoming(rowid=n, guid=f"{self.name}-msg-{n}", chat_guid=self.chat_id(native_chat),
                       is_group=native_chat in self._groups, chat_name=name, handle=handle, text=text,
                       is_from_me=from_me, ts=self._clock(), thread_originator_guid=reply_to,
                       is_tapback=tapback, transport=self.name)
        self._inbox.append(inc)
        return inc

    # --- ChatTransport --------------------------------------------------------

    def poll(self) -> list[Incoming]:
        return [m for m in self._inbox if m.rowid > self._acked]

    def ack(self, inc: Incoming) -> None:
        self._acked = max(self._acked, inc.rowid)

    def send(self, chat_id: str, text: str) -> None:
        split_id(chat_id)
        if self.fail_sends:
            raise RuntimeError("fake send failure")
        self.sent.append((chat_id, text))
        # Like Messages, our own post comes back through the transport as is_from_me.
        native = split_id(chat_id)[1]
        self.deliver(native, None, text, from_me=True)

    def participants(self, chat_id: str) -> list[str]:
        return list(self._groups.get(split_id(chat_id)[1], []))

    def is_group(self, chat_id: str) -> bool:
        return split_id(chat_id)[1] in self._groups
