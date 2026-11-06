"""Microsoft Graph as a MailTransport: results are read from the inbox by delta query."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from ..store import Store
from ..tasks import graph_sender_problem
from .mail import ResultMail

log = logging.getLogger(__name__)

DELTA_KEY = "inbox_delta_link"  # kv key, unchanged so a live install keeps its delta position


class GraphMail:
    name = "graph"

    def __init__(self, graph, store: Store):
        self.graph = graph
        self.address = graph.mailbox
        self._store = store
        self._next_link: str | None = None

    def send(self, subject: str, body: str) -> str | None:
        return self.graph.send_to_self(subject, body)

    def fetch_results(self, subject_re: re.Pattern, tag: str) -> list[ResultMail]:
        delta = self._store.kv_get(DELTA_KEY)
        since = datetime.now(UTC) - timedelta(days=1)
        items, self._next_link = self.graph.inbox_delta(delta, since)
        out = []
        for item in items:
            m = subject_re.match(item.get("subject") or "")
            if not m:
                continue
            msg = self.graph.get_message_for_post(item["id"])
            m = subject_re.match(msg.get("subject") or "")
            if not m:
                continue
            problem = graph_sender_problem(msg, self.address)
            if problem:
                log.warning("rejected [%s-POST] %s: %s", tag, item["id"], problem)
                continue
            out.append(ResultMail(item["id"], m.group(1).upper(), (msg.get("uniqueBody") or {}).get("content", "")))
        return out

    def commit(self) -> None:
        if self._next_link:
            self._store.kv_set(DELTA_KEY, self._next_link)
            self._next_link = None
