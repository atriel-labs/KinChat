"""Task handoff to Mia in Duet ([MIA-CHAT]) and result acceptance ([MIA-POST])."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config
from .llm import TaskRequest, format_transcript
from .store import Store, Task

log = logging.getLogger(__name__)

POST_SUBJECT_RE = re.compile(r"^\s*(?:(?:re|aw|fw|fwd)\s*:\s*)*\[MIA-POST\]\s*(T-\d{4,})\b", re.IGNORECASE)
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


def accept_result(msg: dict, mailbox: str, store: Store) -> ParsedResult | None:
    """Apply the bridge acceptance rules to a fetched Graph message. Returns None (and logs) on rejection."""
    mid = msg.get("id", "?")
    subject = msg.get("subject") or ""
    m = POST_SUBJECT_RE.match(subject)
    if not m:
        return None  # not a result email; [MIA-CHAT] copies and other mail land here too
    task_id = m.group(1).upper()

    def reject(why: str) -> None:
        log.warning("rejected [MIA-POST] %s for %s: %s", mid, task_id, why)

    addr = lambda f: ((msg.get(f) or {}).get("emailAddress") or {}).get("address", "").lower()
    if addr("from") != mailbox.lower() or addr("sender") != mailbox.lower():
        return reject(f"sender {addr('from') or '?'} is not {mailbox}")
    headers = {h["name"].lower(): h.get("value", "") for h in msg.get("internetMessageHeaders") or []}
    if headers.get("x-ms-exchange-organization-authas", "").lower() != "internal":
        return reject("not authenticated as internal to the tenant")
    if not store.task_is_open(task_id):
        return reject("task unknown or not open")
    if store.already_posted(mid):
        return reject("already posted")
    text, status = parse_result_body((msg.get("uniqueBody") or {}).get("content", ""))
    if not text:
        return reject("empty body")
    return ParsedResult(task_id, text, status)


class TaskDesk:
    def __init__(self, cfg: Config, store: Store, send_mail: Callable[[str, str], str | None] | None):
        self.cfg = cfg
        self.store = store
        self._send_mail = send_mail

    def hand_off(self, req: TaskRequest, chat: str, requested_by: str) -> Task | None:
        """Create or update a task and email it to Mia. In shadow mode, log only."""
        if self.cfg.shadow_mode or self._send_mail is None:
            log.info("[shadow] would hand off task from %s: %s", requested_by, req)
            return None
        req.requested_by = requested_by  # from the approved contact, not the model
        existing = self.store.get_task(req.update_of_task_id.upper()) if req.update_of_task_id else None
        if existing and existing.chat_guid != chat:
            existing = None  # never let one chat amend another chat's task
        if existing:
            task_id = existing.task_id
            self.store.update_task(task_id, summary=req.summary, details=req.details, status="open")
        else:
            task_id = self.store.next_task_id()
            self.store.create_task(task_id, chat, req.summary, req.details, requested_by)

        context = format_transcript(self.store.recent_messages(chat, 10), self.cfg.tz)
        payload = {
            "task_id": task_id,
            "summary": req.summary,
            "details": req.details,
            "requested_by": req.requested_by,
            "deadline": req.deadline,
            "update_of_task_id": task_id if existing else None,
            "chat_context": context.splitlines(),
        }
        heading = f"Update to {task_id}" if existing else f"New request {task_id} from the family group"
        body = (
            f"{heading}\n\n{req.summary}\n\n{req.details}\n\n"
            + f"Requested by: {req.requested_by}" + (f"\nDeadline: {req.deadline}" if req.deadline else "")
            + f"\n\nRecent chat:\n{context}\n\n```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n"
        )
        try:
            msg_id = self._send_mail(f"[MIA-CHAT] {task_id}: {req.summary}", body)
        except Exception:
            log.exception("failed to send task email for %s", task_id)
            return self.store.get_task(task_id)
        self.store.update_task(task_id, email_msg_id=msg_id)
        log.info("handed off %s (%s)", task_id, "update" if existing else "new")
        return self.store.get_task(task_id)


class ResultPoller:
    def __init__(self, cfg: Config, store: Store, graph, post: Callable[[str, str], None]):
        self.cfg = cfg
        self.store = store
        self.graph = graph
        self.post = post
        self.last_ok: float | None = None

    def poll_once(self) -> int:
        delta = self.store.kv_get("inbox_delta_link")
        since = datetime.now(UTC) - timedelta(days=1)
        items, next_link = self.graph.inbox_delta(delta, since)
        posted = 0
        for item in items:
            if not POST_SUBJECT_RE.match(item.get("subject") or ""):
                continue
            result = accept_result(self.graph.get_message_for_post(item["id"]), self.graph.mailbox, self.store)
            if result is None:
                continue
            posted += self._post(item["id"], result)
        self.store.kv_set("inbox_delta_link", next_link)
        self.last_ok = time.time()
        return posted

    def _post(self, graph_id: str, r: ParsedResult) -> int:
        if self.cfg.shadow_mode:
            log.info("[shadow] would post result for %s (%s): %s", r.task_id, r.status, r.text)
            self.store.mark_posted(graph_id, r.task_id)
            return 0
        self.store.mark_posted(graph_id, r.task_id)  # mark first: a duplicate post is worse than a missed one
        self.post(self.store.get_task(r.task_id).chat_guid, r.text)
        if r.status in ("done", "failed"):
            self.store.update_task(r.task_id, status=r.status)
        elif r.status == "needs_input":
            self.store.update_task(r.task_id, status="needs_input")
        else:
            self.store.update_task(r.task_id)  # no STATUS line: keep open, touch updated_ts
        log.info("posted result for %s (%s)", r.task_id, r.status or "no status")
        return 1
