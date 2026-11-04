"""Wire the bridge together: chat.db watcher, router/replier, outbound, result poller, calendar, heartbeat, dashboard."""
from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime

from . import secrets
from .config import Config
from .dashboard import create_app
from .graph import Graph
from .llm import LLM
from .messages import ChatDB, Incoming, send_text
from .outbound import Outbound
from .router import Decision, Router
from .settings import Notes, Settings
from .store import Store
from .tasks import ResultPoller, TaskDesk

log = logging.getLogger(__name__)

HANDOFF_ACK = "On it, I'll let you know."


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.settings = Settings(self.store)
        self.notes = Notes(self.store, self.settings, cfg.family_md)
        self.chatdb = ChatDB(cfg.messages.chat_db)
        rp = cfg.replier
        self.llm = LLM(secrets.anthropic_api_key(), rp.model_router, rp.model_reply, rp.router_effort,
                       rp.reply_effort, rp.timeout_s, rp.fallbacks)
        g = cfg.graph
        self.graph = Graph(g.tenant, g.client_id, g.cert_thumbprint, secrets.graph_cert_private_key(),
                           g.mailbox, cfg.timezone)
        self.outbound = Outbound(self.store, self.settings, send_text, rp.max_msgs_per_10min,
                                 cfg.routing.window_minutes, cfg.shadow_mode, cfg.tz,
                                 cfg.quiet_hours.start, cfg.quiet_hours.end)
        self.router = Router(cfg, self.store, self.settings,
                             lambda h, m, t, n: self.llm.classify(h, m, t, n, cfg.tz),
                             self.chatdb.participants)
        self.desk = TaskDesk(cfg, self.store, self.graph.send_to_self)
        self.poller = ResultPoller(cfg, self.store, self.graph, self.outbound.post)
        self.dashboard_password = secrets.dashboard_password()
        self.calendar = ""
        self.last_chatdb_read: float | None = None

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
        try:
            family_md = cfg.family_md.read_text()
        except OSError:
            family_md = "(no family notes)"
        can_hand_off = self.settings.feature("task_handoff") and d.contact.can_assign
        reply = self.llm.reply(
            self.store.recent_messages(chat, cfg.replier.reply_history_messages),
            self.store.relevant_tasks(chat), self.calendar, family_md, d.classification,
            [c.name for c in self.settings.contacts()], can_hand_off, cfg.tz)
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

    def _watch_chatdb(self) -> None:
        last = self.store.kv_get("chatdb_last_rowid")
        if last is None:
            last = str(self.chatdb.max_rowid())  # first run: start from now, not from history
            self.store.kv_set("chatdb_last_rowid", last)
        rowid = int(last)
        while True:
            try:
                for inc in self.chatdb.new_messages(rowid):
                    try:
                        self.handle(inc)
                    except Exception:
                        log.exception("failed handling %s", inc.guid)
                    rowid = inc.rowid
                    self.store.kv_set("chatdb_last_rowid", str(rowid))
                self.last_chatdb_read = time.time()
            except Exception:
                log.exception("reading chat.db failed (Full Disk Access?)")
                self.chatdb.reset()
                time.sleep(10)
            time.sleep(self.cfg.messages.poll_seconds)

    # --- background loops --------------------------------------------------

    def _every(self, seconds: float, fn, name: str) -> None:
        def loop():
            while True:
                try:
                    fn()
                except Exception:
                    log.exception("%s failed", name)
                time.sleep(seconds)
        threading.Thread(target=loop, name=name, daemon=True).start()

    def _refresh_calendar(self) -> None:
        self.calendar = self.graph.calendar_snapshot(self.cfg.graph.calendars, self.cfg.graph.calendar_days)

    def _prune(self) -> None:
        n = self.store.prune_messages(self.cfg.history_retention_days)
        if n:
            log.info("pruned %d messages older than %d days", n, self.cfg.history_retention_days)

    def _heartbeat(self) -> None:
        if self.cfg.shadow_mode or not self.settings.feature("heartbeat"):
            return
        fmt = lambda ts: datetime.fromtimestamp(ts, self.cfg.tz).strftime("%Y-%m-%d %H:%M") if ts else "never"
        body = (
            f"kinchat on {socket.gethostname()} is running.\n\n"
            f"Time: {fmt(time.time())}\n"
            f"Mia enabled: {'yes' if self.settings.feature('mia_enabled') else 'no'}\n"
            f"Last chat.db read: {fmt(self.last_chatdb_read)}\n"
            f"Last inbox poll: {fmt(self.poller.last_ok)}\n"
            f"Open tasks: {self.store.open_task_count()}\n"
        )
        self.graph.send_to_self(f"[MIA-HEARTBEAT] {fmt(time.time())}", body)

    def run(self) -> None:
        log.info("starting kinchat (shadow_mode=%s)", self.cfg.shadow_mode)
        threading.Thread(target=self._watch_chatdb, name="chatdb", daemon=True).start()
        self._every(5, self.outbound.tick, "outbound")
        self._every(self.cfg.graph.calendar_refresh_minutes * 60, self._refresh_calendar, "calendar")
        self._every(self.cfg.graph.poll_seconds, self.poller.poll_once, "inbox-poll")
        self._every(3600, self._heartbeat, "heartbeat")
        self._every(6 * 3600, self._prune, "prune")
        app = create_app(self.settings, self.store, self.dashboard_password, self.cfg.tz, self.cfg.shadow_mode,
                         self.notes)
        app.run(host=self.cfg.dashboard.bind, port=self.cfg.dashboard.port, threaded=True)
