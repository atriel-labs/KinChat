"""`kinchat doctor` (alias `kinchat check`): check the setup end to end and say how to fix what's wrong.

Each step reports ok, warn or FAIL with a one-line fix. Every outside dependency (Keychain, chat.db,
Messages, the Claude API, Graph, IMAP/SMTP, calendar feeds, launchctl) comes in through Deps, so tests
can swap in fakes. Secrets and calendar feed URLs are never printed.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import launchd, paths
from . import secrets as secrets_mod
from .config import Config


@dataclass
class Result:
    status: str  # ok, warn, FAIL
    name: str
    detail: str = ""
    hint: str = ""


@dataclass
class Deps:
    home: Path = field(default_factory=Path.home)
    secret: Callable[[str], str] = secrets_mod.get
    run: Callable = subprocess.run  # launchctl
    chatdb: Callable[[str], object] | None = None
    scriptable: Callable[[], int] | None = None
    anthropic: Callable[[str], object] | None = None
    graph: Callable[[Config, str], object] | None = None
    imap: Callable[[object], object] | None = None
    smtp: Callable[[object], object] | None = None
    fetch: Callable[[str], str] | None = None
    current_python: Callable[[], str] = launchd.real_python


class Doctor:
    def __init__(self, cfg: Config, deps: Deps | None = None, probe_mailbox: str | None = None):
        self.cfg = cfg
        self.d = deps or Deps()
        self.probe_mailbox = probe_mailbox
        self.results: list[Result] = []

    def add(self, status: str, name: str, detail: str = "", hint: str = "") -> None:
        self.results.append(Result(status, name, detail, hint))

    def step(self, name: str, fn: Callable[[], str | None], hint: str = "", failure: str = "FAIL") -> bool:
        try:
            detail = fn()
        except Exception as e:  # each check reports its own failure; doctor never stops early
            self.add(failure, name, _describe(e), hint)
            return False
        self.add("ok", name, detail or "")
        return True

    @property
    def failed(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)

    # --- the checks ----------------------------------------------------------------

    def run(self) -> list[Result]:
        cfg = self.cfg
        self.step("family.md", lambda: f"{len(cfg.family_md.read_text())} chars",
                  f"create {cfg.family_md} (copy family.example.md)", failure="warn")
        if "imessage" in cfg.chat.transports:
            self.check_imessage()
        self.check_secrets()
        self.check_claude()
        uses_graph_mail = cfg.tasks.backend == "mailbox" and cfg.tasks.mail == "graph"
        if uses_graph_mail or cfg.calendar.source == "graph":
            self.check_graph(mail=uses_graph_mail, calendar=cfg.calendar.source == "graph")
        if cfg.tasks.backend == "mailbox" and cfg.tasks.mail == "imap":
            self.check_imap()
        if cfg.calendar.source == "ics":
            self.check_ics()
        if cfg.tasks.backend == "managed_agents":
            self.check_managed_agents()
        self.check_launchd()
        return self.results

    def check_imessage(self) -> None:
        from .backends.imessage import ChatDB, messages_scriptable
        chatdb = self.d.chatdb or ChatDB
        self.step("chat.db readable (Full Disk Access)",
                  lambda: f"{len(chatdb(self.cfg.messages.chat_db).list_chats())} chats",
                  f"grant Full Disk Access to {self.d.current_python()} in System Settings > Privacy & Security")
        self.step("Messages scriptable (Automation)", lambda: f"{(self.d.scriptable or messages_scriptable)()} chats",
                  "run `kinchat doctor` from Terminal once and allow it to control Messages")

    def _secret(self, account: str, hint: str, failure: str = "FAIL") -> bool:
        def get():
            if not self.d.secret(account):
                raise ValueError("empty")
        return self.step(f"keychain: {account}", get, hint, failure)

    def check_secrets(self) -> None:
        add = "security add-generic-password -U -s kinchat -a {} -w '...'  (or run `kinchat setup`)"
        self._secret("anthropic-api-key", add.format("anthropic-api-key"))
        self._secret("dashboard-password", add.format("dashboard-password") + "; without it nobody can sign in",
                     failure="warn")

    def _anthropic(self):
        if self.d.anthropic:
            return self.d.anthropic(self.d.secret("anthropic-api-key"))
        import anthropic
        return anthropic.Anthropic(api_key=self.d.secret("anthropic-api-key"))

    def check_claude(self) -> None:
        r = self.cfg.replier
        self.step("Claude models", lambda: ", ".join(self._anthropic().models.retrieve(m).id
                                                      for m in (r.model_router, r.model_reply)),
                  "check the API key and replier.model_router / model_reply in config.yaml")

    def check_graph(self, mail: bool, calendar: bool) -> None:
        g = self.cfg.graph
        missing = [k for k in ("tenant", "client_id", "cert_thumbprint", "mailbox") if not getattr(g, k)]
        if missing:
            self.add("FAIL", "graph config", f"missing {', '.join(missing)}", "fill in the graph: section of config.yaml")
            return
        if not self._secret("graph-cert-key-b64", "store the certificate key: see README step 5"):
            return
        try:
            if self.d.graph:
                client = self.d.graph(self.cfg, secrets_mod.graph_cert_private_key())
            else:
                from .graph import Graph
                client = Graph(g.tenant, g.client_id, g.cert_thumbprint, secrets_mod.graph_cert_private_key(),
                               g.mailbox, self.cfg.timezone)
        except Exception as e:
            self.add("FAIL", "graph client", _describe(e), "pip install 'kinchat[graph]' and check the certificate key")
            return
        if not self.step("graph token", lambda: client._headers() and None,
                         "check tenant, client_id, the certificate thumbprint and that the app has admin consent"):
            return
        if mail:
            self.step("graph inbox read", lambda: client._get(f"/users/{g.mailbox}/mailFolders/inbox",
                                                              {"$select": "totalItemCount"}) and None,
                      f"grant Mail.ReadWrite for {g.mailbox} (Exchange RBAC for Applications)")
        if calendar:
            self.step("graph calendars", lambda: f"{len(client.calendar_snapshot(g.calendars, 1).splitlines())} lines",
                      "grant Calendars.Read for each address in graph.calendars")
        self.check_graph_scope(client)

    def check_graph_scope(self, client) -> None:
        """The app must NOT reach other mailboxes: RBAC should scope it to the assistant's own."""
        name = "graph scoped to the assistant's mailbox"
        if not self.probe_mailbox:
            self.add("warn", name, "not checked", "re-run with --probe-mailbox <another address in the tenant>")
            return
        try:
            client._get(f"/users/{self.probe_mailbox}/mailFolders/inbox", {"$select": "totalItemCount"})
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (403, 404):
                self.add("ok", name, f"{self.probe_mailbox} is denied ({status})")
            else:
                self.add("warn", name, f"probe failed: {_describe(e)}", "check the address and the network")
            return
        self.add("FAIL", name, f"{self.probe_mailbox}'s inbox is readable",
                 "scope the app with Exchange RBAC for Applications (New-ManagementScope + New-ManagementRoleAssignment"
                 " for the assistant's mailbox only), then check with Test-ServicePrincipalAuthorization")

    def check_imap(self) -> None:
        from .backends.mail_imap import ImapMail, _quote_mailbox
        if not self._secret("imap-app-password", "store the app password: kinchat setup, or security ... -a imap-app-password"):
            return
        c = self.cfg.imap
        mail = ImapMail(c, secrets_mod.imap_app_password(), store=None)
        imap_factory = (lambda: self.d.imap(c)) if self.d.imap else mail._imap_factory
        smtp_factory = (lambda: self.d.smtp(c)) if self.d.smtp else mail._smtp_factory

        def imap_check():
            imap = imap_factory()
            try:
                imap.login(c.username, secrets_mod.imap_app_password())
                typ, _ = imap.select(_quote_mailbox(c.sent_folder), readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"the Sent folder {c.sent_folder!r} can't be selected")
                return f"logged in; {c.sent_folder} selectable"
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

        def smtp_check():
            smtp = smtp_factory()
            try:
                smtp.login(c.username, secrets_mod.imap_app_password())
                return "logged in (nothing sent)"
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass

        self.step("IMAP login + Sent folder", imap_check,
                  'check imap.host/username, the app password and imap.sent_folder (Gmail: "[Gmail]/Sent Mail")')
        self.step("SMTP login", smtp_check, "check imap.smtp_host/smtp_port and the app password")

    def check_ics(self) -> None:
        from .backends.cal_ics import IcsCalendar, _expanded_events, expansion_available, parse_calendar
        if not self._secret("ics-urls", "store the feed URLs: kinchat setup, or security ... -a ics-urls"):
            return
        urls = secrets_mod.ics_urls()
        fetch = self.d.fetch or IcsCalendar._http_get
        expand = expansion_available()
        from datetime import datetime, timedelta
        now = datetime.now(self.cfg.tz)
        for i, url in enumerate(urls, 1):
            def check(url=url):
                text = fetch(url)
                if expand:
                    _, events = _expanded_events(text, self.cfg.tz, now, now + timedelta(days=14))
                else:
                    _, events = parse_calendar(text, self.cfg.tz)
                return f"{len(events)} events"
            # The URL is a credential: name the feed by its position only.
            self.step(f"calendar feed {i}", check, "check the feed URL in the Keychain account ics-urls")
        if expand:
            self.add("ok", "recurring events", "expanded (kinchat[ics] installed)")
        else:
            self.add("warn", "recurring events", "not expanded", "pip install 'kinchat[ics]'")

    def check_managed_agents(self) -> None:
        ma = self.cfg.managed_agents
        if not (ma.agent_id and ma.environment_id):
            self.add("FAIL", "managed agent", "not set up", "run `kinchat agent setup`")
            return
        try:
            client = self._anthropic()
        except Exception as e:
            self.add("FAIL", "managed agent", _describe(e), "store the Anthropic API key")
            return

        def agent():
            a = client.beta.agents.retrieve(ma.agent_id)
            if ma.agent_version and a.version != ma.agent_version:
                raise RuntimeError(f"agent is at version {a.version}, config says {ma.agent_version}")
            return f"{a.id} v{a.version}"
        self.step("managed agent", agent, "run `kinchat agent setup` to update the agent and the recorded version")
        self.step("managed environment", lambda: client.beta.environments.retrieve(ma.environment_id).id,
                  "run `kinchat agent setup` to recreate the environment")
        for vid in ma.vault_ids:
            self.step(f"vault {vid}", lambda vid=vid: client.beta.vaults.retrieve(vid).id and None,
                      "check managed_agents.vault_ids in config.yaml")

    def check_launchd(self) -> None:
        home, run = self.d.home, self.d.run
        plist = paths.plist(home)
        new_loaded = launchd.is_loaded(paths.LABEL, run)
        old_loaded = launchd.is_loaded(paths.LEGACY_LABEL, run)
        if old_loaded and new_loaded:
            self.add("FAIL", "only one bridge running", f"both {paths.LEGACY_LABEL} and {paths.LABEL} are loaded",
                     f"launchctl bootout {launchd.domain()}/{paths.LEGACY_LABEL}  (or `kinchat migrate --rollback`)")
        elif old_loaded:
            self.add("warn", "launchd agent", f"the old {paths.LEGACY_LABEL} agent is running",
                     "run `kinchat migrate --from mia-bridge` to switch to kinchat")
        if not plist.exists():
            if not old_loaded:
                self.add("warn", "launchd agent", f"{plist.name} not installed", "run `kinchat install-agent`")
            return
        python = launchd.plist_python(plist)
        if not python or not os.path.exists(python):
            self.add("FAIL", "launchd python", f"{python or '?'} does not exist", "run `kinchat install-agent`")
            return
        real = os.path.realpath(python)
        record = paths.fda_record(home)
        granted = record.read_text().strip() if record.exists() else None
        if granted and granted != real:
            self.add("warn", "Full Disk Access binary", f"was {granted}, launchd now runs {real}",
                     f"grant Full Disk Access to {real} (the Python binary changed, e.g. after brew upgrade),"
                     " then `kinchat install-agent` to record it")
        elif not granted:
            self.add("warn", "Full Disk Access binary", f"launchd runs {real}",
                     "make sure that binary has Full Disk Access; `kinchat install-agent` records it")
        else:
            self.add("ok", "Full Disk Access binary", real)
        if not old_loaded or new_loaded:
            self.add("ok" if new_loaded else "warn", "launchd agent", "loaded" if new_loaded else "not loaded",
                     "" if new_loaded else f"launchctl bootstrap {launchd.domain()} {plist}")


def _describe(e: BaseException) -> str:
    text = " ".join(str(e).split())[:200]
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def report(results: list[Result], out=print) -> None:
    for r in results:
        label = {"ok": "ok  ", "warn": "warn", "FAIL": "FAIL"}[r.status]
        out(f"  {label}  {r.name}" + (f": {r.detail}" if r.detail else ""))
        if r.hint and r.status != "ok":
            out(f"        fix: {r.hint}")
