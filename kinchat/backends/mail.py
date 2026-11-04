"""The MailTransport interface the mailbox task backend sends and receives through."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ResultMail:
    """A [<TAG>-POST] message that passed the transport's own authenticity rules."""
    key: str      # unique and stable for this message; used to post it only once
    task_id: str
    body: str     # plain text, reply-only where the transport can tell


class MailTransport(Protocol):
    name: str
    address: str  # the assistant's mailbox; task mail goes to and comes from here

    def send(self, subject: str, body: str) -> str | None:
        """Send a message from the assistant's mailbox to itself. Returns a message ID if known. Raises on failure."""

    def fetch_results(self, subject_re: re.Pattern, tag: str) -> list[ResultMail]:
        """New messages whose subject matches `subject_re` (group 1 = task ID) and that are genuine results."""

    def commit(self) -> None:
        """The results from the last fetch_results() were handled; don't return them again."""
