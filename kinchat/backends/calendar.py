"""CalendarSource interface and the snapshot text format every source produces.

The snapshot goes into the reply prompt, so event text is untrusted: titles and locations are flattened to
one line, stripped of angle brackets and capped (see clean), and only events the calendar's owner made or
accepted are included (anyone can send an invite).
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..health import Health

MAX_EVENT_FIELD = 80
NOTHING = "  (nothing scheduled)"


def clean(text: str | None) -> str:
    text = " ".join((text or "").replace("<", "").replace(">", "").split())
    return text if len(text) <= MAX_EVENT_FIELD else text[: MAX_EVENT_FIELD - 1].rstrip() + "…"


def format_event(start: datetime, end: datetime, all_day: bool, subject: str | None, location: str | None,
                 busy: str = "busy") -> str:
    """One snapshot line; start and end are already in the display time zone."""
    if all_day:
        when = start.strftime("%a %b %d") + " (all day)"
    else:
        when = f"{start.strftime('%a %b %d %H:%M')}-{end.strftime('%H:%M')}"
    loc = clean(location)
    return f"  {when}: {clean(subject) or '(no title)'}" + (f" @ {loc}" if loc else "") + f" [{busy}]"


def section(title: str, lines: list[str]) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else NOTHING)


class CalendarSource(Protocol):
    name: str
    health: Health

    def snapshot(self) -> str:
        """The next days of events as prompt text. May raise; the bridge keeps the last good snapshot."""


class NoCalendar:
    """calendar.source: none."""

    name = "none"

    def __init__(self):
        self.health = Health("calendar", "Calendar (none)")
        self.health.ok()

    def snapshot(self) -> str:
        return ""
