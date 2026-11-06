"""Chat transport interface and the registry that routes namespaced chat IDs to transports.

Every chat ID the bridge stores or passes around is namespaced as "<transport>:<native id>", for example
"imessage:iMessage;+;chat123". The registry picks the transport from that prefix.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..health import Health


@dataclass
class Incoming:
    rowid: int  # the transport's own position marker for this message (iMessage: chat.db ROWID)
    guid: str
    chat_guid: str  # namespaced: "<transport>:<native id>"
    is_group: bool
    chat_name: str | None
    handle: str | None
    text: str
    is_from_me: bool
    ts: float
    thread_originator_guid: str | None
    is_tapback: bool
    transport: str = "imessage"


def make_id(transport: str, native: str) -> str:
    return f"{transport}:{native}"


def split_id(chat_id: str) -> tuple[str, str]:
    """"imessage:iMessage;+;x" -> ("imessage", "iMessage;+;x"). Raises ValueError on an un-namespaced ID."""
    prefix, sep, native = chat_id.partition(":")
    if not sep or not prefix or not native:
        raise ValueError(f"chat ID {chat_id!r} has no transport prefix")
    return prefix, native


@runtime_checkable
class ChatTransport(Protocol):
    """A chat network the bridge can read and post to. See docs/writing-a-transport.md."""

    name: str  # the chat ID prefix
    health: Health

    def poll(self) -> list[Incoming]:
        """New messages since the last acknowledged one, oldest first. May raise; the bridge logs and retries."""

    def ack(self, inc: Incoming) -> None:
        """The bridge has handled `inc`; move the bookmark past it so a restart doesn't return it again."""

    def send(self, chat_id: str, text: str) -> None:
        """Post `text` to the namespaced `chat_id`. Raises on failure."""

    def participants(self, chat_id: str) -> list[str]:
        """Handles of the chat's other members."""

    def is_group(self, chat_id: str) -> bool: ...


class TransportRegistry:
    def __init__(self, transports: list[ChatTransport] | None = None):
        self._by_name: dict[str, ChatTransport] = {}
        for t in transports or []:
            self.add(t)

    def add(self, transport: ChatTransport) -> None:
        if transport.name in self._by_name:
            raise ValueError(f"transport {transport.name!r} registered twice")
        self._by_name[transport.name] = transport

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def get(self, name: str) -> ChatTransport | None:
        return self._by_name.get(name)

    def for_id(self, chat_id: str) -> ChatTransport:
        prefix, _ = split_id(chat_id)
        t = self._by_name.get(prefix)
        if t is None:
            raise KeyError(f"no transport {prefix!r} for chat {chat_id!r}")
        return t

    def send(self, chat_id: str, text: str) -> None:
        self.for_id(chat_id).send(chat_id, text)

    def participants(self, chat_id: str) -> list[str]:
        return self.for_id(chat_id).participants(chat_id)

    def is_group(self, chat_id: str) -> bool:
        """False for an ID no registered transport claims, so nothing is ever posted there as a group."""
        try:
            return self.for_id(chat_id).is_group(chat_id)
        except (KeyError, ValueError):
            return False
