"""kinchat CLI: run the service, check setup, or list chats found in chat.db."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from . import config as config_mod
from . import secrets
from .config import APP_DIR, LOG_DIR


def _setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = logging.handlers.RotatingFileHandler(LOG_DIR / "bridge.log", maxBytes=5_000_000, backupCount=5)
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file)
    root.addHandler(console)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_run(cfg: config_mod.Config) -> int:
    from .app import Bridge
    Bridge(cfg).run()
    return 0


def cmd_chats(cfg: config_mod.Config) -> int:
    from .backends.base import make_id
    from .backends.imessage import NAME, ChatDB
    for c in ChatDB(cfg.messages.chat_db).list_chats():
        kind = "group" if c["is_group"] else "1:1"
        print(f"{make_id(NAME, c['guid'])}\t{kind}\t{c['name'] or ''}\t{', '.join(c['participants'])}")
    return 0


def cmd_check(cfg: config_mod.Config) -> int:
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"  ok    {name}" + (f": {detail}" if detail else ""))
        except Exception as e:
            ok = False
            print(f"  FAIL  {name}: {e}")

    import anthropic

    from .backends.imessage import ChatDB, messages_scriptable

    print(f"shadow_mode={cfg.shadow_mode}, dashboard http://{cfg.dashboard.bind}:{cfg.dashboard.port}")
    step("family.md", lambda: f"{len(cfg.family_md.read_text())} chars")
    step("chat.db readable (Full Disk Access)", lambda: f"{len(ChatDB(cfg.messages.chat_db).list_chats())} chats")
    step("Messages scriptable (Automation)", lambda: f"{messages_scriptable()} chats")
    step("keychain: anthropic-api-key", lambda: bool(secrets.anthropic_api_key()) and None)
    step("keychain: dashboard-password", lambda: bool(secrets.dashboard_password()) and None)
    step("keychain: graph-cert-key-b64", lambda: bool(secrets.graph_cert_private_key()) and None)
    step("anthropic models", lambda: ", ".join(
        anthropic.Anthropic(api_key=secrets.anthropic_api_key()).models.retrieve(m).id
        for m in (cfg.replier.model_router, cfg.replier.model_reply)))

    def graph():
        from .graph import Graph  # imports msal
        g = cfg.graph
        client = Graph(g.tenant, g.client_id, g.cert_thumbprint, secrets.graph_cert_private_key(), g.mailbox, cfg.timezone)
        client._get(f"/users/{g.mailbox}/mailFolders/inbox", {"$select": "totalItemCount"})
        snap = client.calendar_snapshot(g.calendars, 1)
        return f"inbox readable; calendars readable ({len(snap.splitlines())} lines)"
    step("graph mailbox + calendars", graph)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kinchat")
    p.add_argument("command", nargs="?", default="run", choices=["run", "check", "chats"])
    p.add_argument("--config", type=Path, default=APP_DIR / "config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = config_mod.load(args.config)
    return {"run": cmd_run, "check": cmd_check, "chats": cmd_chats}[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
