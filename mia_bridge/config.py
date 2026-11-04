"""Load config.yaml (infrastructure only; contacts, chats and toggles live in the dashboard)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

APP_DIR = Path.home() / "Library" / "Application Support" / "mia-bridge"
LOG_DIR = Path.home() / "Library" / "Logs" / "mia-bridge"


@dataclass
class Messages:
    chat_db: str = "~/Library/Messages/chat.db"
    poll_seconds: float = 2


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
class Config:
    messages: Messages = field(default_factory=Messages)
    routing: Routing = field(default_factory=Routing)
    replier: Replier = field(default_factory=Replier)
    dashboard: Dashboard = field(default_factory=Dashboard)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    graph: Graph = field(default_factory=Graph)
    timezone: str = "America/Denver"
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
        routing=_section(Routing, raw.get("routing")),
        replier=_section(Replier, raw.get("replier")),
        dashboard=_section(Dashboard, raw.get("dashboard")),
        quiet_hours=_section(QuietHours, raw.get("quiet_hours")),
        graph=_section(Graph, raw.get("graph")),
        timezone=raw.get("timezone", "America/Denver"),
        shadow_mode=bool(raw.get("shadow_mode", True)),
    )
    if cfg.dashboard.bind not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("config: dashboard.bind must be a loopback address")
    if raw.get("family_md"):
        cfg.family_md = Path(raw["family_md"]).expanduser()
    if raw.get("db_path"):
        cfg.db_path = Path(raw["db_path"]).expanduser()
    return cfg
