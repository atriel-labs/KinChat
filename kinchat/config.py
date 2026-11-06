"""Load config.yaml (infrastructure only; contacts, chats and toggles live in the dashboard)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .settings import CALLING_CODES

_LIBRARY = Path.home() / "Library"

TASK_BACKENDS = ("mailbox", "managed_agents", "none")
MAIL_TRANSPORTS = ("graph", "imap")
CALENDAR_SOURCES = ("graph", "ics", "none")


def _valid_zone(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def system_timezone(localtime: str = "/etc/localtime") -> str:
    """The system's IANA time zone: from the /etc/localtime link (macOS and most Linux), then time.tzname,
    falling back to UTC."""
    try:
        target = os.path.realpath(localtime) if os.path.islink(localtime) else ""
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        name = _valid_zone(target.split("zoneinfo/", 1)[1])
        if name:
            return name
    for name in time.tzname:
        # Only region names: abbreviations like "MST" are fixed-offset legacy zones with no daylight saving.
        if "/" in name and _valid_zone(name):
            return name
    return "UTC"

LEGACY_APP_DIR = _LIBRARY / "Application Support" / "mia-bridge"


def _app_dir() -> Path:
    """The app directory; an install made before the rename keeps using its old one."""
    new = _LIBRARY / "Application Support" / "kinchat"
    return LEGACY_APP_DIR if LEGACY_APP_DIR.is_dir() and not new.exists() else new


APP_DIR = _app_dir()
LOG_DIR = _LIBRARY / "Logs" / "kinchat"


@dataclass
class Messages:
    chat_db: str = "~/Library/Messages/chat.db"
    poll_seconds: float = 2


@dataclass
class Chat:
    transports: list[str] = field(default_factory=lambda: ["imessage"])  # imessage, fake


@dataclass
class Tasks:
    backend: str = "mailbox"  # mailbox or none
    mail: str = "graph"       # mailbox only: graph or imap
    poll_seconds: int = 30    # how often results are fetched (imap; graph uses graph.poll_seconds)


@dataclass
class ManagedAgents:
    model: str = "claude-opus-5-5"
    budget_usd: float | None = 5.0  # spending cap per task session, at list prices; null for none
    vault_ids: list[str] = field(default_factory=list)  # credentials for MCP servers and tools
    mcp_servers: list[dict] = field(default_factory=list)  # [{name, url}]
    web: bool = False                # let the agent use web search/fetch
    networking: str = "limited"      # limited or unrestricted (the sandbox's outbound network)
    allowed_hosts: list[str] = field(default_factory=list)  # limited networking: hosts the agent may reach
    archive_after_hours: float = 24
    # Written by `kinchat agent setup` (in the managed_agents_state block); not set by hand.
    agent_id: str = ""
    agent_version: int | None = None
    environment_id: str = ""


@dataclass
class Calendar:
    source: str = "graph"  # graph (graph.calendars), ics (Keychain account ics-urls) or none
    owner_emails: list[str] = field(default_factory=list)  # ics: drop invites these attendees haven't accepted
    days: int | None = None             # defaults to graph.calendar_days
    refresh_minutes: int | None = None  # defaults to graph.calendar_refresh_minutes


@dataclass
class Imap:
    host: str = ""
    port: int = 993
    username: str = ""
    address: str = ""          # the assistant's address; defaults to username
    sent_folder: str = "Sent"  # results are read only from here, e.g. "[Gmail]/Sent Mail"
    smtp_host: str = ""        # defaults to host
    smtp_port: int = 587       # 587 = STARTTLS, 465 = TLS


@dataclass
class Routing:
    window_minutes: int = 12
    respond_threshold: float = 0.75
    not_for_mia_threshold: float = 0.80
    quiet_minutes: int = 60
    history_messages: int = 20


@dataclass
class Replier:
    model_router: str = "claude-haiku-5-5"
    model_reply: str = "claude-sonnet-5-5"
    router_effort: str = "low"
    reply_effort: str = "low"
    reply_history_messages: int = 30
    max_msgs_per_10min: int = 6
    timeout_s: float = 15
    # Server-side refusal fallback on the reply model (Claude API only).
    fallbacks: bool = True


@dataclass
class Dashboard:
    bind: str = "127.0.0.1"
    port: int = 8770


@dataclass
class QuietHours:
    start: int = 22  # 10 PM
    end: int = 7     # 7 AM


@dataclass
class Graph:
    tenant: str = ""
    client_id: str = ""
    cert_thumbprint: str = ""
    mailbox: str = ""
    calendars: list[str] = field(default_factory=list)
    poll_seconds: int = 30
    calendar_refresh_minutes: int = 15
    calendar_days: int = 14


@dataclass
class Assistant:
    name: str = "Mia"
    aliases: list[str] = field(default_factory=list)  # other names that count as addressing the assistant
    owner: str = "the family organizer"  # who reviews suggested notes, as the reply prompt refers to them
    tag: str = "MIA"  # email subject tags: [MIA-CHAT], [MIA-POST], [MIA-HEARTBEAT]
    handles: list[str] = field(default_factory=list)  # the assistant's own iMessage addresses

    @property
    def names(self) -> list[str]:
        return [n for n in [self.name, *self.aliases] if n and n.strip()]


@dataclass
class Config:
    messages: Messages = field(default_factory=Messages)
    chat: Chat = field(default_factory=Chat)
    tasks: Tasks = field(default_factory=Tasks)
    imap: Imap = field(default_factory=Imap)
    calendar: Calendar = field(default_factory=Calendar)
    managed_agents: ManagedAgents = field(default_factory=ManagedAgents)
    routing: Routing = field(default_factory=Routing)
    replier: Replier = field(default_factory=Replier)
    dashboard: Dashboard = field(default_factory=Dashboard)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    graph: Graph = field(default_factory=Graph)
    assistant: Assistant = field(default_factory=Assistant)
    timezone: str = field(default_factory=system_timezone)
    default_country: str = "US"
    shadow_mode: bool = True
    family_md: Path = APP_DIR / "family.md"
    db_path: Path = APP_DIR / "state.db"
    history_retention_days: int = 30

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _section(cls, data: dict | None):
    data = data or {}
    return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def load(path: Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config(
        messages=_section(Messages, raw.get("messages")),
        chat=_section(Chat, raw.get("chat")),
        tasks=_section(Tasks, raw.get("tasks")),
        imap=_section(Imap, raw.get("imap")),
        calendar=_section(Calendar, raw.get("calendar")),
        managed_agents=_section(ManagedAgents, {**(raw.get("managed_agents") or {}),
                                                **(raw.get("managed_agents_state") or {})}),
        routing=_section(Routing, raw.get("routing")),
        replier=_section(Replier, raw.get("replier")),
        dashboard=_section(Dashboard, raw.get("dashboard")),
        quiet_hours=_section(QuietHours, raw.get("quiet_hours")),
        graph=_section(Graph, raw.get("graph")),
        assistant=_section(Assistant, raw.get("assistant")),
        timezone=raw.get("timezone") or system_timezone(),
        default_country=str(raw.get("default_country") or "US").upper(),
        shadow_mode=bool(raw.get("shadow_mode", True)),
    )
    if not _valid_zone(cfg.timezone):
        raise ValueError(f"config: unknown timezone {cfg.timezone!r}")
    if cfg.default_country not in CALLING_CODES:
        raise ValueError(f"config: default_country must be one of {', '.join(CALLING_CODES)}")
    if cfg.dashboard.bind not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("config: dashboard.bind must be a loopback address")
    known = {"imessage", "fake"}
    cfg.chat.transports = list(cfg.chat.transports or [])
    bad = [t for t in cfg.chat.transports if t not in known]
    if bad or not cfg.chat.transports:
        raise ValueError(f"config: chat.transports must list some of {', '.join(sorted(known))}")
    if cfg.tasks.backend not in TASK_BACKENDS:
        raise ValueError(f"config: tasks.backend must be one of {', '.join(TASK_BACKENDS)}")
    if cfg.tasks.mail not in MAIL_TRANSPORTS:
        raise ValueError(f"config: tasks.mail must be one of {', '.join(MAIL_TRANSPORTS)}")
    if cfg.calendar.source not in CALENDAR_SOURCES:
        raise ValueError(f"config: calendar.source must be one of {', '.join(CALENDAR_SOURCES)}")
    cfg.calendar.owner_emails = list(cfg.calendar.owner_emails or [])
    if cfg.calendar.days is None:
        cfg.calendar.days = cfg.graph.calendar_days
    if cfg.calendar.refresh_minutes is None:
        cfg.calendar.refresh_minutes = cfg.graph.calendar_refresh_minutes
    ma = cfg.managed_agents
    ma.vault_ids, ma.allowed_hosts = list(ma.vault_ids or []), list(ma.allowed_hosts or [])
    ma.mcp_servers = list(ma.mcp_servers or [])
    if any(not isinstance(m, dict) or not m.get("name") or not m.get("url") for m in ma.mcp_servers):
        raise ValueError("config: each managed_agents.mcp_servers entry needs a name and a url")
    if ma.networking not in ("limited", "unrestricted"):
        raise ValueError("config: managed_agents.networking must be limited or unrestricted")
    a = cfg.assistant
    if not a.name.strip() or not a.tag.strip():
        raise ValueError("config: assistant.name and assistant.tag must not be empty")
    a.aliases, a.handles = list(a.aliases or []), list(a.handles or [])
    if not a.handles and cfg.graph.mailbox:
        # Configs from before assistant.handles: the mailbox doubled as the assistant's iMessage address.
        a.handles = [cfg.graph.mailbox]
    if raw.get("family_md"):
        cfg.family_md = Path(raw["family_md"]).expanduser()
    if raw.get("db_path"):
        cfg.db_path = Path(raw["db_path"]).expanduser()
    return cfg
