"""Mailbox task backend: tasks go to an agent as [<TAG>-CHAT] email; results come back as [<TAG>-POST] email.

The agent and the bridge share one mailbox (the assistant's). Mail moves through a MailTransport:
mail_graph (Microsoft 365) or mail_imap (IMAP + SMTP).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from ..config import Config
from ..health import Health
from ..llm import TaskRequest, format_transcript
from ..store import Store, Task
from ..tasks import ParsedResult, check_result, post_subject_re
from .mail import MailTransport

log = logging.getLogger(__name__)


class MailboxBackend:
    name = "mailbox"
    accepts_tasks = True

    def __init__(self, cfg: Config, store: Store, mail: MailTransport | None, post: Callable[[str, str], object],
                 health: Health | None = None):
        self.cfg = cfg
        self.store = store
        self.mail = mail
        self.post = post
        self.tag = cfg.assistant.tag
        self._subject_re = post_subject_re(self.tag)
        self.health = health or Health("mail", f"Task mail ({mail.name if mail else 'not configured'})")
        self.last_ok: float | None = None

    # --- hand-off -----------------------------------------------------------

    def hand_off(self, req: TaskRequest, chat: str, requested_by: str) -> Task | None:
        """Create or update a task and email it to the agent. In shadow mode, log only."""
        if self.cfg.shadow_mode or self.mail is None:
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
            self.store.create_task(task_id, chat, req.summary, req.details, requested_by, backend=self.name)

        context = format_transcript(self.store.recent_messages(chat, 10), self.cfg.tz, self.cfg.assistant.name)
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
            msg_id = self.send(f"[{self.tag}-CHAT] {task_id}: {req.summary}", body)
        except Exception:
            log.exception("failed to send task email for %s", task_id)
            return self.store.get_task(task_id)
        self.store.update_task(task_id, email_msg_id=msg_id)
        log.info("handed off %s (%s)", task_id, "update" if existing else "new")
        return self.store.get_task(task_id)

    def send(self, subject: str, body: str) -> str | None:
        """Send mail to the assistant's own mailbox (task mail and the heartbeat), recording health."""
        try:
            msg_id = self.mail.send(subject, body)
        except Exception as e:
            self.health.degraded(e)
            raise
        self.health.ok()
        return msg_id

    # --- results ------------------------------------------------------------

    def poll(self) -> int:
        if self.mail is None:
            return 0
        posted = 0
        for r in self.mail.fetch_results(self._subject_re, self.tag):
            result = check_result(r.task_id, r.key, r.body, self.store, self.tag)
            if result is not None:
                posted += self._post(r.key, result)
        self.mail.commit()
        self.last_ok = time.time()
        return posted

    def _post(self, key: str, r: ParsedResult) -> int:
        if self.cfg.shadow_mode:
            log.info("[shadow] would post result for %s (%s): %s", r.task_id, r.status, r.text)
            self.store.mark_posted(key, r.task_id)
            return 0
        self.store.mark_posted(key, r.task_id)  # mark first: a duplicate post is worse than a missed one
        self.post(self.store.get_task(r.task_id).chat_guid, r.text)
        if r.status in ("done", "failed"):
            self.store.update_task(r.task_id, status=r.status)
            self.close(self.store.get_task(r.task_id))
        elif r.status == "needs_input":
            self.store.update_task(r.task_id, status="needs_input")
        else:
            self.store.update_task(r.task_id)  # no STATUS line: keep open, touch updated_ts
        log.info("posted result for %s (%s)", r.task_id, r.status or "no status")
        return 1

    def close(self, task: Task) -> None:
        pass  # nothing is held per task: the agent's mailbox thread simply ends
