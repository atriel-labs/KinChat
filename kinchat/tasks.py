"""Task backends: the interface, the shared result rules, and the `none` backend.

A task backend takes the work the replier hands off and later posts results back to the chat the task
came from. Backends live in kinchat/backends/: mailbox (email to an agent, [<TAG>-CHAT] out and
[<TAG>-POST] back) and managed_agents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .health import Health
from .llm import TaskRequest
from .store import Store, Task

log = logging.getLogger(__name__)

def post_subject_re(tag: str = "MIA") -> re.Pattern:
    return re.compile(rf"^\s*(?:(?:re|aw|fw|fwd)\s*:\s*)*\[{re.escape(tag)}-POST\]\s*(T-\d{{4,}})\b", re.IGNORECASE)


POST_SUBJECT_RE = post_subject_re()
STATUS_RE = re.compile(r"^\s*STATUS\s*:\s*(done|needs_input|failed)\s*$", re.IGNORECASE | re.MULTILINE)
MAX_RESULT_CHARS = 600


@dataclass
class ParsedResult:
    task_id: str
    text: str
    status: str | None


def parse_result_body(body: str) -> tuple[str, str | None]:
    """Strip notes below a `---` line and pull out the optional STATUS line."""
    lines = body.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            lines = lines[:i]
            break
    text = "\n".join(lines)
    status = None
    m = STATUS_RE.search(text)
    if m:
        status = m.group(1).lower()
        text = STATUS_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_RESULT_CHARS:
        log.warning("result text over %d chars; truncating", MAX_RESULT_CHARS)
        text = text[: MAX_RESULT_CHARS - 1].rstrip() + "…"
    return text, status


def check_result(task_id: str, key: str, body: str, store: Store, tag: str = "MIA") -> ParsedResult | None:
    """The rules every backend applies to a result: the task is open, the result is new, the body isn't empty."""
    def reject(why: str) -> None:
        log.warning("rejected [%s-POST] %s for %s: %s", tag, key, task_id, why)

    if not store.task_is_open(task_id):
        return reject("task unknown or not open")
    if store.already_posted(key):
        return reject("already posted")
    text, status = parse_result_body(body)
    if not text:
        return reject("empty body")
    return ParsedResult(task_id, text, status)


def graph_sender_problem(msg: dict, mailbox: str) -> str | None:
    """Why a Graph message is not a genuine result from the assistant's own mailbox, or None if it is."""
    addr = lambda f: ((msg.get(f) or {}).get("emailAddress") or {}).get("address", "").lower()
    if addr("from") != mailbox.lower() or addr("sender") != mailbox.lower():
        return f"sender {addr('from') or '?'} is not {mailbox}"
    headers = {h["name"].lower(): h.get("value", "") for h in msg.get("internetMessageHeaders") or []}
    if headers.get("x-ms-exchange-organization-authas", "").lower() != "internal":
        return "not authenticated as internal to the tenant"
    return None


def accept_result(msg: dict, mailbox: str, store: Store, tag: str = "MIA") -> ParsedResult | None:
    """Apply the bridge acceptance rules to a fetched Graph message. Returns None (and logs) on rejection."""
    mid = msg.get("id", "?")
    subject = msg.get("subject") or ""
    m = (POST_SUBJECT_RE if tag == "MIA" else post_subject_re(tag)).match(subject)
    if not m:
        return None  # not a result email; [<TAG>-CHAT] copies and other mail land here too
    task_id = m.group(1).upper()
    problem = graph_sender_problem(msg, mailbox)
    if problem:
        log.warning("rejected [%s-POST] %s for %s: %s", tag, mid, task_id, problem)
        return None
    return check_result(task_id, mid, (msg.get("uniqueBody") or {}).get("content", ""), store, tag)


class TaskBackend(Protocol):
    """Where handed-off tasks go. The bridge calls poll() on a timer and the backend posts results itself."""

    name: str
    health: Health

    def hand_off(self, req: TaskRequest, chat: str, requested_by: str) -> Task | None:
        """Create or update a task for `chat` and send it on. Returns the task, or None if nothing was sent."""

    def poll(self) -> int:
        """Fetch results and post them to their chats. Returns how many were posted. May raise."""

    def close(self, task: Task) -> None:
        """The task is finished (done or failed); release anything held for it."""


class NoTaskBackend:
    """task_backend: none. The assistant chats but never takes on tasks."""

    name = "none"
    accepts_tasks = False

    def __init__(self):
        self.health = Health("tasks", "Tasks")
        self.health.ok()

    def hand_off(self, req: TaskRequest, chat: str, requested_by: str) -> Task | None:
        log.info("task_backend is none; not handing off %r from %s", req.summary, requested_by)
        return None

    def poll(self) -> int:
        return 0

    def close(self, task: Task) -> None:
        pass
