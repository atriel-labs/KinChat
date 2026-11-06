"""`kinchat setup`: an interactive wizard that writes config.yaml, stores secrets and installs the agent.

Every question has a key, so the same flow runs non-interactively from `--answers answers.yaml` (tests, CI,
scripted installs). Secrets are read with getpass, stored in the Keychain with `security`, and never
printed, logged or written to a file. In an answers file they go under `secrets:`, keyed by Keychain
account; prefer typing them interactively.
"""
from __future__ import annotations

import base64
import difflib
import getpass
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

from . import keychain, launchd, paths
from .config import CALLING_CODES, _valid_zone, system_timezone

TASK_CHOICES = ("mailbox+graph", "mailbox+imap", "managed_agents", "none")
CALENDAR_CHOICES = ("graph", "ics", "none")
TRANSPORT_CHOICES = ("imessage", "fake")
FDA_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
# Shipped inside the package so a normal (non-editable) install has it too.
EXAMPLE = Path(__file__).resolve().parent / "data" / "config.example.yaml"


class SetupError(RuntimeError):
    pass


# --- asking -----------------------------------------------------------------------

class Prompter:
    """Interactive questions on the terminal."""

    def __init__(self, out=print, inp=input, secret_inp=getpass.getpass):
        self.out, self._input, self._secret = out, inp, secret_inp

    def ask(self, key: str, question: str, default: str | None = None) -> str:
        hint = f" [{default}]" if default not in (None, "") else ""
        while True:
            value = self._input(f"{question}{hint}: ").strip()
            if value:
                return value
            if default is not None:
                return default
            self.out("  (required)")

    def ask_list(self, key: str, question: str, default: list[str] | None = None) -> list[str]:
        raw = self.ask(key, question + " (comma-separated)", ", ".join(default or []))
        return [x.strip() for x in raw.split(",") if x.strip()]

    def choose(self, key: str, question: str, options: tuple[str, ...], default: str) -> str:
        while True:
            value = self.ask(key, f"{question} ({' / '.join(options)})", default)
            if value in options:
                return value
            self.out(f"  choose one of: {', '.join(options)}")

    def confirm(self, key: str, question: str, default: bool = True) -> bool:
        value = self.ask(key, question + (" [Y/n]" if default else " [y/N]"), "").lower()
        return default if not value else value.startswith("y")

    def secret(self, account: str, question: str) -> str:
        while True:
            value = self._secret(f"{question} (not shown): ")
            if value:
                return value
            self.out("  (required)")


class AnswersPrompter(Prompter):
    """Answers from a dict (an answers.yaml); a missing answer takes the default, or fails if there is none."""

    def __init__(self, answers: dict, out=print):
        super().__init__(out=out)
        self.answers = answers or {}
        self.secrets = dict(self.answers.get("secrets") or {})

    def _get(self, key, default):
        if key in self.answers and self.answers[key] is not None:
            return self.answers[key]
        if default is None:
            raise SetupError(f"answers file has no {key!r}")
        return default

    def ask(self, key, question, default=None):
        return str(self._get(key, default))

    def ask_list(self, key, question, default=None):
        v = self._get(key, default if default is not None else [])
        return [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]

    def choose(self, key, question, options, default):
        v = str(self._get(key, default))
        if v not in options:
            raise SetupError(f"answers file: {key} must be one of {', '.join(options)}")
        return v

    def confirm(self, key, question, default=True):
        return bool(self._get(key, default))

    def secret(self, account, question):
        if not self.secrets.get(account):
            raise SetupError(f"answers file has no secret for {account!r}")
        return str(self.secrets[account])


# --- the questions --------------------------------------------------------------------

def gather(p: Prompter) -> dict:
    """Ask everything; returns the plan (config sections plus which secrets to store). No side effects."""
    plan: dict = {}
    name = p.ask("assistant_name", "Assistant name (what the family calls it)", "Mia")
    tag_default = re.sub(r"[^A-Z0-9]", "", name.upper()) or "KINCHAT"
    plan["assistant"] = {
        "name": name,
        "aliases": p.ask_list("aliases", "Other names it answers to", []),
        "owner": p.ask("owner", "Who reviews suggested notes (as the assistant should call them)",
                       "the family organizer"),
        "tag": p.ask("tag", "Email subject tag", tag_default),
        "handles": p.ask_list("handles", "The assistant's own iMessage addresses (phone or email)", []),
    }
    while True:
        country = p.ask("country", f"Country for phone numbers without + ({'/'.join(CALLING_CODES)})", "US").upper()
        if country in CALLING_CODES:
            break
        if isinstance(p, AnswersPrompter):
            raise SetupError(f"answers file: country must be one of {', '.join(CALLING_CODES)}")
        p.out(f"  choose one of: {', '.join(CALLING_CODES)}")
    while True:
        tz = p.ask("timezone", "Time zone", system_timezone())
        if _valid_zone(tz):
            break
        if isinstance(p, AnswersPrompter):
            raise SetupError(f"answers file: unknown timezone {tz!r}")
        p.out("  not an IANA time zone, e.g. America/New_York")
    plan["default_country"], plan["timezone"] = country, tz

    transports = p.ask_list("transports", f"Chat transports ({'/'.join(TRANSPORT_CHOICES)})", ["imessage"])
    bad = [t for t in transports if t not in TRANSPORT_CHOICES] or ([] if transports else ["(none)"])
    if bad:
        raise SetupError(f"unknown chat transport: {', '.join(bad)}")
    plan["chat"] = {"transports": transports}

    tasks = p.choose("tasks", "Where should tasks go", TASK_CHOICES, "none")
    calendar = p.choose("calendar", "Calendar source", CALENDAR_CHOICES, "none")
    plan["tasks"] = {"backend": "mailbox" if tasks.startswith("mailbox") else tasks}
    if tasks.startswith("mailbox"):
        plan["tasks"]["mail"] = tasks.split("+")[1]
    plan["calendar"] = {"source": calendar}
    secrets = ["anthropic-api-key", "dashboard-password"]

    if tasks == "mailbox+graph" or calendar == "graph":
        plan["graph"] = {
            "tenant": p.ask("graph_tenant", "Microsoft 365 tenant domain (e.g. example.com)"),
            "client_id": p.ask("graph_client_id", "Entra app (client) ID"),
            "cert_thumbprint": p.ask("graph_cert_thumbprint", "Certificate thumbprint (SHA-1 hex)"),
            "mailbox": p.ask("graph_mailbox", "The assistant's mailbox (e.g. assistant@example.com)"),
        }
        if calendar == "graph":
            plan["graph"]["calendars"] = p.ask_list("graph_calendars", "Calendars to read (addresses)",
                                                    [plan["graph"]["mailbox"]])
        secrets.append("graph-cert-key-b64")
    if tasks == "mailbox+imap":
        user = p.ask("imap_username", "IMAP username (usually the address)")
        plan["imap"] = {
            "host": p.ask("imap_host", "IMAP server (e.g. imap.example.com)"),
            "username": user,
            "address": p.ask("imap_address", "The assistant's address", user),
            "sent_folder": p.ask("imap_sent_folder", 'Sent folder (Gmail: "[Gmail]/Sent Mail")', "Sent"),
            "smtp_host": p.ask("imap_smtp_host", "SMTP server (e.g. smtp.example.com)"),
            "smtp_port": int(p.ask("imap_smtp_port", "SMTP port (587 STARTTLS, 465 TLS)", "587")),
        }
        secrets.append("imap-app-password")
    if tasks == "managed_agents":
        plan["managed_agents"] = {
            "model": p.ask("ma_model", "Model for task sessions", "claude-opus-5-5"),
            "budget_usd": float(p.ask("ma_budget_usd", "Spending cap per task in USD", "5.00")),
            "web": p.confirm("ma_web", "Let the agent search and read the web?", False),
        }
    if calendar == "ics":
        plan["calendar"]["owner_emails"] = p.ask_list(
            "calendar_owner_emails", "Your calendar addresses (invites you haven't accepted are left out)", [])
        secrets.append("ics-urls")
    plan["_secrets"] = secrets
    return plan


# --- writing the config ----------------------------------------------------------------

SECTION_ORDER = ("shadow_mode", "assistant", "chat", "tasks", "imap", "managed_agents", "calendar", "graph",
                 "default_country", "timezone")


def example_comments(example: Path = EXAMPLE) -> dict[str, str]:
    """The comment lines directly above each top-level key in config.example.yaml."""
    try:
        lines = example.read_text().splitlines()
    except OSError:
        return {}
    out, block = {}, []
    for line in lines:
        commented_key = re.match(r"^#\s?([a-z_]+):\s*$", line)  # a commented-out section, e.g. "# imap:"
        key = re.match(r"^([a-z_]+):", line)
        if commented_key or key:
            name = (commented_key or key).group(1)
            if block and name not in out:
                out[name] = "\n".join(block)
            block = []
        elif line.startswith("#"):
            block.append(line)
        else:
            block = []  # a blank line or a value ends the comment that belongs to the next key
    return out


def render(plan: dict, example: Path = EXAMPLE) -> str:
    comments = example_comments(example)
    values = {"shadow_mode": True, **{k: v for k, v in plan.items() if not k.startswith("_")}}
    parts = ["# Written by `kinchat setup`. Edit freely; see config.example.yaml for every option.\n"
             "# Secrets are NOT in this file; they are in the Keychain (service kinchat).\n"]
    for key in SECTION_ORDER:
        if key not in values:
            continue
        text = yaml.safe_dump({key: values[key]}, sort_keys=False, allow_unicode=True, default_flow_style=False)
        parts.append((comments[key] + "\n" if key in comments else "") + text)
    return "\n".join(parts)


def write_config(text: str, path: Path, p: Prompter) -> bool:
    """Write the config; if one exists, show the diff, ask, and keep a .bak. Returns whether it was written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = path.read_text()
        if old == text:
            p.out(f"{path} is already up to date.")
            return True
        diff = difflib.unified_diff(old.splitlines(keepends=True), text.splitlines(keepends=True),
                                    fromfile=f"{path} (current)", tofile=f"{path} (new)")
        p.out("".join(diff))
        if not p.confirm("overwrite", f"Replace {path}? The current file is kept as {path.name}.bak", False):
            p.out("Left the existing config unchanged.")
            return False
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return True


# --- secrets ------------------------------------------------------------------------

SECRET_QUESTIONS = {
    "anthropic-api-key": "Anthropic API key",
    "dashboard-password": "Password for the local dashboard",
    "imap-app-password": "IMAP/SMTP app password",
    "ics-urls": "Calendar feed URL(s), separated by spaces",
}


def collect_secret(account: str, p: Prompter) -> str:
    if account == "graph-cert-key-b64":
        # The Graph certificate's private key: read the PEM file, store it base64-encoded (see secrets.py).
        if isinstance(p, AnswersPrompter) and account in p.secrets:
            return p.secret(account, "")
        pem = Path(p.ask("graph_cert_key_path", "Path to the certificate's private key (.pem)")).expanduser()
        return base64.b64encode(pem.read_bytes()).decode()
    value = p.secret(account, SECRET_QUESTIONS[account]).strip()
    return " ".join(value.split()) if account == "ics-urls" else value


# --- the whole flow ------------------------------------------------------------------

def run(p: Prompter, config_path: Path, home: Path | None = None, install: bool | None = None,
        security_run: Callable = subprocess.run, launchctl_run: Callable = subprocess.run,
        opener: Callable[[list[str]], object] | None = None, agent_setup: Callable[[Path], int] | None = None) -> int:
    home = home or Path.home()
    p.out("kinchat setup: answer a few questions; Enter takes the [default].\n")
    plan = gather(p)
    written = write_config(render(plan), config_path, p)
    if written:
        p.out(f"Wrote {config_path}")

    for account in plan["_secrets"]:
        if keychain.read(account, keychain.SERVICE, run=security_run) is not None and not p.confirm(
                f"replace_{account}", f"Keychain already has {account}. Replace it?", False):
            continue
        keychain.store(account, collect_secret(account, p))
        p.out(f"Stored {account} in the Keychain.")

    if plan["tasks"]["backend"] == "managed_agents" and p.confirm(
            "run_agent_setup", "Create the Managed Agents agent now (kinchat agent setup)?", True):
        if agent_setup is not None:
            agent_setup(config_path)

    python = launchd.venv_python()
    p.out("\nFull Disk Access (needed to read chat.db): add this binary in System Settings > Privacy & Security >\n"
          f"Full Disk Access:\n  {launchd.real_python(python)}")
    if sys.platform == "darwin" and p.confirm("open_settings", "Open that settings pane now?", True):
        (opener or (lambda cmd: subprocess.run(cmd, check=False)))(["open", FDA_PANE])

    if install is None:
        install = p.confirm("install", "Install and start the launchd agent now?", sys.platform == "darwin")
    if install:
        for line in launchd.install(home, python, run=launchctl_run):
            p.out(line)
    p.out("\nNext: `kinchat doctor` checks everything; the bridge starts in shadow mode (logs only).")
    return 0


def load_answers(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise SetupError("answers file must be a mapping")
    return data


def default_config_path(home: Path | None = None) -> Path:
    env = os.environ.get("KINCHAT_CONFIG")
    return Path(env) if env else paths.app_dir(home) / "config.yaml"
