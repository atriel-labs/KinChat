"""kinchat CLI: run the service, set it up, check it (doctor), or list chats found in chat.db."""
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


def cmd_doctor(cfg: config_mod.Config, probe_mailbox: str | None = None) -> int:
    from .doctor import Doctor, report
    print(f"shadow_mode={cfg.shadow_mode}, dashboard http://{cfg.dashboard.bind}:{cfg.dashboard.port}")
    d = Doctor(cfg, probe_mailbox=probe_mailbox)
    report(d.run())
    print("\nFAIL items need fixing before the bridge works." if d.failed else "\nNo failures.")
    return 1 if d.failed else 0


def cmd_agent(cfg: config_mod.Config, action: str | None, config_path: Path) -> int:
    if action != "setup":
        print("usage: kinchat agent setup")
        return 2
    import anthropic

    from .backends.managed_agents import setup, write_state
    client = anthropic.Anthropic(api_key=secrets.anthropic_api_key())
    state = setup(client, cfg)
    write_state(config_path, state)
    print(f"agent {state['agent_id']} (version {state['agent_version']}), environment {state['environment_id']}")
    print(f"saved to {config_path}; set tasks.backend: managed_agents and restart")
    return 0


def cmd_setup(args) -> int:
    from . import setup_wizard as sw

    try:
        p = sw.AnswersPrompter(sw.load_answers(args.answers)) if args.answers else sw.Prompter()
    except (OSError, sw.SetupError, ValueError) as e:
        print(f"setup: can't read answers: {e}", file=sys.stderr)
        return 2

    def agent(config_path: Path) -> int:
        return cmd_agent(config_mod.load(config_path), "setup", config_path)

    try:
        return sw.run(p, args.config, install=False if args.no_install else None, agent_setup=agent)
    except sw.SetupError as e:
        print(f"setup: {e}", file=sys.stderr)
        return 2


def cmd_install_agent(args) -> int:
    from . import launchd
    for line in launchd.install():
        print(line)
        if line.startswith("stopped"):
            print(f"To roll back: {launchd.ROLLBACK_HINT}")
    print(f"Grant Full Disk Access to: {launchd.real_python()}")
    return 0


COMMANDS = ("run", "check", "doctor", "chats", "agent", "setup", "install-agent")
NEEDS_NO_CONFIG = ("setup", "install-agent")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kinchat", description="Run the bridge or set it up.")
    p.add_argument("command", nargs="?", default="run", choices=COMMANDS,
                   help="run (default), setup, doctor (alias: check), chats, agent setup, install-agent")
    p.add_argument("action", nargs="?", help="for `agent`: setup")
    p.add_argument("--config", type=Path, default=APP_DIR / "config.yaml")
    p.add_argument("--answers", type=Path, help="setup: take answers from this YAML file instead of asking")
    p.add_argument("--no-install", action="store_true", help="setup: don't install the launchd agent")
    p.add_argument("--probe-mailbox", help="doctor: another address in the tenant that Graph must NOT be able to read")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.command in NEEDS_NO_CONFIG:
        return {"setup": cmd_setup, "install-agent": cmd_install_agent}[args.command](args)
    _setup_logging(args.verbose)
    cfg = config_mod.load(args.config)
    if args.command == "agent":
        return cmd_agent(cfg, args.action, args.config)
    if args.command in ("doctor", "check"):
        return cmd_doctor(cfg, args.probe_mailbox)
    return {"run": cmd_run, "chats": cmd_chats}[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
