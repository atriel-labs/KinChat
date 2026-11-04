"""Wire the bridge together: chat transports, router/replier, outbound, result poller, calendar, heartbeat, dashboard.

Each subsystem starts on its own. A missing secret, missing Graph config or missing macOS permission
switches that part off and shows up in the dashboard's Health section; it never stops the bridge
(or the dashboard) from starting.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime

from . import secrets
from .backends.base import ChatTransport, Incoming, TransportRegistry
from .config import Config
from .dashboard import create_app
from .health import HealthBoard
from .llm import LLM, Classification, Reply
from .outbound import Outbound
from .router import Decision, Router
from .settings import Notes, Settings
from .store import Message, Store, Task
from .tasks import ResultPoller, TaskDesk

log = logging.getLogger(__name__)

HANDOFF_ACK = "On it, I'll let you know."


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.transports = TransportRegistry(self._make_transports())
        self.settings = Settings(self.store, cfg.default_country, self.transports.is_group)
        self.notes = Notes(self.store, self.settings, cfg.family_md)
        self.calendar = ""
        self.last_chatdb_read: float | None = None

        self.health = HealthBoard()
        for t in self.transports:
            self.health.register(t.health)
            if getattr(t, "send_health", None) is not None:
                self.health.register(t.send_health)
        self.h_llm = self.health.add("llm", "Claude API")
        self.h_mail = self.health.add("mail", "Task mail (Graph)")
        self.h_calendar = self.health.add("calendar", "Calendar (Graph)")
        self.h_dashboard = self.health.add("dashboard", "Dashboard password")

        self.llm = self._start_llm()
        self.graph = self._start_graph()
        self.dashboard_password = self._dashboard_password()

        rp = cfg.replier
        # Outbound sends through the registry, which routes each chat ID to its transport by prefix.
        self.outbound = Outbound(self.store, self.settings, self.transports.send, rp.max_msgs_per_10min,
                                 cfg.routing.window_minutes, cfg.shadow_mode, cfg.tz,
                                 cfg.quiet_hours.start, cfg.quiet_hours.end)
        self.router = Router(cfg, self.store, self.settings, self._classify, self.transports.participants)
        self.desk = TaskDesk(cfg, self.store, self._send_mail if self.graph else None)
        self.poller = ResultPoller(cfg, self.store, self.graph, self.outbound.post)

    # --- startup ----------------------------------------------------------

    def _make_transports(self) -> list[ChatTransport]:
        out: list[ChatTransport] = []
        for name in self.cfg.chat.transports:
            if name == "imessage":
                from .backends.imessage import IMessageTransport
                out.append(IMessageTransport(self.cfg.messages.chat_db, self.store))
            elif name == "fake":
                from .backends.fake import FakeTransport
                out.append(FakeTransport())
        return out

    def _start_llm(self) -> LLM | None:
        rp, a = self.cfg.replier, self.cfg.assistant
        try:
            llm = LLM(secrets.anthropic_api_key(), rp.model_router, rp.model_reply, rp.router_effort,
                      rp.reply_effort, rp.timeout_s, rp.fallbacks, a.name, a.owner)
        except Exception as e:
            self.h_llm.failed(e)
            return None
        self.h_llm.degraded("no call made yet")
        return llm

    def _start_graph(self):
        g = self.cfg.graph
        missing = [k for k in ("tenant", "client_id", "cert_thumbprint", "mailbox") if not getattr(g, k)]
        if missing:
            for h in (self.h_mail, self.h_calendar):
                h.failed(f"graph not configured (missing {', '.join(missing)})")
            return None
        try:
            from .graph import Graph  # imports msal; only needed when Graph is configured
            graph = Graph(g.tenant, g.client_id, g.cert_thumbprint, secrets.graph_cert_private_key(),
                          g.mailbox, self.cfg.timezone)
        except Exception as e:
            for h in (self.h_mail, self.h_calendar):
                h.failed(e)
            return None
        self.h_mail.degraded("inbox not polled yet")
        if g.calendars:
            self.h_calendar.degraded("not refreshed yet")
        else:
            self.h_calendar.failed("no calendars configured")
        return graph

    def _dashboard_password(self) -> str | None:
        try:
            pw = secrets.dashboard_password()
        except Exception as e:
            self.h_dashboard.failed(e)
            return None
        if not pw:
            self.h_dashboard.failed("dashboard-password is empty")
            return None
        self.h_dashboard.ok()
        return pw

    def _check_transports(self) -> None:
        for t in self.transports:
            check = getattr(t, "check_scriptable", None)
            if check:
                check()

    # --- subsystem calls that report health -------------------------------

    def _classify(self, history: list[Message], msg: Message, tasks: list[Task],
                  names: list[str]) -> Classification | None:
        if self.llm is None:
            return None
        return self._llm_result(self.llm.classify(history, msg, tasks, names, self.cfg.tz), "classifier")

    def _llm_result(self, result, what: str):
        if result is None:
            self.h_llm.degraded(f"{what} call failed (see log)")
        else:
            self.h_llm.ok()
        return result

    def _send_mail(self, subject: str, body: str) -> str | None:
        try:
            msg_id = self.graph.send_to_self(subject, body)
        except Exception as e:
            self.h_mail.degraded(e)
            raise
        self.h_mail.ok()
        return msg_id

    # --- incoming messages -------------------------------------------------

    def handle(self, inc: Incoming) -> None:
        d = self.router.route(inc)
        if not d.respond:
            return
        if d.ack:
            if self.settings.feature("instant_replies"):
                self.outbound.post(inc.chat_guid, d.ack, urgent=True)
            return
        self._reply(inc, d)

    def _reply(self, inc: Incoming, d: Decision) -> None:
        cfg, chat = self.cfg, inc.chat_guid
        if self.llm is None:
            log.error("no reply for %s: Claude API unavailable (%s)", inc.guid, self.h_llm.status.last_error)
            return
        try:
            family_md = cfg.family_md.read_text()
        except OSError:
            family_md = "(no family notes)"
        can_hand_off = self.settings.feature("task_handoff") and d.contact.can_assign
        reply: Reply | None = self._llm_result(self.llm.reply(
            self.store.recent_messages(chat, cfg.replier.reply_history_messages),
            self.store.relevant_tasks(chat), self.calendar, family_md, d.classification,
            [c.name for c in self.settings.contacts()], can_hand_off, cfg.tz), "reply")
        if reply is None:
            log.error("no reply for %s; staying quiet", inc.guid)
            return
        task = reply.task if can_hand_off else None
        if self.settings.feature("instant_replies"):
            self.outbound.post(chat, reply.message, urgent=d.urgent)
        elif task:
            self.outbound.post(chat, HANDOFF_ACK, urgent=d.urgent)
        log.info("handled %s in %.1fs%s", inc.guid, time.time() - inc.ts, " (shadow)" if cfg.shadow_mode else "")
        if task:
            self.desk.hand_off(task, chat, d.contact.name)
        if reply.notes and self.settings.feature("note_suggestions"):
            added = sum(self.notes.suggest(n, chat, d.contact.name) for n in reply.notes)
            if added:
                log.info("%d note suggestion(s) waiting in the dashboard", added)

    def poll_once(self, transport: ChatTransport) -> int:
        """Handle everything the transport has; returns how many messages were handled."""
        n = 0
        for inc in transport.poll():
            try:
                self.handle(inc)
            except Exception:
                log.exception("failed handling %s", inc.guid)
            transport.ack(inc)
            n += 1
        return n

    def _watch(self, transport: ChatTransport) -> None:
        while True:
            try:
                self.poll_once(transport)
                self.last_chatdb_read = time.time()
            except Exception:
                log.exception("reading from %s failed%s", transport.name,
                              " (Full Disk Access?)" if transport.name == "imessage" else "")
                time.sleep(10)
            time.sleep(self.cfg.messages.poll_seconds)

    # --- background loops --------------------------------------------------

    def _every(self, seconds: float, fn, name: str, health=None) -> None:
        def loop():
            while True:
                try:
                    fn()
                    if health:
                        health.ok()
                except Exception as e:
                    log.exception("%s failed", name)
                    if health:
                        health.degraded(e)
                time.sleep(seconds)
        threading.Thread(target=loop, name=name, daemon=True).start()

    def _refresh_calendar(self) -> None:
        self.calendar = self.graph.calendar_snapshot(self.cfg.graph.calendars, self.cfg.graph.calendar_days)

    def _prune(self) -> None:
        n = self.store.prune_messages(self.cfg.history_retention_days)
        if n:
            log.info("pruned %d messages older than %d days", n, self.cfg.history_retention_days)

    def _heartbeat(self) -> None:
        if self.cfg.shadow_mode or not self.settings.feature("heartbeat") or self.graph is None:
            return
        a = self.cfg.assistant
        fmt = lambda ts: datetime.fromtimestamp(ts, self.cfg.tz).strftime("%Y-%m-%d %H:%M") if ts else "never"
        unhealthy = [f"{s.label}: {s.state} ({s.last_error})" for s in self.health.snapshot() if s.state != "ok"]
        body = (
            f"kinchat on {socket.gethostname()} is running.\n\n"
            f"Time: {fmt(time.time())}\n"
            f"{a.name} enabled: {'yes' if self.settings.feature('mia_enabled') else 'no'}\n"
            f"Last chat.db read: {fmt(self.last_chatdb_read)}\n"
            f"Last inbox poll: {fmt(self.poller.last_ok)}\n"
            f"Open tasks: {self.store.open_task_count()}\n"
            + ("\nNot healthy:\n" + "\n".join(f"- {u}" for u in unhealthy) + "\n" if unhealthy else "")
        )
        self._send_mail(f"[{a.tag}-HEARTBEAT] {fmt(time.time())}", body)

    def create_dashboard(self):
        return create_app(self.settings, self.store, self.dashboard_password, self.cfg.tz, self.cfg.shadow_mode,
                          self.notes, assistant=self.cfg.assistant.name,
                          quiet_hours=(self.cfg.quiet_hours.start, self.cfg.quiet_hours.end),
                          health=self.health.snapshot)

    def start_background(self) -> None:
        g = self.cfg.graph
        threading.Thread(target=self._check_transports, name="transport-check", daemon=True).start()
        for t in self.transports:
            threading.Thread(target=self._watch, args=(t,), name=f"watch-{t.name}", daemon=True).start()
        self._every(5, self.outbound.tick, "outbound")
        if self.graph is not None:
            self._every(g.poll_seconds, self.poller.poll_once, "inbox-poll", self.h_mail)
            if g.calendars:
                self._every(g.calendar_refresh_minutes * 60, self._refresh_calendar, "calendar", self.h_calendar)
            self._every(3600, self._heartbeat, "heartbeat")
        self._every(6 * 3600, self._prune, "prune")

    def run(self) -> None:
        log.info("starting kinchat (shadow_mode=%s)", self.cfg.shadow_mode)
        down = [f"{s.label} ({s.last_error})" for s in self.health.snapshot() if s.state == "failed"]
        if down:
            log.warning("starting without: %s", "; ".join(down))
        self.start_background()
        app = self.create_dashboard()
        app.run(host=self.cfg.dashboard.bind, port=self.cfg.dashboard.port, threaded=True)
