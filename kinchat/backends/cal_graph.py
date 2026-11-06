"""Microsoft Graph calendars as a CalendarSource (the calendarView of each configured user)."""
from __future__ import annotations

from ..health import Health


class GraphCalendar:
    name = "graph"

    def __init__(self, graph, users: list[str], days: int):
        self.graph = graph
        self.users = users
        self.days = days
        self.health = Health("calendar", "Calendar (Graph)")

    def snapshot(self) -> str:
        return self.graph.calendar_snapshot(self.users, self.days)
